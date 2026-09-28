"""Offline end-to-end self-check.

Runs the real application code against `hindsight_double.py`, a contract double
built from the published Hindsight Cloud 0.10.1 OpenAPI. No API keys, no
network, no credits. Run it before pointing the app at real Hindsight:

    python tests/selfcheck.py

It is not a substitute for a live run, but it does prove that every request
this client makes matches the documented contract, that the timeline is
complete, and that a thin competitor does not get a fabricated strategy.
"""

from __future__ import annotations

import json
import re
import sys
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import hindsight_double as double  # noqa: E402

PORT = 8899
BASE = f"http://127.0.0.1:{PORT}"

PASS, FAIL = "\033[32mPASS\033[0m", "\033[31mFAIL\033[0m"
_results: list[tuple[bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    _results.append((bool(condition), name))
    mark = PASS if condition else FAIL
    print(f"  [{mark}] {name}" + (f"  {detail}" if detail and not condition else ""))


def start_double():
    httpd = double.serve(PORT)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def main() -> int:
    httpd = start_double()

    # Point the app at the double before importing it.
    import os

    os.environ.update(
        {
            "HINDSIGHT_API_KEY": "hsk_selftest",
            "HINDSIGHT_BASE_URL": BASE,
            "GROQ_API_KEY": "gsk_selftest",
            "GROQ_BASE_URL": f"{BASE}/v1/openai/v1",
            "DATA_DIR": str(REPO_ROOT / "data"),
        }
    )
    for mod in [m for m in list(sys.modules) if m.startswith("backend")]:
        del sys.modules[mod]

    from backend import hindsight_client as hc
    from backend.config import API_HOST, AUTOSEED, SIGNAL_STACK_API
    from backend.ingestion import extract_signal
    from backend.synthesis import generate_strategic_read

    client = hc.client

    print("\n\033[1m1. Contract compliance — raw HTTP\033[0m")
    import requests

    s = requests.Session()
    s.headers["Authorization"] = "Bearer hsk_selftest"

    r = s.put(f"{BASE}/v1/default/banks/contract-probe", json={"retain_mission": "x"})
    check("PUT bank accepts retain_mission", r.status_code == 200, f"got {r.status_code}")

    r = s.put(f"{BASE}/v1/default/banks/contract-probe", json={"not_a_real_field": 1})
    check("PUT bank rejects unknown fields (422)", r.status_code == 422, f"got {r.status_code}")

    r = s.post(
        f"{BASE}/v1/default/banks/contract-probe/memories",
        json={"items": [{"document_id": "d", "timestamp": "2026-01-01T00:00:00Z"}]},
    )
    check("retain without content is rejected (422)", r.status_code == 422, f"got {r.status_code}")

    r = s.post(
        f"{BASE}/v1/default/banks/contract-probe/memories",
        json={"items": [{"content": "x", "metadata": {"nested": {"a": 1}}}]},
    )
    check("retain metadata must be string values (422)", r.status_code == 422, f"got {r.status_code}")

    r = s.get(f"{BASE}/v1/default/banks", params={"limit": 10, "offset": 0})
    body = r.json()
    check("GET /banks envelope has banks/total/limit/offset",
          all(k in body for k in ("banks", "total", "limit", "offset")))
    check("GET /banks item exposes fact_count + last_write_at",
          all(k in body["banks"][0] for k in ("bank_id", "fact_count", "last_write_at")))

    r = s.get(f"{BASE}/v1/default/banks/does-not-exist/memories/list")
    check("missing bank is 404", r.status_code == 404, f"got {r.status_code}")

    r = s.get(f"{BASE}/v1/default/banks/contract-probe/memories/list", params={"tags_match": "nope"})
    check("invalid tags_match is 422", r.status_code == 422, f"got {r.status_code}")

    s.delete(f"{BASE}/v1/default/banks/contract-probe")

    print("\n\033[1m2. Tag scoping — the regression this double exists for\033[0m")
    # enable_observations=True so the double derives rows, including an
    # untagged one. The app itself sets this False (see section 2b).
    s.put(f"{BASE}/v1/default/banks/competitor-tag-probe", json={"enable_observations": True})
    probe = "competitor-tag-probe"
    r = s.post(
        f"{BASE}/v1/default/banks/{probe}/memories",
        json={"items": [{"content": "Tag Probe shipped a thing on 2026-03-01.", "document_id": "d1",
                         "tags": ["signal", "type:feature"],
                         "metadata": {"signal_uid": "x", "signal_date": "2026-03-01"}}]},
    )
    check("seeded a tagged signal", r.status_code == 200)

    loose = s.get(
        f"{BASE}/v1/default/banks/{probe}/memories/list",
        params={"tags": ["signal"], "tags_match": "any"},
    ).json()
    strict = s.get(
        f"{BASE}/v1/default/banks/{probe}/memories/list",
        params={"tags": ["signal"], "tags_match": "all_strict"},
    ).json()
    check("tags_match=any pulls in untagged derived facts (the trap)",
          loose["total"] > strict["total"],
          f"any={loose['total']} strict={strict['total']}")
    # all_strict excludes the UNTAGGED row but still returns tagged observations,
    # which is exactly how the real service behaves.
    untagged = {
        u["id"]
        for u in s.get(f"{BASE}/v1/default/banks/{probe}/memories/list",
                       params={"limit": 100}).json()["items"]
        if not u.get("tags")
    }
    loose_ids = {u["id"] for u in loose["items"]}
    strict_ids = {u["id"] for u in strict["items"]}
    check("tags_match=all_strict drops every untagged row",
          untagged and not (untagged & strict_ids), f"leaked {untagged & strict_ids}")
    check("tags_match=any does return those untagged rows",
          untagged <= loose_ids, f"missing {untagged - loose_ids}")
    check("the double injects untagged noise (so this can fail)", bool(untagged))

    s.delete(f"{BASE}/v1/default/banks/{probe}")

    print("\n\033[1m2b. Derived observations (the real leak vector)\033[0m")
    # Modelled from the live API: Hindsight's consolidation adds `observation`
    # rows that INHERIT TAGS but carry no metadata and no document_id. So
    # tags_match=all_strict does not exclude them — the metadata.signal_uid
    # guard is what keeps them off the timeline. Both paths are tested here.
    s.put(f"{BASE}/v1/default/banks/obs-probe", json={"enable_observations": True})
    s.post(
        f"{BASE}/v1/default/banks/obs-probe/memories",
        json={"items": [{"content": "Obs Probe cut its price on 2026-04-01.", "document_id": "o1",
                         "timestamp": "2026-04-01T12:00:00Z", "tags": ["signal", "type:pricing"],
                         "metadata": {"signal_uid": "obs-probe-1", "signal_date": "2026-04-01",
                                      "signal_type": "pricing", "signal_summary": "Obs Probe cut its price.",
                                      "source": "t", "competitor": "Obs Probe", "raw_notes": ""}}]},
    )
    obs_units = s.get(f"{BASE}/v1/default/banks/obs-probe/memories/list", params={"limit": 100}).json()["items"]
    derived = [u for u in obs_units if u.get("fact_type") == "observation"]
    tagged = [u for u in derived if u.get("tags")]
    check("consolidation produced observation rows", len(derived) >= 1, f"got {len(derived)}")
    check("observations inherit tags (so all_strict cannot exclude them)",
          bool(tagged), f"got {len(tagged)}/{len(derived)} tagged")
    check("observations carry NO metadata", all(not u.get("metadata") for u in derived))
    check("observations carry no document_id", all(not u.get("document_id") for u in derived))
    s.delete(f"{BASE}/v1/default/banks/obs-probe")

    # The app's own configuration: observations off, so no derived rows at all.
    nimbus_units = s.get(
        f"{BASE}/v1/default/banks/competitor-nimbus-ai/memories/list", params={"limit": 200}
    ).json() if "competitor-nimbus-ai" in [b["bank_id"] for b in s.get(f"{BASE}/v1/default/banks", params={"limit": 100}).json()["banks"]] else {"items": [], "total": 0}
    check("app sends enable_observations=False (bank pre-exists, so no derived rows)",
          not nimbus_units["items"] or not [u for u in nimbus_units["items"]
                                            if u.get("fact_type") == "observation"])
    print("\n\033[1m3. Seeding the real dataset\033[0m")
    from backend.config import SEED_FILE
    from backend.models import Signal

    dataset = json.loads(SEED_FILE.read_text())
    parsed = [Signal(**row) for c in dataset["competitors"] for row in c["signals"]]
    EXPECTED_TOTAL = 71
    EXPECTED_COMPETITORS = 10
    check(f"seed file holds {EXPECTED_TOTAL} signals across {EXPECTED_COMPETITORS} competitors",
          len(parsed) == EXPECTED_TOTAL, f"got {len(parsed)}")
    check("every signal row names the competitor it is filed under",
          all(row["competitor"] == c["name"] for c in dataset["competitors"] for row in c["signals"]))
    check("signal uids are unique across the whole corpus",
          len({s.uid for s in parsed}) == len(parsed))
    # The app sorts by date on retrieval and derives uids from the date, so a
    # scrambled file cannot corrupt behaviour -- this is about the file staying
    # readable as ten hand-maintained timelines, not about correctness.
    unsorted_files = [
        c["name"] for c in dataset["competitors"]
        if [r["date"] for r in c["signals"]] != sorted(r["date"] for r in c["signals"])
    ]
    check("each competitor's signals are listed oldest-first in the file",
          not unsorted_files, f"out of order: {unsorted_files}")

    by_comp: dict[str, int] = {}
    for sig in parsed:
        res = client.write_signal(sig)
        by_comp[sig.competitor] = by_comp.get(sig.competitor, 0) + res.get("items_count", 0)
    for comp, n in sorted(by_comp.items()):
        print(f"    {comp}: wrote {n}")

    # Asserted per-competitor against the file itself, so adding a competitor
    # cannot silently skip the completeness and ordering checks.
    for c in dataset["competitors"]:
        expected = len(c["signals"])
        tl = client.get_timeline(c["name"])
        check(f"{c['name']} timeline complete ({expected})", len(tl) == expected, f"got {len(tl)}")
        check(f"{c['name']} timeline is oldest-first",
              [s.date for s in tl] == sorted(s.date for s in tl))
        check(f"{c['name']} carries no derived noise", all(s.summary for s in tl))

    check("re-seed is idempotent (document_id replaces)",
          len(client.get_timeline("Nimbus AI")) == 12)

    nimbus = client.get_timeline("Nimbus AI")
    check("metadata survived the round trip",
          all(s.signal_type in {"pricing", "feature", "hiring", "messaging", "funding"} for s in nimbus))
    check("signal types span the seeded chain",
          {s.signal_type for s in nimbus} == {"pricing", "feature", "hiring", "messaging", "funding"})

    comps = {c.name: c for c in client.list_competitors()}
    check(f"list_competitors sees all {EXPECTED_COMPETITORS}", len(comps) == EXPECTED_COMPETITORS,
          f"got {sorted(comps)}")
    check("per-competitor counts match the file",
          all(comps[c["name"]].signal_count == len(c["signals"]) for c in dataset["competitors"]),
          f"got {{{', '.join(f'{k}: {v.signal_count}' for k, v in sorted(comps.items()))}}}")
    check("app view totals match the file",
          sum(c.signal_count for c in comps.values()) == EXPECTED_TOTAL,
          f"got {sum(c.signal_count for c in comps.values())}")

    nimbus_units = s.get(
        f"{BASE}/v1/default/banks/competitor-nimbus-ai/memories/list", params={"limit": 200}
    ).json()
    # Was "all three" when the corpus had three competitors; now it has to mean
    # all of them, or the extra banks go unchecked and can quietly accumulate
    # derived rows.
    obs_banks = []
    for c in dataset["competitors"]:
        units = s.get(
            f"{BASE}/v1/default/banks/competitor-{hc.slugify_competitor(c['name'])}/memories/list",
            params={"limit": 200},
        ).json()
        if any(u.get("fact_type") == "observation" for u in units["items"]):
            obs_banks.append(c["name"])
    check(f"all {EXPECTED_COMPETITORS} banks hold zero derived observations",
          not obs_banks, f"observations found in: {obs_banks}")
    check("raw facts >= signals (extractor may split documents)",
          nimbus_units["total"] >= 12, f"got {nimbus_units['total']}")
    distinct_uids = {(u.get("metadata") or {}).get("signal_uid") for u in nimbus_units["items"]}
    distinct_uids.discard(None)
    check("12 signals -> 12 distinct signal_uids in memory",
          len(distinct_uids) == 12, f"got {len(distinct_uids)}")
    check("dedupe collapses raw facts back to 12 timeline signals",
          len(client.get_timeline("Nimbus AI")) == 12
          and nimbus_units["total"] >= 12,
          f"facts={nimbus_units['total']} timeline={len(client.get_timeline('Nimbus AI'))}")
    check("Nimbus window is 2026-02-18 -> 2026-08-19",
          (comps["Nimbus AI"].first_signal, comps["Nimbus AI"].last_signal)
          == ("2026-02-18", "2026-08-19"))

    print("\n\033[1m4. Synthesis grounding\033[0m")
    rich = generate_strategic_read("Nimbus AI")
    joined = " ".join(str(v) for v in rich.model_dump().values()).lower()
    check("rich timeline produces a strategy", "series c" in joined and "enterprise" in joined)
    check("prediction is present and falsifiable",
          bool(rich.predicted_next_move) and "falsified" in joined)
    check("cites dates that exist in the timeline",
          all(d in {s.date for s in nimbus} for d in
              [w.strip(".,") for w in rich.patterns.split() if re_date(w)]))

    # Every competitor is a distinct arc, so grounding must hold across the
    # corpus rather than only for the one competitor the demo happens to use.
    # The double returns a fixed narrative for any timeline it considers rich,
    # so these assert the *contract* -- populated, non-refusing fields -- and
    # deliberately not competitor-specific wording.
    #
    # Three tiers, because the grounding rule has two independent clauses: "fewer
    # than 4 signals" OR "no repeated pattern and no ordering". Vertex Cloud
    # (4 uncorrelated signals) trips only the second, so it is checked
    # separately from the genuinely too-thin ones -- otherwise a rule that
    # counted signals and nothing else would pass the suite.
    rich_names = [c["name"] for c in dataset["competitors"] if len(c["signals"]) >= 6]
    borderline = [c["name"] for c in dataset["competitors"] if 4 <= len(c["signals"]) < 6]
    thin_names = [c["name"] for c in dataset["competitors"] if len(c["signals"]) < 4]
    check("corpus covers all three evidence tiers",
          len(rich_names) >= 5 and len(thin_names) >= 2 and len(borderline) >= 1,
          f"rich={len(rich_names)} borderline={borderline} thin={thin_names}")
    for name in rich_names:
        out = generate_strategic_read(name)
        text = " ".join(str(v) for v in out.model_dump().values()).lower()
        populated = all(
            len(getattr(out, f)) > 30
            for f in ("patterns", "inferred_intent", "predicted_next_move", "recommendation")
        )
        check(f"{name} ({len(client.get_timeline(name))} signals) yields a full argument",
              populated and "insufficient" not in text,
              f"got {[getattr(out, f)[:40] for f in ('patterns', 'predicted_next_move')]}")

    # Caveat: the double refuses below 6 signals, so the borderline band is
    # proven to *surface* a refusal, not proven to be refused by the real model
    # at that count. The live run checks that band against Groq for real.
    for thin in thin_names + borderline:
        out = generate_strategic_read(thin)
        text = " ".join(str(v) for v in out.model_dump().values()).lower()
        check(f"{thin} ({len(client.get_timeline(thin))} signals) refuses to fabricate a pattern",
              any(k in text for k in ("insufficient", "cannot", "not predictable")))

    check("unknown competitor returns empty, not an error", client.get_timeline("Nobody Ltd") == [])

    # ------------------------------------------------------------------
    # 4b. Time anchoring
    # ------------------------------------------------------------------
    # The model only ever sees dates that appear in the timeline, so with no
    # reference point it anchors every forecast to the *last logged signal*.
    # Live, that produced a prediction with a deadline that had already passed
    # ("by 2026-09-20" on the 28th) — grounded in real signals, and still wrong
    # in the way that matters to a reader. These checks pin the clock so they
    # do not silently change meaning as the calendar moves.
    print("\n\033[1m4b. Time anchoring\033[0m")
    import datetime as _dt
    from backend.synthesis import build_prompt, evidence_clock, GROUNDING_RULES

    TODAY = _dt.date(2026, 9, 28)
    nimbus_clock = evidence_clock(nimbus, today=TODAY)
    check("cadence is the median gap between signals",
          nimbus_clock.cadence_days == 14, f"got {nimbus_clock.cadence_days}")
    check("age is measured from the last signal, not the first",
          nimbus_clock.as_of == "2026-08-19" and nimbus_clock.age_days == 40,
          f"as_of={nimbus_clock.as_of} age={nimbus_clock.age_days}")
    check("40 days silent on a 14-day rhythm reads as stale",
          nimbus_clock.staleness == "stale", f"got {nimbus_clock.staleness}")

    # The point of judging against a competitor's own rhythm: a company that
    # speaks every ten weeks is not "stale" at 47 days. Both quiet competitors
    # below have longer median gaps than their silence, so both stay current.
    for name, expect in (("Vertex Cloud", "fresh"), ("Pathfinder Labs", "fresh")):
        c = evidence_clock(client.get_timeline(name), today=TODAY)
        check(f"{name} is not stale ({c.cadence_days}-day rhythm, {c.age_days}d quiet)",
              c.staleness == expect, f"got {c.staleness}")

    # Same data, different clock -> different verdict. Proves the assessment is
    # computed rather than a constant attached to the prompt.
    check("fresh when today is the last signal's own day",
          evidence_clock(nimbus, today=_dt.date(2026, 8, 19)).staleness == "fresh")
    check("aging between 1x and 2x the cadence",
          evidence_clock(nimbus, today=_dt.date(2026, 9, 10)).staleness == "aging")

    p = build_prompt("Nimbus AI", nimbus, today=TODAY)
    check("prompt states today's date explicitly",
          "TIME REFERENCE: Today is 2026-09-28" in p)
    check("prompt quantifies the evidence gap",
          "40 day(s) ago" in p and "2026-08-19" in p)
    check("prompt marks stale evidence as STALE", "STALE" in p)
    check("prompt cites the competitor's own rhythm", "~14-day signal rhythm" in p)
    check("prompt forbids forecasting from the last signal date",
          "Forecast from TODAY" in p)
    check("prompt forbids a past date as a future prediction",
          "NEVER present a date that is already in the past" in p)
    check("TIME ANCHORING rules reached the prompt", "TIME ANCHORING" in p and "TIME ANCHORING" in GROUNDING_RULES)
    check("time-anchored prompt still satisfies Groq's json_object precondition",
          "json" in p.lower())

    anchored = generate_strategic_read("Nimbus AI", today=TODAY)
    check("response carries the evidence as-of date",
          anchored.data_as_of == "2026-08-19", f"got {anchored.data_as_of}")
    check("response carries the evidence age",
          anchored.evidence_age_days == 40, f"got {anchored.evidence_age_days}")
    check("response carries the staleness verdict",
          anchored.evidence_staleness == "stale", f"got {anchored.evidence_staleness}")

    # The real regression: a *deadline* in the past presented as a forecast.
    # Past dates are legitimate in a prediction when cited as evidence ("this
    # rests on evidence that stopped at 2026-08-19"), so the invariant is not
    # "no past dates" but "a future deadline is actually named". The original
    # bug produced a prediction whose only date was 2026-09-20, already gone on
    # the 28th — zero future dates, which is what this catches.
    pred_dates = [
        _dt.date.fromisoformat(w.strip(".,()"))
        for w in anchored.predicted_next_move.split()
        if re_date(w.strip(".,()"))
    ]
    check("prediction names at least one future deadline",
          any(d > TODAY for d in pred_dates),
          f"dates cited: {[str(d) for d in pred_dates]}")
    check("no date in the prediction is mis-parsed as a date",
          all(d.year >= 2026 for d in pred_dates), f"got {[str(d) for d in pred_dates]}")
    check("prediction is anchored to the injected clock, not the last signal",
          "2026-09-28" in anchored.predicted_next_move,
          f"got {anchored.predicted_next_move[:100]!r}")
    check("stale evidence is disclosed in the prediction",
          "quiet" in anchored.predicted_next_move.lower(),
          f"got {anchored.predicted_next_move[:120]!r}")
    check("stale evidence is disclosed in the recommendation",
          "re-check" in anchored.recommendation.lower(),
          f"got {anchored.recommendation[:120]!r}")
    check("stale disclosure never degrades into refusing to analyse",
          "insufficient" not in anchored.patterns.lower()
          and len(anchored.predicted_next_move) > 60)

    fresh = generate_strategic_read("Nimbus AI", today=_dt.date(2026, 8, 25))
    check("fresh evidence does not carry a staleness disclaimer",
          "quiet" not in fresh.predicted_next_move.lower()
          and fresh.evidence_staleness == "fresh",
          f"staleness={fresh.evidence_staleness}")

    # Freshness is provenance, so the no-LLM path has to report it too.
    from backend.synthesis import _fallback_response

    fb = _fallback_response("Nimbus AI", nimbus, "test", nimbus_clock)
    check("fallback read also reports evidence freshness",
          fb.data_as_of == "2026-08-19" and fb.evidence_staleness == "stale")

    # One signal has no rhythm to compare against, so it must not claim one.
    solo = [nimbus[0]]
    solo_clock = evidence_clock(solo, today=TODAY)
    check("a single signal has unknown cadence, not a fabricated one",
          solo_clock.cadence_days is None and solo_clock.staleness == "unknown",
          f"got {solo_clock.cadence_days} / {solo_clock.staleness}")
    check("empty timeline has no as-of date",
          evidence_clock([], today=TODAY).as_of == "")

    print("\n\033[1m5. Live ingestion\033[0m")
    sig = extract_signal(
        "Nimbus AI opened a Senior Solutions Architect role in the enterprise segment on 2026-09-20.",
        "Nimbus AI",
    )
    check("extraction returns a typed signal", sig is not None and bool(sig.signal_type))
    client.write_signal(sig)
    check("new signal is retrievable from memory",
          len(client.get_timeline("Nimbus AI")) == 13,
          f"got {len(client.get_timeline('Nimbus AI'))}")

    print("\n\033[1m6. Malformed-LLM recovery\033[0m")
    from backend.llm_client import extract_json

    for label, raw in [
        ("fenced", '```json\n{"a":1}\n```'),
        ("preamble", 'Sure! Here it is: {"a":1}'),
        ("think block", '<think>hmm</think>\n{"a":1}'),
        ("single quotes", "{'a': 1}"),
        ("trailing comma", '{"a": 1,}'),
        ("truncated nested", '{"a":1,"b":{"c":"cut'),
    ]:
        got = extract_json(raw)
        check(f"recovers {label}", isinstance(got, dict) and bool(got), f"got {got!r}")

    print("\n\033[1m7. Model availability (Groq plans differ)\033[0m")
    from backend import config as cfg
    from backend.llm_client import GroqClient

    gc = GroqClient()
    served = gc.available_models()
    check("GET /models answered", bool(served), f"got {served!r}")
    cands = gc._model_candidates("openai/gpt-oss-120b")
    check("primary is served by this account", "openai/gpt-oss-120b" in cands, f"got {cands}")
    check("unserved model is dropped, not retried",
          gc._model_candidates("qwen/qwen3-32b") != ["qwen/qwen3-32b"],
          f"got {gc._model_candidates('qwen/qwen3-32b')}")
    check("every candidate is actually served", all(c in served for c in cands), f"got {cands}")
    check("default fallback is a real Groq model id",
          cfg.GROQ_FALLBACK_MODEL in served, f"got {cfg.GROQ_FALLBACK_MODEL}")

    # Groq's json_object mode requires the literal word "json" in the prompt.
    check("synthesis prompt satisfies Groq's json_object precondition",
          "json" in __import__("backend.synthesis", fromlist=["build_prompt"])
          .build_prompt("Nimbus AI", nimbus).lower())
    from backend.ingestion import EXTRACTION_PROMPT

    check("extraction prompt satisfies Groq's json_object precondition",
          "json" in EXTRACTION_PROMPT.lower())

    print("\n\033[1m8. Cold-start bootstrap (deploy path)\033[0m")
    # A fresh container has empty memory. bootstrap() in backend/main.py must
    # actually populate it. This went silently dead once because it called a
    # module-level function that only exists as a client method, and the
    # try/except swallowed it — so the deploy came up looking empty. These
    # checks call the real hook against the double.
    from backend.config import API_HOST, AUTOSEED, SIGNAL_STACK_API
    from backend.ingestion import load_seed_file, seed_from_file
    from backend.main import bootstrap

    check("seed_from_file writes the dataset",
          seed_from_file()["written"] == EXPECTED_TOTAL,
          f"got {seed_from_file()['written']}")
    check(f"load_seed_file returns {EXPECTED_COMPETITORS} competitors",
          len(load_seed_file()) == EXPECTED_COMPETITORS, f"got {len(load_seed_file())}")

    # Behaviour, not "did it raise": bootstrap() swallows its own errors by
    # design, so asserting the absence of an exception passes even when seeding
    # is completely dead. Empty the memory, boot, and require it to refill.
    #
    # Isolate the display-name registry too, or this would not resemble a fresh
    # container: the registry is a local cache that deliberately outlives a
    # deleted bank, so leaving it in place makes empty memory look populated.
    import tempfile
    real_registry = hc.REGISTRY_FILE
    hc.REGISTRY_FILE = Path(tempfile.mkdtemp()) / "competitors.json"
    # bootstrap() reads AUTOSEED at call time from config, and it is now off by
    # default. The deploy path opts in (Dockerfile sets AUTOSEED=1), so this
    # section must too — otherwise it silently tests the no-op branch and the
    # cold-start contract goes unverified, which is exactly how it broke before.
    import backend.config as _cfg
    real_autoseed = _cfg.AUTOSEED
    _cfg.AUTOSEED = True
    import backend.main as _main
    _main.AUTOSEED = True
    try:
        for b in hc.client.list_banks():
            if (b.get("bank_id") or "").startswith("competitor-"):
                hc.client.delete_bank(b["bank_id"].removeprefix("competitor-"))
        check("memory is empty before boot",
              not [b for b in hc.client.list_banks()
                   if (b.get("bank_id") or "").startswith("competitor-")],
              f"got {len(hc.client.list_banks())} banks")

        bootstrap()
        banks = [b for b in hc.client.list_banks()
                 if (b.get("bank_id") or "").startswith("competitor-")]
        check(f"bootstrap() populates all {EXPECTED_COMPETITORS} banks (the real deploy path)",
              len(banks) == EXPECTED_COMPETITORS, f"got {len(banks)} banks")

        # The double splits documents into several facts each, so its raw
        # fact_count is legitimately ~2x the signal count. The app's own view
        # is what must equal the corpus size.
        app_total = sum(c.signal_count for c in hc.client.list_competitors())
        check(f"boot-seeded app view is {EXPECTED_TOTAL} signals",
              app_total == EXPECTED_TOTAL, f"got {app_total}")
        raw_after_first = sum(b.get("fact_count") or 0 for b in banks)
        check("raw fact_count >= signals (splitting is allowed)",
              raw_after_first >= EXPECTED_TOTAL, f"got {raw_after_first}")

        bootstrap()  # a restart must not duplicate anything
        raw_after_second = sum(
            b.get("fact_count") or 0
            for b in hc.client.list_banks()
            if (b.get("bank_id") or "").startswith("competitor-")
        )
        check("bootstrap() is idempotent: raw facts do not grow on restart",
              raw_after_second == raw_after_first,
              f"{raw_after_first} -> {raw_after_second}")
        check(f"bootstrap() is idempotent: app view still {EXPECTED_TOTAL} signals",
              sum(c.signal_count for c in hc.client.list_competitors()) == EXPECTED_TOTAL)
    finally:
        hc.REGISTRY_FILE = real_registry
        _cfg.AUTOSEED = real_autoseed
        _main.AUTOSEED = real_autoseed

    check("the deploy path opts in to boot seeding explicitly",
          "AUTOSEED=1" in Path("Dockerfile").read_text()
          and 'value: "1"' in Path("render.yaml").read_text(),
          "a fresh container would come up with empty memory")

    check("API binds loopback by default (not the whole interface)",
          API_HOST == "127.0.0.1", f"got {API_HOST}")
    check("frontend is pointed at the API by env, overridable",
          SIGNAL_STACK_API.startswith("http://"), f"got {SIGNAL_STACK_API}")
    # Boot-time seeding writes to whichever Hindsight account the key points at,
    # so it is opt-in. The deploy path sets AUTOSEED=1 explicitly (asserted
    # below), and a developer pointing at a live account gets a no-op instead of
    # a surprise write.
    check("autoseed is off by default (no surprise writes on boot)",
          AUTOSEED is False, f"got {AUTOSEED}")
    check("start.sh is executable and execs streamlit (PID-1 safe)",
          Path("start.sh").exists()
          and "exec " in Path("start.sh").read_text()
          and "streamlit" in Path("start.sh").read_text())
    check("Dockerfile exposes only the UI port",
          "EXPOSE 8501" in Path("Dockerfile").read_text()
          and "EXPOSE 8000" not in Path("Dockerfile").read_text())
    check("Dockerfile refuses to bake a .env into the image",
          "test ! -f .env" in Path("Dockerfile").read_text())

    # The display-name registry is gitignored and NOT in the image, so a fresh
    # container has none. It used to fall back to Hindsight's `name`, which
    # echoes bank_id — the UI then showed "competitor-nimbus-ai" and resolved
    # lookups to "competitor-competitor-nimbus-ai", silently rendering 0
    # signals. Found only by running the real image.
    import tempfile as _tf
    real_reg, real_seed = hc.REGISTRY_FILE, hc.SEED_FILE
    hc.REGISTRY_FILE = Path(_tf.mkdtemp()) / "competitors.json"
    try:
        check("fresh container: registry self-initialises from the seed file",
              hc._read_registry() != {}, f"got {hc._read_registry()}")
        check("fresh container: true display name survives",
              hc._display_name("nimbus-ai") == "Nimbus AI",
              f"got {hc._display_name('nimbus-ai')!r}")
        check("display name round-trips to the right bank id",
              hc.bank_id_for(hc._display_name("nimbus-ai")) == "competitor-nimbus-ai",
              f"got {hc.bank_id_for(hc._display_name('nimbus-ai'))}")
        # A bank_id echoed back as `name` must not be mistaken for a display
        # name — that was the mechanism of the bug.
        echoed = [b for b in double.BANKS.values()]
        comps = client.list_competitors()
        check("no competitor is ever named after its bank_id",
              all(c.name != c.bank_id and not c.name.startswith("competitor-") for c in comps),
              f"got {[c.name for c in comps]}")
        check("all competitors resolve to real timelines",
              all(c.signal_count > 0 for c in comps),
              f"got {[(c.name, c.signal_count) for c in comps]}")
    finally:
        hc.REGISTRY_FILE, hc.SEED_FILE = real_reg, real_seed

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # 9. Audit regressions. One check per failure mode the ten-competitor
    #    sweep actually found, and one per validator. Section 10 then mutates
    #    the rule each check pins and requires that check to fail, so a rule
    #    that stops existing cannot leave a green suite behind.
    # ------------------------------------------------------------------
    print("\n\033[1m9. Audit regressions (A-E) and validator enforcement\033[0m")
    from datetime import date

    from backend import facts as _facts
    from backend import validators as _val
    from backend.facts import build_facts
    from backend.llm_client import (LLMError, RATE_LIMIT_DEFAULT_WAIT,
                                    RATE_LIMIT_MAX_WAIT, RateLimitError,
                                    _retry_after_seconds)
    from backend.models import Confidence as _Conf, Signal
    from backend.synthesis import (_coerce_confidence, _correction_notice,
                                   is_refusal as _is_refusal, _unvalidated_response,
                                   evidence_clock, timeline_window as _tw)

    _TODAY = date(2026, 9, 28)          # the audit date, pinned
    _seed = load_seed_file()
    _timeline = {
        name: " ".join(f"{s.date} {s.signal_type} {s.summary}" for s in sigs)
        for name, sigs in _seed.items()
    }

    # The single pre-fix response the audit kept coming back to, kept verbatim
    # so the regressions below are about that text and not about a tidy
    # re-imagining of it. Nimbus: one real feature->hiring repeat hidden among
    # nine one-off transitions, a stream 26 days past its own rhythm, and a
    # forecast that says none of that.
    PRE_FIX = {
        "patterns": (
            "Nimbus AI's hiring -> pricing -> messaging -> feature cycle repeats "
            "three times, and pricing cuts consistently precede tier launches."
        ),
        "inferred_intent": "Land-grab: win enterprise first, monetise afterwards.",
        "predicted_next_move": "They will launch an Enterprise tier within 3 weeks.",
        "recommendation": "Ship SSO before they do.",
        "confidence": "high",
        "missing_evidence": "A second pricing cut would confirm the cycle.",
    }
    nimbus_facts = build_facts(_seed["Nimbus AI"], today=_TODAY)
    nimbus_tl = _timeline["Nimbus AI"]

    # -- A. repetition may only be claimed when a transition repeats --------
    check("A: Nimbus has exactly one repeating transition (feature -> hiring x2)",
          nimbus_facts.repeated_transitions() == {("feature", "hiring"): 2},
          f"got {nimbus_facts.repeated_transitions()}")
    check("A: the other 9 Nimbus transitions are single instances",
          len(nimbus_facts.single_transitions()) == 9,
          f"got {len(nimbus_facts.single_transitions())}")
    check("A: no transition occurs 3 times anywhere, so 'repeats three times' is unsupported",
          max(max(f.transitions.values(), default=0) for f in
              (build_facts(s, today=_TODAY) for s in _seed.values())) < 3,
          "a 3-occurrence transition exists; the fixture no longer proves the point")
    check("A: the FACTS block tells the model which transitions may be called cycles",
          "transitions that DO repeat" in nimbus_facts.render()
          and "seen only ONCE" in nimbus_facts.render(),
          nimbus_facts.render()[:200])
    check("A: the prompt states the >=2 rule and forbids counting the prediction",
          "at least twice" in GROUNDING_RULES
          and "one instance" in GROUNDING_RULES.lower()
          and "never count your own prediction" in GROUNDING_RULES,
          "the repetition rule is missing from the prompt")

    # -- B. an overdue stream must be acknowledged and capped below high -----
    check("B: Nimbus computes as 26 days overdue (age 40, median 14)",
          (nimbus_facts.age_days, nimbus_facts.median_interval,
           nimbus_facts.days_overdue) == (40, 14, 26),
          f"got age={nimbus_facts.age_days} median={nimbus_facts.median_interval} "
          f"overdue={nimbus_facts.days_overdue}")
    check("B: the FACTS block states the overdue figure in days",
          "days overdue: 26" in nimbus_facts.render(), nimbus_facts.render()[:200])
    check("B: the prompt gains an overdue clause only when a stream is overdue",
          "MUST NOT be \"high\"" in nimbus_facts.overdue_instruction()
          and build_facts(_seed["Corvus Data"], today=_TODAY).overdue_instruction() == "",
          "the overdue clause is unconditional or absent")
    check("B: the overdue clause reaches the actual prompt",
          "26 day(s) past its 14-day rhythm" in build_prompt(
              "Nimbus AI", _seed["Nimbus AI"], today=_TODAY),
          "clause missing from the prompt")
    check("B: the pre-fix read is rejected: silent on the gap, and claims high",
          any("overdue" in p for p in
              _val.check_overdue_acknowledged(PRE_FIX, nimbus_facts,
                                              PRE_FIX["predicted_next_move"]))
          and any("high" in p for p in
                  _val.check_overdue_acknowledged(PRE_FIX, nimbus_facts,
                                                  PRE_FIX["predicted_next_move"])),
          _val.check_overdue_acknowledged(PRE_FIX, nimbus_facts,
                                          PRE_FIX["predicted_next_move"]))
    acknowledged = {
        **PRE_FIX,
        "confidence": "low",
        "predicted_next_move": (
            "The stream has been quiet for 26 day(s) past its 14-day rhythm and no "
            "signal has arrived since 2026-08-19, so timing is a projection: a "
            "messaging shift by 2026-10-20 would confirm the stream resumed."
        ),
    }
    check("B: the same forecast with the gap stated and confidence=low is accepted",
          not _val.check_overdue_acknowledged(
              acknowledged, nimbus_facts, acknowledged["predicted_next_move"]),
          _val.check_overdue_acknowledged(acknowledged, nimbus_facts,
                                          acknowledged["predicted_next_move"]))
    corvus_facts = build_facts(_seed["Corvus Data"], today=_TODAY)
    check("B: a stream within rhythm is not capped (Corvus, 0 days overdue)",
          _val.check_overdue_acknowledged(
              {**PRE_FIX, "confidence": "high"}, corvus_facts,
              "They announce a cloud partnership by 2026-10-30.") == [],
          "a current stream was wrongly constrained")
    check("B: staleness is reported alongside the overdue figure",
          (nimbus_facts.staleness, corvus_facts.staleness) == ("stale", "fresh"),
          f"got {nimbus_facts.staleness}/{corvus_facts.staleness}")

    # -- C. the prediction may not silently skip a stage of its own cycle ----
    nimbus_prompt = build_prompt("Nimbus AI", _seed["Nimbus AI"], today=_TODAY)
    check("C: the last three signals are in the prompt verbatim",
          all(s.summary in nimbus_prompt for s in nimbus_facts.last_three)
          and [s.summary for s in nimbus_facts.last_three]
          == [s.summary for s in _seed["Nimbus AI"][-3:]],
          "the last three are not in the prompt, or are not the real ones")
    check("C: the prompt requires consistency with those three and names the skip",
          "consistent with the three most recent signals" in nimbus_prompt
          and "silently skip a stage" in nimbus_prompt,
          "the consistency rule is missing from the prompt")
    check("C: the fixture reproduces the skip (Nimbus ends on pricing, cycle expects messaging)",
          _seed["Nimbus AI"][-1].signal_type == "pricing",
          f"last signal is {_seed['Nimbus AI'][-1].signal_type}")
    check("C: staleness reaches the reader through the age sentence",
          "STALE" in build_prompt("Nimbus AI", _seed["Nimbus AI"], today=_TODAY),
          "the age sentence does not flag stale evidence")

    # -- shared fixtures, defined once for sections 9 and 10 ---------------
    # Each single-cause fixture below fails exactly one rule. That is what lets
    # a mutation in section 10 prove a single line is load-bearing: if the
    # fixture also tripped a different rule, deleting the target would change
    # nothing and the mutation would pass vacuously.
    lumen_facts = build_facts(_seed["Lumen Health"], today=_TODAY)
    brightline_facts = build_facts(_seed["Brightline Retail"], today=_TODAY)
    brightline_tl = _timeline["Brightline Retail"]
    b_only = {**acknowledged, "predicted_next_move": "A messaging shift by 2026-10-20, "
                                                     "once they resume posting."}
    d_only = {k: v for k, v in acknowledged.items() if k != "confidence"}
    m_only = {k: v for k, v in acknowledged.items() if k != "missing_evidence"}
    invented_in_recommendation = {
        **PRE_FIX,
        "recommendation": "Counter their claim of 'the only clinically validated scribe "
                          "in the market'.",
    }
    month_restatement = {**PRE_FIX,
                         "patterns": "Brightline's rhythm is about 1 month between signals."}
    true_cadence = {**PRE_FIX, "patterns": "The cadence is 21 to 36 days."}
    fabricated_60 = {**PRE_FIX, "patterns": "Lumen Health moves on a 60 day cadence."}
    # A citation, as opposed to a cadence claim. Built from a purpose-made
    # timeline because no summary in the seed corpus carries a day figure that
    # falls outside the interval band: every real figure there is already a
    # measured interval, so the rule the exemption exists for cannot be shown
    # with the shipped data. Two signals 40 days apart, one of which states a
    # 15-day span that is neither the interval nor within tolerance of it.
    _cite_signals = [
        Signal(competitor="Fixture Co", date="2026-06-01", signal_type="feature",
               summary="Beta opens.", source="press release"),
        Signal(competitor="Fixture Co", date="2026-07-11", signal_type="hiring",
               summary="They closed the role gap within 15 days of the beta opening.",
               source="job board"),
    ]
    cite_facts = build_facts(_cite_signals, today=_TODAY)
    cite_evidence = " ".join(f"{s.date} {s.signal_type} {s.summary}"
                             for s in _cite_signals)
    cited = {**PRE_FIX, "patterns": "They closed the role gap within 15 days of the beta."}
    check("E: the citation fixture is a citation, not a cadence (15 is no real interval)",
          15 not in cite_facts.allowed_interval_claims()
          and "15" in cite_evidence,
          f"allowed: {sorted(cite_facts.allowed_interval_claims())}")
    real_citation = {**PRE_FIX,
                     "predicted_next_move": "A demand-forecasting feature landed on "
                                             "2026-04-13, so expect a messaging reset by "
                                             "2026-10-20."}
    narrative_past_date = {**PRE_FIX,
                           "predicted_next_move": "The 2026-04-02 demand-forecasting "
                                                   "launch was never announced, so expect "
                                                   "a messaging reset by 2026-10-20."}
    past_deadline = {**PRE_FIX,
                     "predicted_next_move": ("The next expected signal is a messaging "
                                             "announcement on 2026-08-03.")}
    _lumen_evidence = _timeline["Lumen Health"]
    check("each single-cause fixture trips exactly one rule, and nothing else",
          all([
              any("acknowledge the gap" in p for p in
                  _val.check_overdue_acknowledged(b_only, nimbus_facts,
                                                  b_only["predicted_next_move"])),
              any("confidence is absent" in p for p in
                  _val.check_confidence_present(d_only, nimbus_facts)),
              any("missing_evidence is absent" in p for p in
                  _val.check_confidence_present(m_only, nimbus_facts)),
              bool(_val.check_quotes_verbatim(invented_in_recommendation,
                                              build_facts(_seed["Lumen Health"],
                                                          today=_TODAY),
                                              _lumen_evidence)),
              bool(_val.check_interval_claims(month_restatement, brightline_facts,
                                              month_restatement["patterns"],
                                              evidence=brightline_tl)) is False,
              bool(_val.check_interval_claims(fabricated_60, lumen_facts,
                                              fabricated_60["patterns"],
                                              evidence=_lumen_evidence)),
              not _val.check_interval_claims(cited, cite_facts, cited["patterns"],
                                             evidence=cite_evidence),
              any("2026-08-03" in p for p in
                  _val.check_predicted_date_future(past_deadline, brightline_facts,
                                                   past_deadline["predicted_next_move"])),
              not _val.check_predicted_date_future(real_citation, brightline_facts,
                                                   real_citation["predicted_next_move"]),
              not _val.check_predicted_date_future(narrative_past_date, brightline_facts,
                                                   narrative_past_date["predicted_next_move"]),
          ]),
          "a single-cause fixture is not single-cause")

    # Two ways this check was quietly broken, both found by sweeping all ten
    # competitors with fabricated cadences rather than by reading the code.
    # 1. The citation exemption was a substring test, so "20" was satisfied by
    #    every 2026 date and "10" by a 10x% price cut.
    for _name, _prose in [
        ("Brightline Retail", "Brightline Retail runs on a 10 day cadence."),
        ("Vertex Cloud", "Vertex Cloud runs on a 30 day cadence."),
        ("Nimbus AI", "Nimbus AI runs on a 120 day cadence."),
        ("Tidewater Analytics", "Tidewater Analytics runs on a 10 day cadence."),
    ]:
        _f = build_facts(_seed[_name], today=_TODAY)
        _ev = _timeline[_name]
        _digits_present = any(str(d) in _ev for d in (10, 30, 120))
        check(f"E: '{_prose[:28]}...' is rejected even though its digits appear "
              f"in the evidence",
              _digits_present
              and bool(_val.check_interval_claims(PRE_FIX, _f, _prose, evidence=_ev)),
              "a non-duration number in the evidence licensed a cadence")
    # 2. Day and week figures shared one bag, so the tolerance window bridged
    #    them: "10 day cadence" matched Brightline's 84-day age as 12 weeks.
    check("E: a day claim cannot be satisfied by a week figure (10d is not within 2 of 28d)",
          12 in brightline_facts.allowed_interval_claims()
          and 12 not in brightline_facts.day_interval_claims()
          and bool(_val.check_interval_claims(
              {**PRE_FIX, "patterns": "Brightline Retail runs on a 10 day cadence."},
              brightline_facts, "Brightline Retail runs on a 10 day cadence.",
              evidence=brightline_tl)),
          "the unit bags are still mixed")
    check("E: every fabricated cadence in a ten-competitor sweep is rejected, or "
          "defensible on a measured figure",
          all(
              bool(_val.check_interval_claims(PRE_FIX, build_facts(_seed[n], today=_TODAY),
                                              f"{n} runs on a {d} day cadence.",
                                              evidence=_timeline[n]))
              or any(abs(d - r) <= 2
                     for r in build_facts(_seed[n], today=_TODAY).day_interval_claims())
              or d in _val._stated_durations(_timeline[n])
              for n in _seed for d in (10, 20, 30, 45, 60, 90, 120)
          ),
          "a fabricated cadence passed without a measured figure behind it")
    check("B: the single-cause fixture is rejected overall, only for the gap",
          _val.validate_response(b_only, nimbus_facts, nimbus_tl)
          and all("acknowledge the gap" in p for p in
                  _val.validate_response(b_only, nimbus_facts, nimbus_tl)),
          _val.validate_response(b_only, nimbus_facts, nimbus_tl))

    # -- D. calibration is required, and quotes must be verbatim ------------
    cases = [
        ("both fields present", {**PRE_FIX}, True),
        ("confidence absent", {k: v for k, v in PRE_FIX.items() if k != "confidence"}, False),
        ("missing_evidence absent", {k: v for k, v in PRE_FIX.items()
                                     if k != "missing_evidence"}, False),
        ("confidence nonsense", {**PRE_FIX, "confidence": "quite high"}, False),
        ("confidence empty", {**PRE_FIX, "confidence": "  "}, False),
    ]
    for label, resp, should_pass in cases:
        probs = _val.check_confidence_present(resp, nimbus_facts)
        check(f"D: {label} -> {'accept' if should_pass else 'reject'}",
              (not probs) if should_pass else bool(probs), f"got {probs}")

    # The fabricated quotations the live sweep accepted. They read exactly like
    # timeline prose, which is why a loose "does it look quoted" test passed.
    invented = {
        **PRE_FIX,
        "patterns": (
            "The launch is billed as 'end-to-end store automation for multi-site "
            "retailers' and the stated goal is 'a mandatory store-execution platform'."
        ),
    }
    check("D: invented quotations are rejected even in a well-formed response",
          bool(_val.check_quotes_verbatim(invented, brightline_facts, brightline_tl)),
          _val.check_quotes_verbatim(invented, brightline_facts, brightline_tl))
    check("D: a coined label is not mistaken for a quotation",
          not _val.check_quotes_verbatim(
              {**PRE_FIX, "patterns": "A 'Build-then-Monetize' sequence is visible."},
              nimbus_facts, nimbus_tl),
          "a coined label was flagged")
    check("D: a real verbatim quotation is accepted",
          not _val.check_quotes_verbatim(
              {**PRE_FIX, "patterns": "They replaced 'AI for everyone' with "
                                     "'Enterprise-ready AI for growing companies'."},
              nimbus_facts, nimbus_tl),
          "a genuine quotation was rejected")
    check("D: a quotation invented in the recommendation is also caught",
          bool(_val.check_quotes_verbatim(
              invented_in_recommendation,
              build_facts(_seed["Lumen Health"], today=_TODAY),
              _timeline["Lumen Health"])),
          "an invented quote outside patterns slipped through")

    refusal = {
        "patterns": "The evidence is insufficient to identify a pattern.",
        "inferred_intent": "Not inferred.",
        "predicted_next_move": "No reliable prediction can be made from four signals.",
        "recommendation": "Keep collecting signals.",
        "confidence": "none",
        "missing_evidence": "A fifth signal showing a repeat of any transition.",
    }
    check("D: a well-formed refusal passes (exempt from interval and quote checks)",
          _val.validate_response(refusal, nimbus_facts, nimbus_tl, refused=True) == [],
          _val.validate_response(refusal, nimbus_facts, nimbus_tl, refused=True))
    check("D: a refusal that claims confidence is rejected",
          any("refusal" in p for p in
              _val.validate_response({**refusal, "confidence": "medium"}, nimbus_facts,
                                     nimbus_tl, refused=True)),
          "a mid-confidence refusal passed")
    check("D: a refusal missing its missing_evidence sentence is rejected",
          any("missing_evidence" in p for p in
              _val.validate_response({k: v for k, v in refusal.items()
                                      if k != "missing_evidence"},
                                     nimbus_facts, nimbus_tl, refused=True)),
          "a refusal with no named observation passed")
    check("D: refusal is decided by the confidence field and nothing else",
          _is_refusal({**PRE_FIX, "confidence": "none"})
          and not _is_refusal({**PRE_FIX, "confidence": "low"})
          and not _is_refusal({**PRE_FIX, "confidence": "medium"})
          and not _is_refusal({**PRE_FIX, "confidence": "high"})
          and not _is_refusal(PRE_FIX),
          "refusal classification is not following the structured field")
    check("D: prose that sounds like a refusal does not make one",
          not _is_refusal({**PRE_FIX,
                           "patterns": "The 2026-04-15 cut was insufficient to move "
                                       "enterprise buyers, so they re-priced in May.",
                           "predicted_next_move": "They re-price within two weeks."}),
          "a narrative was classified as a refusal on its wording")
    check("D: prose cannot talk a reader into 'none' either",
          not _is_refusal({**PRE_FIX,
                           "confidence": "None of these observations support a "
                                         "cadence, but the feature/hiring pairing is real."}),
          "an English 'none' inside the confidence field became a refusal")
    check("D: a refusal keeps the exemption and still owes its calibration",
          # The exemption is real: a refusal is not asked to forecast.
          not _val.validate_response(
              {"patterns": "No transition repeats.", "inferred_intent": "None.",
               "predicted_next_move": "No reliable prediction can be made.",
               "recommendation": "Keep collecting signals.",
               "confidence": "none",
               "missing_evidence": "A second occurrence of any transition."},
              nimbus_facts, nimbus_tl, refused=True)
          # and it is not an exemption from saying how sure it is.
          and _val.validate_response(
              {"patterns": "No transition repeats.", "inferred_intent": "None.",
               "predicted_next_move": "No reliable prediction can be made.",
               "recommendation": "Keep collecting signals.",
               "confidence": "none"},
              nimbus_facts, nimbus_tl, refused=True) == ["missing_evidence is absent"],
          "the refusal branch is not holding refusals to the calibration rules")

    # -- E. every stated interval must be one the FACTS block supports -------
    check("E: Lumen's real intervals are 21-36 days, median 26",
          (lumen_facts.min_interval, lumen_facts.max_interval,
           lumen_facts.median_interval) == (21, 36, 26),
          f"got {lumen_facts.min_interval}-{lumen_facts.max_interval}")
    bad_cadence = {**PRE_FIX,
                   "patterns": "The timeline shows a roughly monthly cadence (30-45 days)."}
    check("E: the pre-fix '30-45 days' claim is rejected",
          bool(_val.check_interval_claims(bad_cadence, lumen_facts, bad_cadence["patterns"])),
          _val.check_interval_claims(bad_cadence, lumen_facts, bad_cadence["patterns"]))
    for label, prose, which in [
        ("a range that matches the real intervals",
         "The timeline shows a cadence of 21 to 36 days between consecutive signals.",
         lumen_facts),
        ("the same interval stated in weeks",
         "Signals arrive roughly every 3 weeks, consistent with a 21-day interval.",
         lumen_facts),
        ("a month restatement of a 28-day rhythm",
         month_restatement["patterns"], brightline_facts),
    ]:
        check(f"E: {label} is accepted",
              not _val.check_interval_claims({**PRE_FIX, "patterns": prose},
                                             which, prose,
                                             evidence=brightline_tl
                                             if which is brightline_facts
                                             else _lumen_evidence),
              _val.check_interval_claims({**PRE_FIX, "patterns": prose}, which, prose))
    check("E: a fabricated '2 month' cadence is rejected for a 21-36 day timeline",
          bool(_val.check_interval_claims(
              {**PRE_FIX, "patterns": "They move on a 2 month cadence."},
              lumen_facts, "They move on a 2 month cadence.")),
          "a 60-day claim for a 21-36 day timeline passed")
    check("E: the counter that broke interval checking is gone",
          "allowed_numbers()" not in Path("backend/validators.py").read_text(),
          "check_interval_claims is consulting the loose number set again")

    # The pre-fix Brightline read: its own 28-day cadence put the next signal on
    # 2026-08-03, 56 days past, and the read announced it as a live plan.
    check("E: Brightline is 56 days overdue (age 84, median 28)",
          brightline_facts.days_overdue == 56, f"got {brightline_facts.days_overdue}")
    check("E: the pre-fix past deadline 2026-08-03 is rejected",
          any("2026-08-03" in p for p in
              _val.check_predicted_date_future(past_deadline, brightline_facts,
                                               past_deadline["predicted_next_move"])),
          _val.check_predicted_date_future(past_deadline, brightline_facts,
                                           past_deadline["predicted_next_move"]))
    check("E: a future deadline is accepted",
          not _val.check_predicted_date_future(
              {**PRE_FIX,
               "predicted_next_move": "They announce a messaging shift by 2026-10-15."},
              brightline_facts, "They announce a messaging shift by 2026-10-15."),
          "a correct future date was rejected")
    check("E: a real signal date cited as evidence in the prediction is not flagged",
          not _val.check_predicted_date_future(
              real_citation, brightline_facts, real_citation["predicted_next_move"]),
          "citing a real signal date was mistaken for an invented deadline")
    check("E: a past date as narrative context, with no deadline cue, is not flagged",
          not _val.check_predicted_date_future(
              narrative_past_date, brightline_facts,
              narrative_past_date["predicted_next_move"]),
          "a narrative reference was mistaken for a deadline")
    check("E: past dates in the evidence fields are never flagged",
          not _val.check_predicted_date_future(
              {**PRE_FIX, "patterns": "They cut pricing on 2026-04-15 and rebranded "
                                      "on 2026-05-20."},
              nimbus_facts, PRE_FIX["predicted_next_move"]),
          "an evidence citation in patterns was flagged")

    # -- end-to-end: the fixtures above are the ones the sweep failed on -----
    check("A/B/D/E: the pre-fix Nimbus response is rejected overall",
          bool(_val.validate_response(PRE_FIX, nimbus_facts, nimbus_tl)),
          "the pre-fix response now passes every validator")
    check("A/B/D/E: the corrected Nimbus response passes",
          _val.validate_response(acknowledged, nimbus_facts, nimbus_tl) == [],
          _val.validate_response(acknowledged, nimbus_facts, nimbus_tl))
    check("A: the corrected response no longer claims a 3x cycle, so it also passes E",
          not _val.check_interval_claims(acknowledged, nimbus_facts,
                                        *acknowledged.values()) ,
          _val.check_interval_claims(acknowledged, nimbus_facts, *acknowledged.values()))

    # -- the header bug ------------------------------------------------------
    lumen = _seed["Lumen Health"]
    check("header: the window string is the date span only",
          _tw(lumen) == "2026-01-14 to 2026-08-11", f"got {_tw(lumen)!r}")
    check("header: a single-signal window is that one date, with no count",
          _tw(lumen[:1]) == lumen[0].date, f"got {_tw(lumen[:1])!r}")
    check("header: an empty window says so",
          _tw([]) == "no signals recorded", f"got {_tw([])!r}")
    app_src = Path("frontend/app.py").read_text()
    # The caption is assembled across two source lines, so read the statement
    # as the module sees it rather than a 200-character window of source.
    caption_stmt = re.search(
        r'meta_col1\.caption\((.*?)\)\n', app_src, re.S).group(1)
    check("header: the count appears once, in the UI's own sentence",
          caption_stmt.count("signal_count") == 1
          and "read['timeline_window']" in caption_stmt
          and "signals)" not in caption_stmt,
          f"caption statement: {' '.join(caption_stmt.split())}")

    # -- calibration plumbing end to end ------------------------------------
    check("confidence: every level has a distinct badge in the UI",
          all(f'"{level}":' in app_src for level in ("high", "medium", "low", "none"))
          and "confidence_badge" in app_src,
          "a confidence level has no badge")
    check("confidence: the UI names what would raise it",
          "What would raise confidence" in app_src, "missing-evidence UI absent")
    check("confidence: the UI surfaces days overdue",
          "Overdue by" in app_src and "days_overdue" in app_src,
          "overdue UI absent")
    check("confidence: an unknown label degrades to low, never to none",
          _coerce_confidence("quite high") is _Conf.high
          and _coerce_confidence("banana") is _Conf.low
          and _coerce_confidence(None) is _Conf.low,
          "coercion is wrong")
    check("confidence: none is reserved for an actual refusal",
          _coerce_confidence("none") is _Conf.none
          and all(generate_strategic_read(n, today=_TODAY).confidence is _Conf.none
                  for n in ("Vertex Cloud", "Pathfinder Labs", "Tidewater Analytics")),
          "a refusal or the coercion of 'none' is wrong")
    check("confidence: a rejected response is withheld, not shown as a forecast",
          _coerce_confidence("high") is _Conf.high
          and "rejected by validators" in _unvalidated_response(
              "Nimbus AI", _seed["Nimbus AI"],
              evidence_clock(_seed["Nimbus AI"], today=_TODAY), nimbus_facts,
              "test").model_used,
          "the withheld-response path does not identify itself")

    # -- defect 1: the correction notice contradicted the validator ----------
    # A live run rejected a valid refusal with "a refusal must report confidence
    # 'none'", then told the model to report one of high/medium/low, and rejected
    # the retry again. Halcyon and Pathfinder both lost a read to it. The
    # contract now lives in one function; these checks make the two ends of it
    # unable to drift apart again.
    _refusal_notice = _correction_notice(
        ["a refusal must report confidence 'none'"],
        build_facts(_seed["Vertex Cloud"], today=_TODAY))
    _forecast_notice = _correction_notice(
        ["interval claim '14 days' does not correspond to any measured interval"],
        nimbus_facts)

    def _notice_contract(notice: str) -> tuple[set[str], str | None]:
        """What the notice tells the model to write, read the way the validator would.

        Returns (values for a forecast, value for a refusal). The notice explains
        both branches, because a rejected response may be either, and that is
        correct — the defect was not mentioning both, it was instructing a
        refusal to use the forecast branch's values.
        """
        _graded = re.search(
            r'"confidence" must be one of ([a-z/]+) if you make a forecast', notice)
        _refusal = re.search(
            r'decline to forecast.*?"confidence" must be [\'"]?(\w+)', notice, re.S)
        return (set(re.findall(r"high|medium|low", _graded.group(1))) if _graded
                else set(),
                _refusal.group(1) if _refusal else None)

    _graded_vals, _refusal_val = _notice_contract(_refusal_notice)
    check("notice: the refusal branch names the value the validator demands",
          _refusal_val == "none"
          and _val.allowed_confidence_values(refused=True) == ("none",),
          f"the notice tells a refusal to report {_refusal_val!r}, while the "
          f"validator accepts only {_val.allowed_confidence_values(refused=True)}")
    check("notice: the forecast branch names exactly the graded values",
          _graded_vals == set(_val.allowed_confidence_values(refused=False))
          == {"high", "medium", "low"},
          f"the notice offers {sorted(_graded_vals)} for a forecast, while the "
          f"validator accepts only {sorted(_val.allowed_confidence_values(False))}")
    check("notice: neither branch is stated unconditionally",
          re.search(r'"confidence" must be one of (?:high|medium|low)'
                    r'(?![a-z/])(?! if you make a forecast)',
                    _refusal_notice) is None,
          "the notice states a confidence requirement without its condition")
    # The two notices the model can receive must not disagree, whichever
    # complaint triggered them. Before the fix they did.
    check("notice: the contract does not depend on which complaint triggered it",
          _notice_contract(_refusal_notice) == _notice_contract(_forecast_notice),
          "the confidence contract changes with the problem being reported")
    # And the end-to-end claim: each branch of the notice, followed exactly,
    # produces a response the validator accepts for that branch. Before the fix
    # the refusal branch produced a rejection, then a second rejection. The
    # forecast branch is exercised on `acknowledged`, the response that already
    # passes every other rule, so only the confidence is under test.
    _refusal_read = {**PRE_FIX, "confidence": _refusal_val or "low",
                     "missing_evidence": "A named retention deal would settle it."}
    check("notice: a refusal written to the notice passes the validator",
          not _val.validate_response(_refusal_read,
                                     build_facts(_seed["Vertex Cloud"], today=_TODAY),
                                     _timeline["Vertex Cloud"], refused=True),
          "the notice still produces a response the validator rejects")
    # And the end-to-end claim: every confidence value the notice names must be
    # one the validator accepts for a response that is otherwise clean, and
    # every value the validator accepts must be one the notice names. Before the
    # fix a refusal was the second half of this with nothing to name: the notice
    # offered high/medium/low, the validator wanted "none", so the retry failed
    # the same way the first attempt did.
    _acceptable = {level for level in ("high", "medium", "low")
                   if not [p for p in _val.validate_response(
                       {**acknowledged, "confidence": level}, nimbus_facts, nimbus_tl)
                       if "confidence" in p]}
    check("notice: every graded level the validator would accept is one it names",
          _acceptable and _acceptable <= _graded_vals,
          f"the validator accepts {sorted(_acceptable)} but the notice names "
          f"{sorted(_graded_vals)}")
    check("notice: a forecast written to the notice passes the validator",
          all(not _val.validate_response({**acknowledged, "confidence": level},
                                         nimbus_facts, nimbus_tl)
              for level in _acceptable),
          "the notice's forecast branch is not satisfiable for any level it offers")
    check("notice: the overdue cap is still stated, so 'high' is not offered blindly",
          (nimbus_facts.days_overdue <= 0)
          or ('must not be "high"' in _forecast_notice),
          "the notice omits the overdue confidence cap")
    check("notice: a graded confidence is still rejected for a refusal",
          any("refusal must report confidence" in p
              for p in _val.validate_response({**_refusal_read, "confidence": "low"},
                                              build_facts(_seed["Vertex Cloud"],
                                                          today=_TODAY),
                                              _timeline["Vertex Cloud"], refused=True)),
          "the refusal rule was dropped rather than the notice being fixed")

    # -- defect 2: retries must converge -------------------------------------
    # Lumen's retry fixed the overdue complaint and wrote a different wrong
    # interval (7 days after 14 was rejected); Halcyon's retry fixed the
    # confidence complaint and wrote 30 days. The notice now names the numbers
    # that are permitted, so the second attempt has a closed set to choose from.
    _lumen_facts = build_facts(_seed["Lumen Health"], today=_TODAY)
    # The notice the model gets for this competitor, not the Nimbus one: the
    # numbers are per-competitor, so the list has to be built from these facts.
    _lumen_notice = _correction_notice(["interval claim '7 days' does not correspond"
                                        " to any measured interval"], _lumen_facts)
    _permitted = re.search(r"numbers of days: \[([\d,\s]+)\]", _lumen_notice)
    check("notice: it lists exactly the permitted interval numbers",
          _permitted is not None
          and {int(n) for n in _permitted.group(1).split(",") if n.strip()}
          == _lumen_facts.day_interval_claims(),
          f"the notice offers "
          f"{sorted(int(n) for n in _permitted.group(1).split(',') if n.strip()) if _permitted else 'no list'}"
          f" where the validator permits only {sorted(_lumen_facts.day_interval_claims())}")
    check("notice: it says the list is the whole permitted set",
          re.search(r"must be one of these", _lumen_notice)
          and "state no interval figure at all" in _lumen_notice,
          "the notice does not close the set of permitted figures")
    # Every number the notice permits must actually pass. If the notice offered
    # a figure the validator rejects, following the notice would lose a read.
    _permitted_ok = all(
        not _val.check_interval_claims(
            {**PRE_FIX, "patterns": f"The stream runs on a {n} day cadence."},
            _lumen_facts, f"The stream runs on a {n} day cadence.",
            evidence=_timeline["Lumen Health"])
        for n in sorted(_lumen_facts.day_interval_claims()))
    check("notice: every permitted number passes the interval check",
          _permitted_ok,
          "the notice permits a figure the validator rejects")
    _withheld = _unvalidated_response(
        "Lumen Health", _seed["Lumen Health"],
        evidence_clock(_seed["Lumen Health"], today=_TODAY), _lumen_facts, "test")
    check("notice: a failed retry yields a 200 read that says it was withheld",
          _withheld.narrative_withheld is True
          and _withheld.confidence is _Conf.none
          and "rejected by validators" in _withheld.model_used
          and "Nimbus" not in _withheld.model_used,
          "the withheld response does not identify itself as withheld")
    check("notice: the UI says when the narrative was withheld",
          "narrative_withheld" in app_src and "Narrative withheld" in app_src,
          "the withheld narrative is not surfaced in the UI")

    # -- defect 3: spelled-out durations -------------------------------------
    # _INTERVAL was digits-only, so "2 weeks" was checked and "two weeks" was
    # not. The same claim got a different verdict by spelling, which is the whole
    # problem: the rule cannot depend on the number format.
    _word_vs_digit = []
    for _digits, _words, _comp in (
            ("14 day", "two week", "Brightline Retail"),
            ("30 day", "four week", "Halcyon Mobility"),
            ("7 day", "one week", "Lumen Health"),
            ("a fortnight", "a fortnight", "Corvus Data"),
    ):
        _f = build_facts(_seed[_comp], today=_TODAY)
        _ev = _timeline[_comp]
        _d = f"The stream runs on a {_digits} cadence."
        _w = f"The stream runs on a {_words} cadence."
        _word_vs_digit.append(
            bool(_val.check_interval_claims({}, _f, _d, evidence=_ev))
            == bool(_val.check_interval_claims({}, _f, _w, evidence=_ev)))
    check("durations: a spelled-out amount is judged exactly as the digits are",
          all(_word_vs_digit),
          f"the two spellings diverged in {len(_word_vs_digit) - sum(_word_vs_digit)}"
          " of 4 fixtures")
    check("durations: 'two weeks' and 'a fortnight' are both caught as 14 days",
          bool(_val.check_interval_claims(
              {}, build_facts(_seed["Lumen Health"], today=_TODAY),
              "Lumen Health runs on a fortnight between signals.",
              evidence=_timeline["Lumen Health"]))
          and bool(_val.check_interval_claims(
              {}, build_facts(_seed["Lumen Health"], today=_TODAY),
              "Lumen Health runs on a two week cadence.",
              evidence=_timeline["Lumen Health"])),
          "a word-form interval escaped the check")
    check("durations: 'a month' is measured, and its article is not a quantity",
          # "a month" against a 28-day stream: the same verdict as "1 month".
          not _val.check_interval_claims(
              {}, build_facts(_seed["Brightline Retail"], today=_TODAY),
              "Brightline Retail's rhythm is about a month between signals.",
              evidence=_timeline["Brightline Retail"])
          # and the article next to a real figure is not read as "1 day", which
          # is what rejected "a 28 day cadence" before the article was handled.
          and not _val.check_interval_claims(
              {}, build_facts(_seed["Brightline Retail"], today=_TODAY),
              "Brightline Retail runs on a 28 day cadence.",
              evidence=_timeline["Brightline Retail"])
          # "a month" against a 38-day stream is still a measured claim, and the
          # cadence cue makes it one: 30 is not a Halcyon interval.
          and _val.check_interval_claims(
              {}, build_facts(_seed["Halcyon Mobility"], today=_TODAY),
              "Halcyon Mobility's rhythm is about a month between signals.",
              evidence=_timeline["Halcyon Mobility"]),
          "'a month' or the article handling is wrong")
    check("durations: a forecast horizon is not a cadence claim",
          not any(
              _val.check_interval_claims(
                  {}, build_facts(_seed["Halcyon Mobility"], today=_TODAY), _prose,
                  evidence=_timeline["Halcyon Mobility"])
              for _prose in ("We expect a launch within a month.",
                             "Expect news within a week.",
                             "They may ship something in a fortnight.")),
          "a bare indefinite duration is being read as a measured cadence")
    check("durations: prose numbers that are not durations are not collected",
          not _val._INTERVAL.findall("one observed instance, three roles, "
                                     "the second phase, a couple of hires, "
                                     "two rivals, five years of silence"),
          "a non-duration number matched the duration pattern")

    # -- the 429 fix, with no network ---------------------------------------

    class _Response:
        def __init__(self, status_code, text="", headers=None):
            self.status_code, self.text = status_code, text
            self.headers = headers or {}

    check("429: the server's own wait hint is honoured",
          _retry_after_seconds(_Response(429, headers={"retry-after": "35"})) == 35.0,
          "the Retry-After header was not parsed")
    check("429: Groq's in-body 'try again in Ns' hint is honoured",
          _retry_after_seconds(
              _Response(429, "Rate limit reached. Please try again in 34.98s.")) == 34.98,
          "the in-body hint was not parsed")
    check("429: no hint means no invented wait",
          _retry_after_seconds(_Response(429, "no hint here")) is None,
          "a wait was invented")
    check("429: a non-429 is not treated as rate limiting",
          _retry_after_seconds(_Response(500, "boom")) is None, "misclassified")
    check("429: rate limiting is a distinct, catchable LLMError",
          issubclass(RateLimitError, LLMError)
          and RateLimitError("x", retry_after=12.0).retry_after == 12.0,
          "RateLimitError is not a usable LLMError")
    client_src = Path("backend/llm_client.py").read_text()
    check("429: the retry budget waits for a server hint, not a fixed instant",
          "time.sleep(wait)" in client_src
          and RATE_LIMIT_DEFAULT_WAIT >= 10
          and RATE_LIMIT_MAX_WAIT > RATE_LIMIT_DEFAULT_WAIT,
          "the backoff is not actually waiting")

    # -- the FACTS block is the only source of the numbers ------------------
    check("FACTS: the prompt carries the block and forbids recomputing it",
          "FACTS (computed by the application" in nimbus_prompt
          and "do not compute" in nimbus_prompt.lower(),
          "the block or its rule is absent")
    check("FACTS: the prompt asks for both new fields",
          '"confidence"' in nimbus_prompt and '"missing_evidence"' in nimbus_prompt,
          "the field list was not updated")
    check("FACTS: every competitor produces a block",
          all(build_facts(s, today=_TODAY).render().startswith("FACTS")
              for s in _seed.values()),
          "a competitor produced no FACTS block")
    check("FACTS: a same-day pair is excluded, not counted as a 0-day rhythm",
          build_facts([*_seed["Vertex Cloud"],
                        Signal(**{**_seed["Vertex Cloud"][-1].model_dump(),
                                 "date": _seed["Vertex Cloud"][-1].date})],
                       today=_TODAY).intervals == _facts.intervals(_seed["Vertex Cloud"]),
          "a duplicate same-day signal changed the interval list")
    check("FACTS: overdue is never negative",
          [_facts.days_overdue(age, 14) for age in (0, 10, 14, 40)] == [0, 0, 0, 26],
          "overdue arithmetic is wrong")
    check("FACTS: every adjacent pair is counted exactly once",
          sum(nimbus_facts.transitions.values()) == len(_seed["Nimbus AI"]) - 1,
          f"transition total {sum(nimbus_facts.transitions.values())} vs "
          f"{len(_seed['Nimbus AI']) - 1} pairs")
    check("FACTS: signal_dates covers the real timeline, for the deadline check",
          set(nimbus_facts.signal_dates) == {s.date for s in _seed["Nimbus AI"]},
          "signal_dates does not match the timeline")

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # 10. Mutations. Every rule above is re-run against a copy of the source
    #     with that rule deleted. A check that fires both with its guard
    #     present and with it removed is not a check, and that is exactly how a
    #     grounding rule rots: someone trims a prompt line or a validator's
    #     condition and the suite stays green.
    #
    #     The edit is applied to the module's source and the copy is executed
    #     from that text, so the thing being removed is the rule in the file.
    #     Every replacement and every removal must match, or the mutation says
    #     so instead of quietly proving nothing.
    # ------------------------------------------------------------------
    print("\n\033[1m10. Mutation tests (each rule must be load-bearing)\033[0m")
    import importlib
    import types

    from backend import facts as _facts
    from backend import llm_client as _llm
    from backend import synthesis as _syn
    from backend import validators as _val

    _runs = [0]

    def _mutant_from(module, replacements=(), removals=(), subs=()):
        original = importlib.import_module(module)
        source = Path(original.__file__).read_text()
        for old, new in replacements:
            if old not in source:
                raise AssertionError(
                    f"replacement target absent from {module}: {old[:70]!r}")
            source = source.replace(old, new, 1)
        for pattern, new in subs:
            source, hits = re.subn(pattern, new, source, flags=re.S)
            if hits != 1:
                raise AssertionError(
                    f"substitution matched {hits} times in {module}: {pattern[:70]!r}")
        for pattern in removals:
            source, hits = re.subn(pattern, "", source, flags=re.S)
            if hits != 1:
                raise AssertionError(
                    f"removal matched {hits} times in {module}: {pattern[:70]!r}")
        _runs[0] += 1
        clone = types.ModuleType(
            f"backend._mutant_{original.__name__.rsplit('.', 1)[-1]}_{_runs[0]}")
        clone.__file__ = original.__file__
        clone.__package__ = "backend"
        # @dataclass resolves annotations through sys.modules[cls.__module__],
        # so the copy has to be registered before it executes.
        sys.modules[clone.__name__] = clone
        exec(compile(source, original.__file__, "exec"), clone.__dict__)
        return clone

    def _stack(validators=None, synthesis=None, facts_mod=None, llm=None):
        """The modules under test, plus a builder for the facts object.

        The facts are built through whichever module is in the stack, so a
        mutation inside facts.py actually reaches the code that reads them
        rather than sitting inert next to an already-constructed instance.
        """
        stack = {"val": validators or _val, "syn": synthesis or _syn,
                 "facts": facts_mod or _facts, "llm": llm or _llm}
        stack["F"] = lambda sigs: stack["facts"].build_facts(sigs, today=_TODAY)
        return stack

    def _mutates(label, build, fires):
        """`fires` must detect the defect on the real code and miss it on the mutant."""
        intact = fires(_stack())
        try:
            mutant = build()
        except AssertionError as exc:
            check(f"{label}: the mutation applied cleanly", False, str(exc))
            return
        mutated = fires(mutant)
        check(f"{label}: fires on the real code, silent once the rule is deleted",
              intact is True and mutated is False,
              f"intact={intact} mutated={mutated}")

    # -- A: the repetition rule, deleted from the prompt --------------------
    _mutates(
        "A: deleting the REPETITION block from the prompt",
        lambda: _stack(synthesis=_mutant_from(
            "backend.synthesis",
            removals=[r"REPETITION \(the FACTS block counts every transition for you\):"
                      r".*?\n\n"])),
        lambda m: "never count your own prediction" in
        m["syn"].build_prompt("Nimbus AI", _seed["Nimbus AI"], today=_TODAY),
    )

    # -- C: the consistency rule, deleted from the prompt -------------------
    _mutates(
        "C: deleting the consistency-with-the-latest-signals block",
        lambda: _stack(synthesis=_mutant_from(
            "backend.synthesis",
            removals=[r"CONSISTENCY WITH THE LATEST SIGNALS:.*?\n\n"])),
        lambda m: "silently skip a stage" in
        m["syn"].build_prompt("Nimbus AI", _seed["Nimbus AI"], today=_TODAY),
    )

    # -- the FACTS block and the overdue clause -----------------------------
    _mutates(
        "FACTS: blanking the block that carries the numbers",
        lambda: _stack(synthesis=_mutant_from(
            "backend.synthesis",
            replacements=[("facts_block=facts.render() + facts.overdue_instruction(),",
                           "facts_block='FACTS: removed by mutation',")])),
        lambda m: "FACTS (computed by the application" in
        m["syn"].build_prompt("Nimbus AI", _seed["Nimbus AI"], today=_TODAY),
    )
    _mutates(
        "FACTS: making the overdue clause disappear from the prompt",
        lambda: _stack(facts_mod=_mutant_from(
            "backend.facts",
            replacements=[('        if self.days_overdue <= 0:\n            return ""',
                           '        return ""')])),
        lambda m: 'MUST NOT be "high"' in
        m["F"](_seed["Nimbus AI"]).overdue_instruction(),
    )
    _mutates(
        "FACTS: making the transition counts unreadable, so repetition cannot be priced",
        lambda: _stack(facts_mod=_mutant_from(
            "backend.facts",
            replacements=[("if v >= 2}", "if v >= 99}"),
                          ("if v == 1}", "if v == 99}")])),
        lambda m: "transitions that DO repeat" in m["F"](_seed["Nimbus AI"]).render()
        and "seen only ONCE" in m["F"](_seed["Nimbus AI"]).render(),
    )

    # -- B: each half of the overdue rule -----------------------------------
    _mutates(
        "B: removing the 'must acknowledge the gap' rejection",
        lambda: _stack(validators=_mutant_from(
            "backend.validators", replacements=[("    if not mentions_gap:",
                                                 "    if False:")])),
        lambda m: any("acknowledge the gap" in p for p in
                      m["val"].check_overdue_acknowledged(
                          b_only, m["F"](_seed["Nimbus AI"]),
                          b_only["predicted_next_move"])),
    )
    _mutates(
        "B: removing the 'confidence must not be high' cap",
        lambda: _stack(validators=_mutant_from(
            "backend.validators", replacements=[('    if confidence == "high":',
                                                 "    if False:")])),
        lambda m: any("confidence is 'high'" in p for p in
                      m["val"].check_overdue_acknowledged(
                          PRE_FIX, m["F"](_seed["Nimbus AI"]),
                          PRE_FIX["predicted_next_move"])),
    )

    # -- D: the calibration rules -------------------------------------------
    _mutates(
        "D: removing the 'confidence is absent' rejection",
        lambda: _stack(validators=_mutant_from(
            "backend.validators",
            replacements=[('        problems.append("confidence is absent")',
                           "        pass")])),
        lambda m: any("confidence is absent" in p for p in
                      m["val"].check_confidence_present(d_only, m["F"](_seed["Nimbus AI"]))),
    )
    _mutates(
        "D: removing the 'missing_evidence is absent' rejection",
        lambda: _stack(validators=_mutant_from(
            "backend.validators",
            replacements=[('        problems.append("missing_evidence is absent")',
                           "        pass")])),
        lambda m: any("missing_evidence is absent" in p for p in
                      m["val"].check_confidence_present(m_only, m["F"](_seed["Nimbus AI"]))),
    )
    _mutates(
        "D: removing the refusal-confidence rule",
        lambda: _stack(validators=_mutant_from(
            "backend.validators",
            replacements=[('    if str(response.get("confidence") or "")'
                           '.strip().lower() not in required:', "    if False:")])),
        lambda m: any("refusal must report confidence" in p for p in
                      m["val"].validate_response({**refusal, "confidence": "medium"},
                                                 m["F"](_seed["Nimbus AI"]),
                                                 nimbus_tl, refused=True)),
    )
    _mutates(
        "D: deciding refusal from the prose again",
        # Deliberately not `re.search`: the module no longer imports `re`, and a
        # mutant that raises would prove nothing about the rule it replaced.
        lambda: _stack(synthesis=_mutant_from(
            "backend.synthesis",
            replacements=[('    return _coerce_confidence(parsed.get("confidence"))'
                           " is Confidence.none",
                           "    return 'insufficient' in (  # MUTANT: prose again\n"
                           "        str(parsed.get('patterns', '')).lower()\n"
                           "        + str(parsed.get('predicted_next_move', '')).lower())")])),
        lambda m: not m["syn"].is_refusal(
            {**PRE_FIX,
             "patterns": "The 2026-04-15 cut was insufficient to move enterprise "
                         "buyers, so they re-priced in May.",
             "confidence": "low"}),
    )
    _mutates(
        "D: letting the coercer hand out 'none' by substring",
        lambda: _stack(synthesis=_mutant_from(
            "backend.synthesis",
            replacements=[("        if candidate is Confidence.none:\n            continue\n", "")])),
        lambda m: m["syn"]._coerce_confidence(
            "None of these observations support a cadence") is _Conf.low,
    )
    _mutates(
        "D: removing the quote check",
        lambda: _stack(validators=_mutant_from(
            "backend.validators",
            replacements=[("    for field in _NARRATIVE_FIELDS:",
                           "    for field in ():")])),
        lambda m: bool(m["val"].check_quotes_verbatim(
            invented, m["F"](_seed["Brightline Retail"]), brightline_tl)),
    )
    _mutates(
        "D: narrowing the quote check back to the two evidence fields",
        lambda: _stack(validators=_mutant_from(
            "backend.validators",
            replacements=[('_NARRATIVE_FIELDS = ("patterns", "inferred_intent", '
                           '"predicted_next_move",\n                     "recommendation")',
                           '_NARRATIVE_FIELDS = ("patterns", "inferred_intent")')])),
        lambda m: bool(m["val"].check_quotes_verbatim(
            invented_in_recommendation, m["F"](_seed["Lumen Health"]), _lumen_evidence)),
    )

    # -- E: the interval rules ---------------------------------------------
    _mutates(
        "E: restoring the loose number set that licensed any '30 days' claim",
        lambda: _stack(validators=_mutant_from(
            "backend.validators",
            replacements=[("    exact = facts.day_interval_claims()",
                           "    exact = facts.allowed_numbers()")])),
        lambda m: bool(m["val"].check_interval_claims(
            fabricated_60, m["F"](_seed["Lumen Health"]),
            fabricated_60["patterns"], evidence=_lumen_evidence)),
    )
    _mutates(
        "E: holding month claims to the exact figure, with no unit tolerance",
        lambda: _stack(validators=_mutant_from(
            "backend.validators",
            replacements=[("days, tolerance = {int(amount * 30), int(amount * 31)}, 4",
                           "days, tolerance = {int(amount * 30), int(amount * 31)}, 0")])),
        lambda m: not m["val"].check_interval_claims(
            month_restatement, m["F"](_seed["Brightline Retail"]),
            month_restatement["patterns"], evidence=brightline_tl),
    )
    _mutates(
        "E: removing the 'cited from the evidence' exemption",
        lambda: _stack(validators=_mutant_from(
            "backend.validators",
            replacements=[("                if cited and cited.intersection(days):",
                           "                if False:")])),
        lambda m: not m["val"].check_interval_claims(
            cited, m["F"](_cite_signals), cited["patterns"], evidence=cite_evidence),
    )
    _mutates(
        "E: exempting a cadence on any matching digit anywhere in the evidence",
        lambda: _stack(validators=_mutant_from(
            "backend.validators",
            replacements=[("                if cited and cited.intersection(days):\n"
                           "                    continue  # quoted from the evidence, not "
                           "a cadence claim",
                           "                if any(f\"{int(candidate)}\" in evidence_text\n"
                           "                       for candidate in days):\n"
                           "                    continue  # MUTANT: substring exemption")],
            subs=[(r"    cited = _stated_durations\(evidence\)\n",
                   "    cited = _stated_durations(evidence)\n"
                   "    evidence_text = _norm(evidence).lower()\n")])),
        lambda m: bool(m["val"].check_interval_claims(
            {**PRE_FIX, "patterns": "Brightline Retail runs on a 10 day cadence."},
            m["F"](_seed["Brightline Retail"]),
            "Brightline Retail runs on a 10 day cadence.",
            evidence=brightline_tl)),
    )
    _mutates(
        "E: putting day and week figures back in one bag",
        lambda: _stack(validators=_mutant_from(
            "backend.validators",
            replacements=[("    exact = facts.day_interval_claims()",
                           "    exact = facts.allowed_interval_claims()")])),
        lambda m: bool(m["val"].check_interval_claims(
            {**PRE_FIX, "patterns": "Brightline Retail runs on a 10 day cadence."},
            m["F"](_seed["Brightline Retail"]),
            "Brightline Retail runs on a 10 day cadence.",
            evidence=brightline_tl)),
    )
    _mutates(
        "E: disconnecting the interval check from the measured FACTS",
        lambda: _stack(facts_mod=_mutant_from(
            "backend.facts",
            subs=[(r"    def day_interval_claims\(self\) -> set\[int\]:.*?"
                   r"        return out\n",
                   "    def day_interval_claims(self) -> set[int]:\n"
                   "        return set()\n")])),
        lambda m: not m["val"].check_interval_claims(
            true_cadence, m["F"](_seed["Lumen Health"]), true_cadence["patterns"],
            evidence=_lumen_evidence),
    )
    _mutates(
        "E: rejecting the day unit instead of tolerating an approximation",
        lambda: _stack(validators=_mutant_from(
            "backend.validators",
            replacements=[("                    days, tolerance = {int(amount)}, 2",
                           "                    days, tolerance = {int(amount)}, 0")])),
        lambda m: not m["val"].check_interval_claims(
            {**PRE_FIX, "patterns": "Halcyon moves about every 35 days."},
            m["F"](_seed["Halcyon Mobility"]),
            "Halcyon moves about every 35 days.",
            evidence=_timeline["Halcyon Mobility"]),
    )
    # The three defects the live sweep found. Each mutation re-introduces the
    # original defect exactly, so the checks above cannot pass with the fix
    # reverted — which is the only thing that makes them worth having.
    _mutates(
        "defect 1: letting a refusal report high/medium/low again",
        lambda: _stack(validators=_mutant_from(
            "backend.validators",
            replacements=[('    return ("none",) if refused else ("high", "medium", "low")',
                           '    return ("high", "medium", "low")  # MUTANT')])),
        lambda m: not m["val"].check_refusal_calibrated(
            {**PRE_FIX, "confidence": "none", "missing_evidence": "A deal settles it."},
            m["F"](_seed["Vertex Cloud"])),
    )
    _mutates(
        "defect 1: the notice asking the wrong end of the contract",
        lambda: _stack(synthesis=_mutant_from(
            "backend.synthesis",
            replacements=[('    forecast = "/".join(validators.allowed_confidence_values('
                           "refused=False))",
                           '    forecast = "/".join(validators.allowed_confidence_values('
                           "refused=True))  # MUTANT: wrong end")])),
        lambda m: all(v in
                      m["syn"]._correction_notice(["x"], m["F"](_seed["Nimbus AI"]))
                      for v in ("high", "medium", "low")),
    )
    _mutates(
        "defect 2: dropping the permitted-number list from the notice",
        lambda: _stack(synthesis=_mutant_from(
            "backend.synthesis",
            replacements=[("        permitted = sorted(facts.day_interval_claims())\n"
                           "        if permitted:",
                           "        permitted = []\n        if False:")])),
        lambda m: bool(re.search(r"numbers of days: \[[\d,\s]+\]",
                                m["syn"]._correction_notice(
                                    ["interval claim"], m["F"](_seed["Lumen Health"])))),
    )
    _mutates(
        "defect 2: withholding the narrative without saying so",
        lambda: _stack(synthesis=_mutant_from(
            "backend.synthesis",
            replacements=[("        narrative_withheld=True,",
                           "        narrative_withheld=False,")])),
        lambda m: m["syn"]._unvalidated_response(
            "Lumen Health", _seed["Lumen Health"],
            evidence_clock(_seed["Lumen Health"], today=_TODAY),
            m["F"](_seed["Lumen Health"]), "test").narrative_withheld,
    )
    _mutates(
        "defect 3: restoring the digits-only interval pattern",
        lambda: _stack(validators=_mutant_from(
            "backend.validators",
            replacements=[('_NUM = (r"\\d+(?:\\.\\d+)?|"\n'
                           '        + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True)))',
                           '_NUM = r"\\d+(?:\\.\\d+)?"  # MUTANT: digits only')])),
        lambda m: bool(m["val"].check_interval_claims(
            {}, m["F"](_seed["Lumen Health"]),
            "Lumen Health runs on a fortnight between signals.",
            evidence=_timeline["Lumen Health"])),
    )
    _mutates(
        "defect 3: reading the indefinite article as the quantity",
        lambda: _stack(validators=_mutant_from(
            "backend.validators",
            replacements=[('                if value.strip().lower() in ("a", "an") '
                           "and match.group(2):\n                    continue",
                           "                if False:\n                    continue")])),
        lambda m: not m["val"].check_interval_claims(
            {}, m["F"](_seed["Brightline Retail"]),
            "Brightline Retail runs on a 28 day cadence.",
            evidence=_timeline["Brightline Retail"]),
    )
    _mutates(
        "defect 3: dropping the cadence cue, so horizons read as cadences",
        lambda: _stack(validators=_mutant_from(
            "backend.validators",
            replacements=[("                if not _CADENCE_CUE.search(around):\n"
                           "                    continue",
                           "                if False:\n                    continue")])),
        lambda m: not m["val"].check_interval_claims(
            {}, m["F"](_seed["Halcyon Mobility"]),
            "We expect a launch within a month.",
            evidence=_timeline["Halcyon Mobility"]),
    )
    check("E: the day tolerance is what accepts that approximation (35 is not a Halcyon interval)",
          35 not in build_facts(_seed["Halcyon Mobility"],
                                today=_TODAY).allowed_interval_claims()
          and not _val.check_interval_claims(
              {**PRE_FIX, "patterns": "Halcyon moves about every 35 days."},
              build_facts(_seed["Halcyon Mobility"], today=_TODAY),
              "Halcyon moves about every 35 days.",
              evidence=_timeline["Halcyon Mobility"]),
          "the tolerance fixture is not an approximation")
    check("E: the allowed set is already week-denominated, so a valid week "
          "restatement never needs the conversion branch",
          all(round(v / 7) in build_facts(sigs, today=_TODAY).allowed_interval_claims()
              for sigs in _seed.values()
              for v in build_facts(sigs, today=_TODAY).intervals),
          "a valid week figure is missing from the allowed set")

    # -- E: the deadline rules ---------------------------------------------
    _mutates(
        "E: removing signal_dates, so citing a real signal looks invented",
        lambda: _stack(facts_mod=_mutant_from(
            "backend.facts",
            replacements=[("        signal_dates=tuple(s.date for s in sigs),",
                           "        signal_dates=(),")])),
        lambda m: not m["val"].check_predicted_date_future(
            real_citation, m["F"](_seed["Brightline Retail"]),
            real_citation["predicted_next_move"]),
    )
    _mutates(
        "E: removing the deadline-cue requirement",
        lambda: _stack(validators=_mutant_from(
            "backend.validators",
            replacements=[('            if not re.search(r"\\b(by|before|on|due|expected|'
                           'deadline|target|around)\\s*$",\n                             lead):\n'
                           '                continue  # narrative reference, not a deadline',
                           "            if False:\n                continue")])),
        lambda m: not m["val"].check_predicted_date_future(
            narrative_past_date, m["F"](_seed["Brightline Retail"]),
            narrative_past_date["predicted_next_move"]),
    )
    _mutates(
        "E: removing the past-date rejection entirely",
        lambda: _stack(validators=_mutant_from(
            "backend.validators",
            replacements=[('            problems.append(\n'
                           '                f"predicted_next_move names {iso} as a future '
                           'target, but it is before "',
                           '            _ignored = (\n'
                           '                f"predicted_next_move names {iso} as a future '
                           'target, but it is before "')])),
        lambda m: bool(m["val"].check_predicted_date_future(
            past_deadline, m["F"](_seed["Brightline Retail"]),
            past_deadline["predicted_next_move"])),
    )
    _mutates(
        "E: inverting the past/future comparison",
        lambda: _stack(validators=_mutant_from(
            "backend.validators",
            replacements=[("            if parsed is None or parsed > facts.today:",
                           "            if parsed is None or parsed < facts.today:")])),
        lambda m: not m["val"].check_predicted_date_future(
            {**PRE_FIX, "predicted_next_move": "They ship admin analytics by 2026-10-12."},
            m["F"](_seed["Nimbus AI"]),
            "They ship admin analytics by 2026-10-12."),
    )

    # -- the 429 backoff ----------------------------------------------------
    _mutates(
        "429: ignoring the server's own Retry-After header",
        lambda: _stack(llm=_mutant_from(
            "backend.llm_client",
            replacements=[('    raw = headers.get("retry-after") or '
                           'headers.get("Retry-After")', "    raw = None")])),
        lambda m: m["llm"]._retry_after_seconds(
            _Response(429, headers={"retry-after": "35"})) == 35.0,
    )
    _mutates(
        "429: ignoring Groq's in-body wait hint",
        lambda: _stack(llm=_mutant_from(
            "backend.llm_client",
            removals=[r'    match = re\.search\(r"try again in.*?return None\n'])),
        lambda m: m["llm"]._retry_after_seconds(
            _Response(429, "Rate limit reached. Please try again in 34.98s.")) == 34.98,
    )
    _mutates(
        "429: shrinking the default wait back to the sub-second retry that never worked",
        lambda: _stack(llm=_mutant_from(
            "backend.llm_client",
            replacements=[("RATE_LIMIT_DEFAULT_WAIT = 20.0",
                           "RATE_LIMIT_DEFAULT_WAIT = 0.4")])),
        lambda m: m["llm"].RATE_LIMIT_DEFAULT_WAIT >= 10,
    )

    # A floor, not an exact count: the point is that a mutation which fails to
    # apply is reported above rather than silently counted, so a floor catches
    # a batch of them being dropped wholesale.
    check(f"all {_runs[0]} mutations applied and every rule proved load-bearing",
          _runs[0] >= 20, f"only {_runs[0]} mutations ran")

    # Tear the double down only once every section is done. It was shut down
    # before the last section once, which made those checks pass vacuously.
    httpd.shutdown()
    double.BANKS.clear()
    double.UNITS.clear()

    # Summarise last, so the tally covers every section.
    failed = [n for ok, n in _results if not ok]
    total = len(_results)


    print("\n" + "─" * 62)
    if failed:
        print(f"{FAIL}  {len(failed)}/{total} checks failed:")
        for n in failed:
            print(f"        - {n}")
        return 1
    print(f"\033[32m✓ all {total} checks passed\033[0m")
    return 0


def re_date(token: str) -> bool:
    import re

    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", token))


if __name__ == "__main__":
    raise SystemExit(main())
