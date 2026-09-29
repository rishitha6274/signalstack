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

import inspect
import json
import re
import subprocess
import sys
import threading
import time
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


_RECALL_USE = re.compile(
    r"\brecall_signals\s*\(|\.recall\s*\(|import\s+[^\n]*\brecall\b")


def synthesis_uses_recall(src: str) -> bool:
    """True if synthesis reaches for recall at run time.

    Prose about recall is allowed, and synthesis.py's own docstring explains
    why it must not call recall -- matching the bare word would make the rule
    unsatisfiable. What is forbidden is a call or an import.
    """
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.strip().startswith("#"))
    return bool(_RECALL_USE.search(code))


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

    print("\n\033[1m5b. The demo flip: one signal turns a refusal into a forecast\033[0m")
    # This is the UI's hero path, so it is pinned end to end: Vertex Cloud is
    # seeded with 4 signals, refuses, and the moment a 5th is logged it
    # forecasts. The demo depends on the flip landing on exactly that click, so
    # "it worked when I tried it" is not good enough.
    #
    # The floor in facts.py is what makes it flip, and it is bidirectional:
    # below the floor, *forecasting* may only report confidence "none" -- so a
    # forecast is not merely discouraged, it is unrepresentable. At or above the
    # floor, *refusing* is no longer allowed at all. The refusal and the
    # forecast each become the only permitted answer at their own side.
    import statistics
    from backend.facts import (MAX_INTERVAL_SPREAD as _MAX_SPREAD,
                                MIN_SIGNALS_FOR_EVIDENCE, build_facts)
    from backend.ingestion import load_seed_file
    from backend.models import Signal
    from backend.synthesis import generate_strategic_read, is_refusal as _is_refusal
    from backend.validators import allowed_confidence_values

    # load_seed_file() is already keyed by competitor name.
    _vertex = list(load_seed_file()["Vertex Cloud"])
    check("Vertex Cloud is seeded below the evidence floor (the demo's starting point)",
          len(_vertex) < MIN_SIGNALS_FOR_EVIDENCE,
          f"seeded with {len(_vertex)}, floor is {MIN_SIGNALS_FOR_EVIDENCE}")
    check("one more signal crosses the floor",
          len(_vertex) + 1 == MIN_SIGNALS_FOR_EVIDENCE,
          f"{len(_vertex)} + 1 != {MIN_SIGNALS_FOR_EVIDENCE}")

    def _vertex_signals(n: int) -> list[Signal]:
        # Already Signal objects; copied so the extra append cannot mutate the
        # module-level seed the later sections read.
        out = [s.model_copy() for s in _vertex]
        # The extra signal is a hire, the one signal type absent from the
        # seeded four, and it is dated late so the interval spread stays wide.
        # That is deliberate: it is what caps the result at medium instead of
        # letting the demo promise high confidence on five data points.
        if n > len(_vertex):
            out.append(Signal(
                competitor="Vertex Cloud", date="2026-09-14", signal_type="hiring",
                summary=("Vertex Cloud posted a compliance engineer opening, "
                         "its first security hire in the dataset."),
                source="selfcheck", raw_text="compliance engineer",
            ))
        return out[:n]

    _f4 = build_facts(_vertex_signals(4))
    _f5 = build_facts(_vertex_signals(5))
    check("below the floor: a forecast can only report confidence 'none'",
          allowed_confidence_values(_f4, refused=False) == ("none",),
          f"got {allowed_confidence_values(_f4, refused=False)}")
    check("below the floor: refusing is the calibrated option",
          allowed_confidence_values(_f4, refused=True) == ("none",),
          f"got {allowed_confidence_values(_f4, refused=True)}")
    check("at the floor: refusing is no longer permitted",
          allowed_confidence_values(_f5, refused=True) == (),
          f"got {allowed_confidence_values(_f5, refused=True)}")
    check("at the floor: confidence is capped below high",
          "high" not in allowed_confidence_values(_f5, refused=False),
          f"got {allowed_confidence_values(_f5, refused=False)}")

    # Why the cap is medium and not high. Not the dispersion rule: Vertex's
    # intervals are ~[76, 83, 43, 33], so the longest gap is well inside 3x the
    # median and dispersion PASSES. What holds confidence down is that the
    # floor has only just been cleared -- five signals is the minimum, and
    # nothing has repeated yet. Pinned here because the demo's closing line is
    # "now it forecasts, at medium", and if a future seed edit made this
    # genuinely well-evidenced the demo would quietly start promising "high"
    # and overclaim on the strength of five data points.
    from backend.facts import intervals as _intervals
    _v4 = _intervals(_vertex_signals(4))
    _v5 = _intervals(_vertex_signals(5))
    check("dispersion is not what limits this timeline (it passes at both counts)",
          max(_v5) <= _MAX_SPREAD * statistics.median(_v5),
          f"intervals {_v5}, median {statistics.median(_v5)}, allowance {_MAX_SPREAD}x")
    # `intervals()` returns the GAPS between signals, so a 5-signal timeline
    # yields 4 intervals. Counting signals means asking the facts, not len().
    check("the timeline is only just over the floor, which is what caps confidence",
          _f5.n == MIN_SIGNALS_FOR_EVIDENCE,
          f"facts n={_f5.n}, floor={MIN_SIGNALS_FOR_EVIDENCE}")
    check("the four seeded signals alone are below the floor",
          _f4.n == len(_vertex) < MIN_SIGNALS_FOR_EVIDENCE,
          f"facts n={_f4.n}, seed={len(_vertex)}, floor={MIN_SIGNALS_FOR_EVIDENCE}")
    check("no transition has repeated even after the fifth signal",
          not _f5.repeated_transitions(),
          f"repeated {list(_f5.repeated_transitions())}")
    check("and at n=4 nothing repeated either",
          not _f4.repeated_transitions(),
          f"repeated {list(_f4.repeated_transitions())}")

    # And the whole path against the live double: 4 refuses, log 1, 5 forecasts.
    def _refused(resp) -> bool:
        return _is_refusal({"patterns": resp.patterns,
                            "confidence": resp.confidence.value,
                            "predicted_next_move": resp.predicted_next_move})

    _before = generate_strategic_read("Vertex Cloud")
    check("demo start: Vertex refuses with confidence 'none'",
          _refused(_before) and _before.confidence.value == "none",
          f"refusal={_refused(_before)} confidence={_before.confidence.value}")
    check("demo start: the refusal names how many more signals are needed",
          "more signal" in _before.missing_evidence,
          f"missing_evidence={_before.missing_evidence[:90]!r}")

    _hired = _vertex_signals(5)[-1]
    client.write_signal(_hired)
    _after = generate_strategic_read("Vertex Cloud")
    check("after logging one signal: Vertex no longer refuses",
          not _refused(_after), "still refusing after the 5th signal")
    check("after logging one signal: it now forecasts at medium or lower",
          _after.confidence.value in ("medium", "low"),
          f"got {_after.confidence.value}")
    check("the flip is what the demo claims: refusal -> forecast, capped",
          _refused(_before) and not _refused(_after)
          and _after.confidence.value == "medium",
          f"{_before.confidence.value} -> {_after.confidence.value}")
    check("the forecast is dated, not vague",
          re.search(r"\d{4}-\d{2}-\d{2}", _after.predicted_next_move) is not None,
          f"got {_after.predicted_next_move[:100]!r}")

    # The double's refusal threshold must track the app's floor, or the flip
    # above verifies nothing: the app would permit a forecast the fake refuses.
    import hindsight_double as _dbl
    check("the double's refusal threshold IS the app's evidence floor",
          _dbl._MIN_SIGNALS_FOR_EVIDENCE == MIN_SIGNALS_FOR_EVIDENCE,
          f"double {_dbl._MIN_SIGNALS_FOR_EVIDENCE} vs app {MIN_SIGNALS_FOR_EVIDENCE}")

    # And the fake must survive a short timeline. It used to narrate Nimbus's
    # story with a hardcoded dates[5], raising IndexError -- a 500 the client
    # sees as a disconnect -- on any timeline with fewer than six signals,
    # which is exactly the demo's case.
    check("the double can forecast a 5-signal timeline without crashing",
          not _after.narrative_withheld
          and _after.model_used != "fallback (no LLM)",
          f"withheld={_after.narrative_withheld} model={_after.model_used}")
    check("the double's forecast reads off the actual timeline, not a fixed script",
          "2026-09-14" in _after.patterns,
          f"the new signal is missing from the narrative: {_after.patterns[:120]!r}")
    check("the double still narrates Nimbus's founding read specifically",
          "series c" in (rich.patterns + rich.inferred_intent).lower())

    # The UI's sample note has to be the one that produces the flip. If the
    # seeder or the floor moves, this catches the sample going stale before a
    # demo does.
    _app_src = (REPO_ROOT / "frontend" / "app.py").read_text()
    _SAMPLE_MARK = "SAMPLE_VERTEX_SIGNAL = ("
    check("the UI defines a sample signal for the demo path",
          _SAMPLE_MARK in _app_src, "SAMPLE_VERTEX_SIGNAL is missing")
    if _SAMPLE_MARK in _app_src:
        import re as _re
        # The note is the first double-quoted run after the assignment.
        _m = _re.search(r'"([^"]{40,})"',
                        _app_src.split(_SAMPLE_MARK, 1)[1][:2000])
        _note = _m.group(1) if _m else ""
        check("the sample note is present and non-trivial",
              len(_note) > 40, f"got {len(_note)} chars")
        _sig = extract_signal(_note, "Vertex Cloud") if _note else None
        check("the sample note extracts to a real signal",
              _sig is not None and bool(_sig.signal_type),
              "extraction returned nothing")
        if _sig is not None:
            check("the sample note is the type the flip needs (hiring)",
                  _sig.signal_type == "hiring",
                  f"got {_sig.signal_type}")
            check("the sample's date is after the last seeded Vertex signal",
                  _sig.date > max(s.date for s in _vertex),
                  f"sample {_sig.date} vs last seeded {max(s.date for s in _vertex)}")
            # And the decisive one: with this signal in place, the read flips.
            client.write_signal(_sig)
            _sample_read = generate_strategic_read("Vertex Cloud")
            check("the UI's sample signal flips Vertex to a forecast",
                  not _refused(_sample_read),
                  f"still {_sample_read.confidence.value}")
            check("and the flip is capped at medium or lower",
                  _sample_read.confidence.value in ("medium", "low"),
                  f"got {_sample_read.confidence.value}")

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
    # 8b. Optional write auth (API_KEY). Two modes, both load-bearing:
    # unset = writes are open, which is what local dev and the single-image
    # demo need; set = a write without a matching X-API-Key is refused.
    # The guard is exercised through the real FastAPI dependency rather than
    # by calling the helper directly, so a route that stops declaring the
    # dependency is caught here.
    # ------------------------------------------------------------------
    print("\n\033[1m8b. Optional write auth (API_KEY)\033[0m")
    # Deliberately not TestClient: that needs httpx, which is not a runtime
    # dependency of the app and is absent in a minimal checkout. The guard is
    # verified two other ways that need nothing extra — the dependency is
    # called directly, and the routes are checked for actually declaring it,
    # so a route that drops the dependency fails here rather than silently
    # reopening a write path.
    _auth_checks()

    print("\n\033[1m8a2. Memory visibility after an ingest\033[0m")
    _memory_visibility_checks()

    print("\n\033[1m8a3. Secondary recall (task 2)\033[0m")
    _recall_checks()

    print("\n\033[1m8a4. Prompt budget and startup model check\033[0m")
    _prompt_budget_checks()

    print("\n\033[1m8c. Rate limiting is distinguishable from a broken key\033[0m")
    # A 429 is caught server-side and answered 200, so the HTTP status carries
    # no signal at all. If rate_limited/retry_after_seconds ever disappear or
    # stop being set, the UI silently degrades to "check GROQ_API_KEY" for a
    # key that is working perfectly -- a wrong instruction, which is worse than
    # no instruction. These checks exist to make that failure loud.
    from backend.llm_client import RateLimitError
    from backend.models import SynthesisResponse
    from backend.synthesis import _fallback_response, generate_strategic_read
    from backend import llm_client as llm_client_mod

    limited = _fallback_response(
        "Nimbus AI", list(nimbus), "429 Too Many Requests",
        rate_limited=True, retry_after_seconds=20.0,
    )
    check("rate-limited fallback is flagged rate_limited",
          limited.rate_limited is True, f"got {limited.rate_limited}")
    check("rate-limited fallback carries the server's wait",
          limited.retry_after_seconds == 20.0,
          f"got {limited.retry_after_seconds}")
    check("rate-limited recommendation says to wait, not to fix the key",
          "wait" in limited.recommendation.lower()
          and "GROQ_API_KEY" not in limited.recommendation,
          f"got {limited.recommendation!r}")
    check("rate-limited fallback still returns the deterministic read (200, not 5xx)",
          limited.signal_count == len(nimbus) and bool(limited.patterns),
          f"signal_count={limited.signal_count}")

    broken = _fallback_response("Nimbus AI", list(nimbus), "401 Unauthorized")
    check("a non-rate-limit failure is NOT flagged rate_limited",
          broken.rate_limited is False, f"got {broken.rate_limited}")
    check("a non-rate-limit failure has no wait to show",
          broken.retry_after_seconds is None,
          f"got {broken.retry_after_seconds}")
    check("a non-rate-limit failure still points at the key",
          "GROQ_API_KEY" in broken.recommendation,
          f"got {broken.recommendation!r}")

    # The two must not be confusable: same timeline, different advice.
    check("rate-limit and broken-key advice are distinguishable",
          limited.recommendation != broken.recommendation,
          "both failures give the same advice")

    # End-to-end through the real entry point with a stubbed client raising the
    # concrete subclass, so the except ordering itself is under test. Catching
    # LLMError first would silently swallow RateLimitError's retry_after.
    real_call = llm_client_mod.client.call_llm_json

    def _raise_rate_limit(prompt, model=None, **kw):
        raise RateLimitError("rate limit exceeded", retry_after=42.0)

    llm_client_mod.client.call_llm_json = _raise_rate_limit
    try:
        out = generate_strategic_read("Nimbus AI")
        check("generate_strategic_read surfaces rate_limited end to end",
              out.rate_limited is True, f"got {out.rate_limited}")
        check("generate_strategic_read surfaces the wait end to end",
              out.retry_after_seconds == 42.0,
              f"got {out.retry_after_seconds}")
    finally:
        llm_client_mod.client.call_llm_json = real_call

    def _raise_generic(prompt, model=None, **kw):
        raise llm_client_mod.LLMError("connection reset")

    llm_client_mod.client.call_llm_json = _raise_generic
    try:
        out = generate_strategic_read("Nimbus AI")
        check("a generic LLM failure is not mistaken for a rate limit",
              out.rate_limited is False, f"got {out.rate_limited}")
    finally:
        llm_client_mod.client.call_llm_json = real_call

    # The schema must tolerate both, so an older cached response or a client
    # that omits the new fields still validates.
    check("SynthesisResponse defaults the new fields for old payloads",
          SynthesisResponse(
              competitor="X", patterns="p", inferred_intent="i",
              predicted_next_move="m", recommendation="r",
          ).rate_limited is False)

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
    from backend.facts import (MAX_INTERVAL_SPREAD as _MAX_SPREAD,
                                MIN_SIGNALS_FOR_EVIDENCE as _MIN_SIGNALS, build_facts)
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
    # Palisade: 11 signals, nothing repeats. The case the old rule got wrong in
    # both directions — ample evidence, no licensable repeat, and a forecast that
    # should have been allowed at a capped confidence.
    palisade_facts = build_facts(_seed["Palisade Security"], today=_TODAY)
    # Six signals but wildly uneven: enough to clear the count floor, not enough
    # to call a rhythm. Intervals of 10,10,10,91,10 give a 10-day median against a
    # 91-day longest gap — a spread of 9.1x, well past the 3x limit. Built by hand
    # rather than seeded so the spread does not drift if the corpus changes.
    _erratic = [
        Signal(competitor="Erratic Co", date=day, signal_type=kind, summary=f"Event {day}.")
        for day, kind in [
            ("2026-01-01", "feature"), ("2026-01-11", "messaging"),
            ("2026-01-21", "pricing"), ("2026-01-31", "hiring"),
            ("2026-05-01", "messaging"), ("2026-05-11", "feature"),
        ]
    ]
    _erratic_facts = build_facts(_erratic, today=_TODAY)

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
        # The pre-fix "cycle repeats three times" claim is gone, and in its place
        # this names the one transition the FACTS block actually prices. Both
        # halves matter: the claim had to be withdrawn, and the withdrawn claim
        # replaced by the truth rather than by vagueness.
        "patterns": (
            "Pricing cuts consistently precede tier launches, and feature->hiring "
            "is the one transition that has occurred twice."
        ),
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
    # This fixture is a refusal for a four-signal timeline, so it is checked
    # against one. It used to be checked against Nimbus (12 signals) and passed
    # there, which the evidence floor would not allow: a refusal on a timeline
    # that supports a read is not a well-formed refusal, it is an unwarranted
    # one, and D2 asserts that it is now rejected.
    _refusal_facts = build_facts(_seed["Vertex Cloud"], today=_TODAY)
    _refusal_tl = _timeline["Vertex Cloud"]
    check("D: a well-formed refusal passes (exempt from interval and quote checks)",
          _val.validate_response(refusal, _refusal_facts, _refusal_tl, refused=True) == [],
          _val.validate_response(refusal, _refusal_facts, _refusal_tl, refused=True))
    check("D: a refusal that claims confidence is rejected",
          any("refusal" in p for p in
              _val.validate_response({**refusal, "confidence": "medium"}, _refusal_facts,
                                     _refusal_tl, refused=True)),
          "a mid-confidence refusal passed")
    check("D: a refusal missing its missing_evidence sentence is rejected",
          any("missing_evidence" in p for p in
              _val.validate_response({k: v for k, v in refusal.items()
                                      if k != "missing_evidence"},
                                     _refusal_facts, _refusal_tl, refused=True)),
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
    _exempt = {"patterns": "No transition repeats.", "inferred_intent": "None.",
               "predicted_next_move": "No reliable prediction can be made.",
               "recommendation": "Keep collecting signals.",
               "confidence": "none",
               "missing_evidence": "A second occurrence of any transition."}
    _four_signal = build_facts(_seed["Vertex Cloud"], today=_TODAY)
    check("D: a refusal keeps the exemption and still owes its calibration",
          # The exemption is real: a refusal is not asked to forecast. Tested on
          # a timeline where refusing is warranted — the evidence floor added in
          # D2, so the exemption now has a precondition it did not use to have.
          not _val.validate_response(
              _exempt, _four_signal, _timeline["Vertex Cloud"], refused=True)
          # and it is not an exemption from saying how sure it is.
          and _val.validate_response(
              {k: v for k, v in _exempt.items() if k != "missing_evidence"},
              _four_signal, _timeline["Vertex Cloud"], refused=True)
          == ["missing_evidence is absent"],
          "the refusal branch is not holding refusals to the calibration rules")

    # -- D2. the evidence floor decides whether a refusal is available -----
    # The rule that replaced "refuse when no transition repeats". Four cases,
    # each of which the old proxy got wrong or could not express.
    _vertex_facts = build_facts(_seed["Vertex Cloud"], today=_TODAY)
    _pathfinder_facts = build_facts(_seed["Pathfinder Labs"], today=_TODAY)
    check("D2: the floor splits the corpus exactly as designed",
          {n for n in _seed if not build_facts(_seed[n], today=_TODAY).evidence_sufficient}
          == {"Pathfinder Labs", "Tidewater Analytics", "Vertex Cloud"},
          "the floor does not put exactly the three designed refusals below it")
    check("D2: three signals are insufficient however even their spacing",
          not _pathfinder_facts.evidence_sufficient
          and _pathfinder_facts.interval_spread is not None
          and _pathfinder_facts.interval_spread <= _MAX_SPREAD,
          "a count below the floor was waved through")
    check("D2: the signal floor is 5, so 4 refuses and 5 forecasts",
          not _vertex_facts.evidence_sufficient
          and build_facts([*_seed["Vertex Cloud"],
                           Signal(competitor="Vertex Cloud", date="2026-09-27",
                                  signal_type="feature", summary="A fifth signal.")],
                          today=_TODAY).evidence_sufficient,
          "the floor is not where it is documented to be")
    check("D2: enough signals but no rhythm is still insufficient",
          not _erratic_facts.evidence_sufficient
          and _erratic_facts.n >= _MIN_SIGNALS
          and _erratic_facts.interval_spread > _MAX_SPREAD,
          f"a spread of {_erratic_facts.interval_spread} was treated as a rhythm")
    check("D2: an unmeasurable rhythm counts as insufficient, not as fine",
          not build_facts([Signal(competitor="Same Day Co", date="2026-08-01",
                                  signal_type=t, summary="x") for t in
                           ("feature", "hiring", "pricing", "messaging", "feature")],
                          today=_TODAY).evidence_sufficient,
          "a zero-length rhythm was read as a perfect one")

    # Below the floor the only accepted answer is a refusal.
    check("D2: below the floor a forecast is rejected and told to refuse",
          any("must be 'none'" in p for p in
              _val.validate_response({**PRE_FIX, "confidence": "low"},
                                     _vertex_facts, _timeline["Vertex Cloud"])),
          "a forecast survived on a timeline with 4 signals")
    check("D2: below the floor a refusal is accepted",
          not _val.validate_response(
              {**PRE_FIX, "confidence": "none",
               "missing_evidence": "A second signal would show whether this is a stream."},
              _vertex_facts, _timeline["Vertex Cloud"], refused=True),
          "a warranted refusal was rejected")
    # Above the floor the only rejected answer is a refusal.
    check("D2: a refusal above the floor is rejected",
          any("refusal" in p and "REJECTED" not in p for p in
              _val.validate_response(
                  {**PRE_FIX, "confidence": "none",
                   "missing_evidence": "No transition has repeated, so nothing more "
                                       "is needed."},
                  palisade_facts, _timeline["Palisade Security"], refused=True)),
          "an unwarranted refusal on 11 signals was accepted")
    check("D2: the rejection names the sufficiency that forbids it",
          any(palisade_facts.sufficiency_note in p for p in
              _val.validate_response(
                  {**PRE_FIX, "confidence": "none",
                   "missing_evidence": "Nothing."},
                  palisade_facts, _timeline["Palisade Security"], refused=True)),
          "the refusal rejection does not tell the model why it is refused")

    # Sufficient but nothing repeats: capped at medium, no repeat language, and
    # the absence has to be disclosed.
    _no_repeat = {**PRE_FIX, "confidence": "medium",
                  "missing_evidence": "No transition has repeated yet; a second "
                                      "feature->hiring would be the first repeat."}
    check("D2: with nothing repeating, 'medium' is accepted and 'high' is not",
          not _val.check_confidence_allowed(_no_repeat, palisade_facts, refused=False)
          and any("medium" in p for p in _val.check_confidence_allowed(
              {**_no_repeat, "confidence": "high"}, palisade_facts, refused=False)),
          "the medium cap is not enforced when nothing repeats")
    check("D2: where something does repeat, 'high' is allowed again",
          not _val.check_confidence_allowed(
              {**_no_repeat, "confidence": "high"}, nimbus_facts, refused=False),
          "the cap leaked onto timelines that do have a repeat")
    check("D2: a read must disclose that nothing has repeated",
          any("no transition type has repeated" in p.lower() for p in
              _val.check_no_repeat_disclosure(
                  {**_no_repeat, "missing_evidence": "Another quarter of data."},
                  palisade_facts)),
          "a read could omit the non-repetition entirely")
    check("D2: saying nothing has repeated satisfies the disclosure",
          not _val.check_no_repeat_disclosure(_no_repeat, palisade_facts),
          "a compliant disclosure was rejected")
    check("D2: naming the missing second occurrence is the same disclosure",
          not _val.check_no_repeat_disclosure(
              {**_no_repeat, "missing_evidence":
               "A second observed instance of any transition would raise confidence "
               "in a recurring strategic cadence."}, palisade_facts),
          "the disclosure was rejected for not containing the word 'no'")
    check("D2: a disclosure that mentions neither is still rejected",
          bool(_val.check_no_repeat_disclosure(
              {**_no_repeat, "missing_evidence":
               "A feature release in the next two weeks."}, palisade_facts)),
          "an unrelated missing_evidence was accepted as a disclosure")
    check("D2: repeat language with nothing priced is rejected",
          any("no transition type repeating" in p for p in _val.check_repeat_language(
              {**_no_repeat,
               "patterns": "Their feature->hiring cycle repeats every quarter."},
              palisade_facts)),
          "an unpriced cycle claim was accepted")
    check("D2: denying a repeat is not claiming one",
          not _val.check_repeat_language(
              {"patterns": "No transition repeats, so the shape is provisional.",
               "inferred_intent": "None yet.",
               "predicted_next_move": "Another feature, probably.",
               "recommendation": "Watch for a second hiring signal."},
              palisade_facts),
          "a disclosure of non-repetition was read as a repeat claim")
    check("D2: naming a priced transition licenses the word",
          not _val.check_repeat_language(
              {"patterns": "feature->hiring repeats: it occurred twice.",
               "inferred_intent": "Hiring follows feature.",
               "predicted_next_move": "Another feature, probably.",
               "recommendation": "Watch."}, nimbus_facts),
          "a licensed repeat claim was rejected")
    check("D2: repeat language naming nothing priced is rejected even when "
          "something repeats",
          any("must name a transition" in p for p in _val.check_repeat_language(
              {"patterns": "The cycle repeats.", "inferred_intent": "x",
               "predicted_next_move": "y", "recommendation": "z"}, nimbus_facts)),
          "an unbacked repeat claim slipped through")
    check("D2: the FACTS block states sufficiency, and which way it goes",
          "EVIDENCE SUFFICIENCY: SUFFICIENT" in palisade_facts.render()
          and "You MUST produce a forecast" in palisade_facts.render()
          and "EVIDENCE SUFFICIENCY: INSUFFICIENT" in _vertex_facts.render()
          and "You MUST refuse" in _vertex_facts.render(),
          "the FACTS block does not tell the model which side of the line it is on")

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

    def _notice_values(notice: str) -> set[str]:
        """The confidence values the rendered notice actually tells the model to use.

        Read out of the text rather than out of the function that produced it,
        so this is a real check and not a tautology. Only the list is read, up to
        the first full stop: the rest of the line discusses the value it is
        *refusing*, which is not an offer.
        """
        line = re.search(r'^CONFIDENCE: report "confidence" as one of: ([^.]*)\.',
                         notice, re.M)
        if not line:
            return set()
        return set(re.findall(r"\b(none|high|medium|low)\b", line.group(1)))

    # The notice and the validator must not disagree, for any case. Checked
    # behaviourally: every value the notice offers is accepted by the validator,
    # and every value it omits is rejected — so "disagree" cannot survive even
    # if both texts were mangled the same way.
    _cases = [
        ("sufficient, some transition repeats", nimbus_facts),
        ("sufficient, nothing repeats", palisade_facts),
        ("insufficient, below the floor", build_facts(_seed["Vertex Cloud"],
                                                      today=_TODAY)),
        ("insufficient, rhythm too erratic", _erratic_facts),
    ]
    for _label, _case in _cases:
        _notice = _correction_notice(["something was wrong"], _case)
        _offered = _notice_values(_notice)
        check(f"notice: offers exactly the validator's values ({_label})",
              _offered == set(_val.allowed_confidence_values(_case, refused=False)),
              f"the notice offers {sorted(_offered)}; the validator accepts "
              f"{sorted(_val.allowed_confidence_values(_case, refused=False))}")
        _mismatched = []
        for _value in ("none", "high", "medium", "low"):
            _resp = {**PRE_FIX, "confidence": _value,
                     "missing_evidence": "A second feature->hiring would be the "
                                         "first repeat; nothing has repeated yet."}
            _accepted = not _val.check_confidence_allowed(
                _resp, _case, refused=(_value == "none"))
            if _accepted != (_value in _offered):
                _mismatched.append(_value)
        check(f"notice: every offered value is accepted and every other rejected "
              f"({_label})",
              not _mismatched,
              f"the notice and the validator disagree about {_mismatched}")
    # A rejection for one reason must not change the advice given for another:
    # the contract follows the timeline, and only the timeline.
    check("notice: the contract depends on the timeline, not the complaint",
          _notice_values(_forecast_notice) == _notice_values(
              _correction_notice(["an unrelated complaint"], nimbus_facts)),
          "the confidence contract changes with the problem being reported")
    check("notice: a below-the-floor timeline is told something different",
          _notice_values(_refusal_notice) != _notice_values(_forecast_notice),
          "the two timelines are being given the same contract")
    _refusal_read = {**PRE_FIX, "confidence": "none",
                     "missing_evidence": "A named retention deal would settle it."}
    check("notice: a refusal written to the notice passes the validator below the floor",
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
          _acceptable and _acceptable <= _notice_values(_forecast_notice),
          f"the validator accepts {sorted(_acceptable)} but the notice names "
          f"{sorted(_notice_values(_forecast_notice))}")
    check("notice: a forecast written to the notice passes the validator",
          all(not _val.validate_response({**acknowledged, "confidence": level},
                                         nimbus_facts, nimbus_tl)
              for level in _acceptable),
          "the notice's forecast branch is not satisfiable for any level it offers")
    check("notice: the overdue cap is still stated, so 'high' is not offered blindly",
          (nimbus_facts.days_overdue <= 0)
          or ('must not be "high"' in _forecast_notice),
          "the notice omits the overdue confidence cap")
    check("notice: a forecast below the floor is rejected, not merely unnamed",
          any("must be 'none'" in p
              for p in _val.validate_response(
                  {**_refusal_read, "confidence": "low"},
                  build_facts(_seed["Vertex Cloud"], today=_TODAY),
                  _timeline["Vertex Cloud"])),
          "the below-the-floor rule was dropped rather than the notice being fixed")

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
    from backend import facts as _facts_mod
    from backend import llm_client as _llm
    from backend import synthesis as _syn
    from backend import synthesis as _syn_mod
    from backend import validators as _val
    from backend import validators as _val_mod
    from backend.models import Signal as _Sig
    from datetime import date as _date

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
            replacements=[("facts_block=coverage + facts.render() + facts.overdue_instruction(),",
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
            replacements=[("    if value not in allowed:", "    if False:  # MUTANT")])),
        lambda m: any("must be one of" in p for p in
                      m["val"].check_confidence_allowed(
                          {**refusal, "confidence": "high"},
                          palisade_facts, refused=False)),
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
            replacements=[('    if not facts.evidence_sufficient:\n        return ("none",)',
                           '    if False:\n        return ("none",)  # MUTANT')])),
        lambda m: m["val"].allowed_confidence_values(
            m["F"](_seed["Vertex Cloud"]), refused=True) == ("none",),
    )
    _mutates(
        "defect 1: the notice asking the wrong end of the contract",
        lambda: _stack(synthesis=_mutant_from(
            "backend.synthesis",
            replacements=[("    forecast_values = validators.allowed_confidence_values("
                           "facts, refused=False)",
                           "    forecast_values = validators.allowed_confidence_values("
                           "facts, refused=True)  # MUTANT: wrong end")])),
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

    # -- D2 mutations: one per rule in the evidence floor -------------------
    # Each deletes exactly one clause of the new contract, so a rule that stops
    # being enforced is caught by the check that quotes it.
    _mutates(
        "D2: deleting the signal floor",
        lambda: _stack(facts_mod=_mutant_from(
            "backend.facts",
            replacements=[("MIN_SIGNALS_FOR_EVIDENCE = 5",
                           "MIN_SIGNALS_FOR_EVIDENCE = 0  # MUTANT")])),
        lambda m: not m["F"](_seed["Vertex Cloud"]).evidence_sufficient,
    )
    _mutates(
        "D2: deleting the dispersion guard",
        lambda: _stack(facts_mod=_mutant_from(
            "backend.facts",
            replacements=[("MAX_INTERVAL_SPREAD = 3",
                           "MAX_INTERVAL_SPREAD = 10000  # MUTANT")])),
        lambda m: not m["F"](_erratic).evidence_sufficient,
    )
    _mutates(
        "D2: treating an unmeasurable rhythm as sufficient",
        lambda: _stack(facts_mod=_mutant_from(
            "backend.facts",
            replacements=[("        return spread is not None and spread <= MAX_INTERVAL_SPREAD",
                           "        return spread is None or spread <= MAX_INTERVAL_SPREAD  # MUTANT")])),
        lambda m: not m["F"]([Signal(competitor="Same Day Co", date="2026-08-01",
                                      signal_type=t, summary="x") for t in
                               ("feature", "hiring", "pricing", "messaging",
                                "feature")]).evidence_sufficient,
    )
    _mutates(
        "D2: letting an unwarranted refusal through",
        lambda: _stack(validators=_mutant_from(
            "backend.validators",
            replacements=[('    if refused:\n        problems.append(\n'
                           '            f"the FACTS block records this evidence as '
                           '{facts.sufficiency_note}, so a "',
                           '    if False:  # MUTANT\n        problems.append(\n'
                           '            f"the FACTS block records this evidence as '
                           '{facts.sufficiency_note}, so a "')])),
        # The clause's job is to say that refusing is not available, not merely
        # that "none" is the wrong number: without it the model is still
        # rejected, but with a message that leaves it guessing which way to move.
        lambda m: any("a refusal" in p and "rejected" in p for p in
                      m["val"].check_confidence_allowed(
                          {"confidence": "none", "missing_evidence": "Nothing."},
                          palisade_facts, refused=True)),
    )
    _mutates(
        "D2: lifting the medium cap when nothing repeats",
        lambda: _stack(validators=_mutant_from(
            "backend.validators",
            replacements=[("    if not facts.repeated_transitions():\n"
                           '        return ("medium", "low")',
                           "    if False:  # MUTANT\n"
                           '        return ("medium", "low")')])),
        lambda m: bool(m["val"].check_confidence_allowed(
            {"confidence": "high", "missing_evidence": "No transition has repeated."},
            palisade_facts, refused=False)),
    )
    _mutates(
        "D2: dropping the no-repeat disclosure",
        lambda: _stack(validators=_mutant_from(
            "backend.validators",
            replacements=[("    if not facts.evidence_sufficient or facts.repeated_transitions():\n"
                           "        return []",
                           "    if True:  # MUTANT\n        return []")])),
        lambda m: bool(m["val"].check_no_repeat_disclosure(
            {"missing_evidence": "Another quarter of data."}, palisade_facts)),
    )
    _mutates(
        "D2: dropping the missing-second-occurrence form of the disclosure",
        lambda: _stack(validators=_mutant_from(
            "backend.validators",
            replacements=[("    if _MISSING_SECOND_DISCLOSURE.search(missing):\n"
                           "        return []",
                           "    if False:  # MUTANT\n        return []")])),
        lambda m: not m["val"].check_no_repeat_disclosure(
            {"missing_evidence": "A second observed instance of any transition would "
                                 "raise confidence in a recurring strategic cadence."},
            palisade_facts),
    )
    _mutates(
        "D2: dropping the repeat-language rule",
        lambda: _stack(validators=_mutant_from(
            "backend.validators",
            replacements=[("    claimed = _unnegated_repeat_words(text)",
                           "    claimed = []  # MUTANT")])),
        lambda m: bool(m["val"].check_repeat_language(
            {"patterns": "Their feature->hiring cycle repeats every quarter."},
            palisade_facts)),
    )
    _mutates(
        "D2: removing sufficiency from the FACTS block",
        lambda: _stack(facts_mod=_mutant_from(
            "backend.facts",
            replacements=[('        lines.append(f"- EVIDENCE SUFFICIENCY: {self.sufficiency_note}")',
                           "        pass  # MUTANT")])),
        lambda m: "EVIDENCE SUFFICIENCY" in m["F"](_seed["Vertex Cloud"]).render(),
    )
    _mutates(
        "D2: the notice withholding the rule from the retry",
        lambda: _stack(synthesis=_mutant_from(
            "backend.synthesis",
            replacements=[('    if not facts.repeated_transitions():\n'
                           '            lines.append(\n'
                           '                "No transition type repeats in this timeline',
                           '    if False:  # MUTANT\n'
                           '            lines.append(\n'
                           '                "No transition type repeats in this timeline')])),
        lambda m: "No transition type repeats" in
        m["syn"]._correction_notice(["x"], m["F"](_seed["Palisade Security"])),
    )

    # -- R: the recall rules, each one deleted to prove it is load-bearing ----
    #
    # Recall is the newest and the least exercised path, and its worst failure
    # is silent: a biased slice of a timeline reads exactly like the timeline
    # itself. So each rule below is mutated individually and must be caught.

    def _recall_mutation_runs(label, build, fires):
        """Same contract as _mutates, for paths outside the _stack quartet."""
        intact = fires()
        try:
            mutant = build()
        except AssertionError as exc:
            check(f"{label}: the mutation applied cleanly", False, str(exc))
            return
        mutated = fires(mutant)
        check(f"{label}: fires on the real code, silent once the rule is deleted",
              intact is True and mutated is False,
              f"intact={intact} mutated={mutated}")

    def _rec_derived_rows_have_uids(m=None):
        """True when recall never returns a row that is not a real signal.

        Takes the module under test rather than reading the global singleton:
        a mutation that is written to a clone and then never invoked proves
        nothing, and would happily report the real code's behaviour as the
        mutant's.
        """
        cli = (m or hc).client
        rows = cli.recall_signals("Nimbus AI", "pattern pricing")
        return bool(rows) and not any(
            "recalled a pattern in the market" in (r.summary or "")
            for r in rows)

    # R1: the signal_uid guard, deleted. This is the one that matters most: a
    # derived observation is the model's own commentary, not an observation, and
    # presenting it as a dated signal is a fabricated citation.
    _dbl_recall_uid = {
        "id": "mut-recall-derived", "bank_id": "competitor-nimbus-ai",
        "text": "Nimbus AI recalled a pattern in the market and repriced "
        "its pricing.",
        "context": "derived", "date": double._now(), "fact_type": "observation",
        "document_id": None, "mentioned_at": double._now(),
        "occurred_start": None, "state": "valid",
        "tags": ["signal", "type:feature"], "metadata": {},
        "entities": "Nimbus AI",
    }
    double.UNITS.append(_dbl_recall_uid)
    try:
        _recall_mutation_runs(
            "R1: deleting the metadata.signal_uid guard in recall_signals",
            lambda: _mutant_from(
                "backend.hindsight_client",
                replacements=[('        if not metadata.get("signal_uid"):\n'
                           '            return None  # a derived observation, not one of our signals',
                           '        if False:  # MUTANT\n'
                           '            return None')]),
            _rec_derived_rows_have_uids,
        )
    finally:
        double.UNITS[:] = [u for u in double.UNITS
                           if u.get("id") != "mut-recall-derived"]

    # R2: the blank-query guard, deleted, so a whitespace-only question is
    # answered with the top-k of nothing and reads as "no results".
    from fastapi import HTTPException as _HE2
    from backend import routes as _rts

    def _rec_blank_q_rejected(m=None):
        try:
            (m or _rts).recall_signals("Nimbus AI", "   ")
        except _HE2 as e:
            return e.status_code == 422
        return False

    _recall_mutation_runs(
        "R2: deleting the blank-q guard in the recall route",
        lambda: _mutant_from(
            "backend.routes",
            replacements=[('        raise HTTPException(status_code=422, detail="q must not be blank")',
                           "        pass  # MUTANT")]),
        _rec_blank_q_rejected,
    )

    # R3: the unknown-bank 404, collapsed into an empty list. A typo would then
    # be indistinguishable from an absence of evidence about a real company.
    def _rec_unknown_bank_is_404(m=None):
        try:
            (m or _rts).recall_signals("Nobody Here", "anything at all")
        except _HE2 as e:
            return e.status_code == 404
        return False

    _recall_mutation_runs(
        "R3: collapsing the unknown-bank 404 into an empty result",
        lambda: _mutant_from(
            "backend.routes",
            replacements=[("        raise HTTPException(\n"
                           "            status_code=404, detail=f\"No memory bank for '{competitor}'\"\n"
                           "        ) from None",
                           "        signals = []  # MUTANT")]),
        _rec_unknown_bank_is_404,
    )

    # R4: synthesis reaching for recall. The isolation rule is a code-review
    # convention unless something executes it, so the executable form is the
    # point of this mutation. The rule it guards is checked by reading source,
    # so the mutation is applied to source too -- a clone nobody calls would
    # report the real code's cleanliness as the mutant's.
    _synth_source_isolated = lambda src: not synthesis_uses_recall(src)

    _synth_src = (REPO_ROOT / "backend" / "synthesis.py").read_text()
    check("R4: synthesis stays isolated from recall (real source)",
          _synth_source_isolated(_synth_src), "synthesis.py mentions recall")
    _synth_mut, _hits = re.subn(
        r"    signals = hindsight_client\.client\.get_timeline\(competitor\)",
        '    signals = hindsight_client.client.recall_signals(\n'
        '        competitor, "what is the pattern?")  # MUTANT',
        _synth_src, count=1)
    check("R4: the mutation applied cleanly", _hits == 1, f"hits={_hits}")
    check("R4: pointing synthesis at recall is detected by the isolation check",
          _synth_source_isolated(_synth_mut) is False,
          "the mutated source still reads as isolated")

    # R5: the double answering with rows that share no term with the question.
    # The guard is the filter in the recall handler; the live server cannot be
    # swapped mid-run, so the mutation is evaluated against the double's own
    # scorer and its own bank contents.
    def _double_recall_filter_drops_unmatched(src: str) -> bool:
        m = re.search(r"^(\s*)hits = (\[.*?\])$", src, re.M)
        if not m:
            # No filter at all is the strongest form of the defect, not an
            # error: the rule is gone, so it is not upheld. Presence is
            # asserted separately so this cannot pass by accident.
            return False
        # Synthetic rows, not live bank contents: by this section earlier
        # checks have legitimately reshaped the double, and a rule that only
        # fires when some bank happens to be populated is not a rule.
        rows = [{"text": "Nimbus AI raised its minimum contract.",
                 "metadata": {"signal_summary": "Nimbus AI raised prices."},
                 "mentioned_at": "2026-01-02T00:00:00Z"}]
        terms = ["zzzqqq", "unrelated"]
        _score = lambda u: (
            -sum(1 for t in terms
                 if t in f"{u.get('text', '')} "
                         f"{(u.get('metadata') or {}).get('signal_summary', '')}".lower()),
            u.get("mentioned_at") or "")
        hits = eval(m.group(2), {"_score": _score}, {"rows": rows})
        return bool(rows) and hits == []

    _dbl_src = (REPO_ROOT / "tests" / "hindsight_double.py").read_text()
    check("R5: the double's recall handler still filters (the rule exists at all)",
          re.search(r"^\s*hits = \[u for u in rows if _score\(u\)\[0\] < 0\]$",
                    _dbl_src, re.M) is not None,
          "the recall filter assignment is missing from hindsight_double.py")
    check("R5: the double answers an unrelated query with nothing (real source)",
          _double_recall_filter_drops_unmatched(_dbl_src), "")
    _dbl_mut, _hits = re.subn(
        r"^(\s*)hits = \[u for u in rows if _score\(u\)\[0\] < 0\]$",
        r"\1hits = list(rows)  # MUTANT", _dbl_src, count=1, flags=re.M)
    check("R5: the mutation applied cleanly", _hits == 1, f"hits={_hits}")
    check("R5: ranking unmatched rows is caught",
          _double_recall_filter_drops_unmatched(_dbl_mut) is False,
          "the mutated filter still drops everything")

    # -- P: the prompt budget, mutated --------------------------------------
    #
    # The dangerous version of this feature is not the cap, it is the cap
    # without the disclosure. Each of these deletes one half of the guarantee
    # and must be caught.

    def _prompt_window_keeps_the_chain(m=None):
        """True when the window keeps the first signal, the newest, and repeats.

        Checked on a hand-built timeline rather than a run of identical types:
        a single-type chain is one transition repeated N-1 times, so every
        signal is a participant in a repeat and nothing is ever droppable. A
        newest-only implementation would pass that fixture by accident, which
        is exactly the regression this mutation is supposed to catch.
        """
        mod = m or _syn_mod
        # The first signal (messaging) is deliberately NOT part of the repeated
        # feature->pricing pair. With a fixture where it were, the repeat rule
        # would keep it anyway and deleting the first-signal rule would be a
        # no-op -- the mutation would "pass" without proving anything.
        types = ["messaging", "feature", "pricing", "hiring",
                 "feature", "pricing", "funding", "feature"]
        sigs = [_Sig(competitor="B", date=f"2026-01-{i + 1:02d}",
                     signal_type=t, summary=f"step {i}")
                for i, t in enumerate(types)]
        kept, dropped = mod.fit_prompt_window(sigs, limit=3)
        dates = {s.date for s in kept}
        return (dropped == 1
                and sigs[0].date in dates          # the first signal
                and all(s.date in dates for s in sigs[-3:])   # recency
                and all(s.date in dates for s in    # the repeat
                        (sigs[1], sigs[2], sigs[4], sigs[5])))

    _recall_mutation_runs(
        "P1: the prompt window keeping the NEWEST signals instead of the chain",
        lambda: _mutant_from(
            "backend.synthesis",
            replacements=[("    keep = {0, *range(len(ordered) - cap, len(ordered))}",
                           "    keep = set(range(len(ordered) - cap, len(ordered)))  "
                           "# MUTANT: newest only, first signal dropped")]),
        _prompt_window_keeps_the_chain,
    )

    def _prompt_window_keeps_repeats(m=None):
        """The weaker half of P1: recency intact, but repeats may be cut."""
        mod = m or _syn_mod
        types = ["messaging", "feature", "pricing", "hiring",
                 "feature", "pricing", "funding", "feature"]
        sigs = [_Sig(competitor="B", date=f"2026-01-{i + 1:02d}",
                     signal_type=t, summary=f"step {i}")
                for i, t in enumerate(types)]
        kept, _ = mod.fit_prompt_window(sigs, limit=3)
        dates = {s.date for s in kept}
        # Indices 1,2 and 4,5. Two of them sit outside the most-recent block,
        # so they are protected by the repeat rule alone.
        return all(s.date in dates for s in (sigs[1], sigs[2], sigs[4], sigs[5]))

    _recall_mutation_runs(
        "P1b: the prompt window cutting a repeated transition in half",
        lambda: _mutant_from(
            "backend.synthesis",
            replacements=[("        if counts[(earlier.signal_type, later.signal_type)] >= 2:",
                           "        if False:  # MUTANT: repeats are not protected")]),
        _prompt_window_keeps_repeats,
    )

    def _prompt_declares_omission(m=None):
        mod = m or _syn_mod
        # Mixed types, for the same reason as P1: an identical-type chain can
        # never be truncated, so a PARTIAL prompt would be unreachable.
        sigs = [_Sig(competitor="B", date=f"2026-02-{i + 1:02d}",
                     signal_type=["feature", "pricing", "hiring", "messaging",
                                  "funding", "feature"][i], summary=f"step {i}")
                for i in range(6)]
        # Lower the cap rather than padding the chain to today's default: the
        # property under test is the disclosure, not the particular number 40.
        original = mod.MAX_SIGNALS_IN_PROMPT
        mod.MAX_SIGNALS_IN_PROMPT = 3
        try:
            prompt = mod.build_prompt("B", sigs, today=_date(2026, 3, 1))
        finally:
            mod.MAX_SIGNALS_IN_PROMPT = original
        # Derived, not hardcoded to the cap: the window is the most recent N
        # plus the first signal, so a cap of 3 shows 4 and asserting "3 of 6"
        # would fail for the right reason on the wrong expectation.
        kept, dropped = mod.fit_prompt_window(sigs, limit=3)
        return ("EVIDENCE COVERAGE: PARTIAL" in prompt
                and dropped > 0
                and f"{len(kept)} of {len(sigs)}" in prompt
                and "repeated transition" in prompt)

    _recall_mutation_runs(
        "P2: truncating the prompt without telling the model what is missing",
        lambda: _mutant_from(
            "backend.synthesis",
            # The prompt is truncated exactly as before; what is deleted is
            # the admission of it. That is the failure being guarded against --
            # a window that never says it is one.
            replacements=[("        if not omitted\n        else (",
                           "        if True or not omitted  # MUTANT: the PARTIAL branch "
                           "is unreachable\n        else (")]),
        _prompt_declares_omission,
    )

    def _partial_claim_is_blocked(m=None):
        mod = m or _val_mod
        facts = _facts_mod.build_facts(
            [_Sig(competitor="B", date="2026-02-01", signal_type="pricing",
                  summary="x")],
            today=_date(2026, 3, 1), omitted=9, omitted_span="2025-01-01 to 2025-03-04")
        return mod.check_no_partial_claims(
            {"patterns": "Across their entire history they repriced.",
             "inferred_intent": "", "predicted_next_move": "",
             "recommendation": "", "missing_evidence": ""}, facts) != []

    _recall_mutation_runs(
        "P3: deleting the check that stops a window being called a whole history",
        lambda: _mutant_from(
            "backend.validators",
            replacements=[("    if not facts.omitted:\n        return []",
                           "    if True:  # MUTANT\n        return []")]),
        _partial_claim_is_blocked,
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


def _memory_visibility_checks() -> None:
    """The three things a user must be able to see after logging a signal.

    Streamlit cannot be executed inside the suite, so these assert the
    structure of frontend/app.py rather than the rendered output. That is a
    real limit and worth stating: what is pinned here is *that the calls are
    made in this order and are not conditional on each other*, which is the
    part that silently regresses -- a diff that renders only when the read
    succeeds looks identical in source review and is wrong in the product.
    """
    src = (REPO_ROOT / "frontend" / "app.py").read_text()

    # 1. With a cached read, the after-read runs by itself.
    check("the cached-read branch no longer waits for a button",
          "🔁 Re-read and show what changed" not in src,
          "the after-read is still behind a second click")
    _seg = src.split("### 🧠 What your signal changed")[1].split("#### Side by side")[0]
    check("the after-read issues POST /synthesize without a st.button wrapper",
          'api("POST", "/synthesize", json={"competitor": selected})' in _seg
          and "🔁 Re-read" not in _seg,
          "no synthesize call in the ingest-diff block")
    # Pinned as the literal guard expression, not merely as a present string:
    # "the call is in the source" also holds when the call is dead code behind
    # `if False and ...`, which is exactly how this check was caught being
    # vacuous. Asserting the positive form is the closest a non-executable UI
    # test can get to asserting that the call actually runs.
    check("the automatic after-read is gated on a live per-signal condition",
          "if _uid not in _auto_done:" in _seg,
          "the synthesize call is present but unreachable (dead-code guard)")
    check("nothing between that guard and the read can reintroduce a click",
          "st.button(" not in _seg.split("if _uid not in _auto_done:")[1]
          .split('api("POST", "/synthesize"')[0],
          "a button sits between the guard and the automatic read")
    check("the automatic after-read is guarded per signal, not per rerun",
          "ss_auto_reread" in _seg and "_uid not in _auto_done" in _seg,
          "an unguarded call re-reads the timeline on every Streamlit rerun")
    check("the guard is cleared by a reset, so a fresh demo can auto-read again",
          'st.session_state.pop("ss_auto_reread", None)' in src,
          "ss_auto_reread survives a reset and permanently blocks the diff")

    # 2. The write's receipt is unconditional; the read is not.
    _delta_at = _seg.find("memory_delta(selected, _pending)")
    _before_at = _seg.find("_before = st.session_state.get")
    check("the memory delta renders before the follow-up read is attempted",
          _delta_at != -1 and _before_at != -1 and _delta_at < _before_at,
          f"delta at {_delta_at}, read branch at {_before_at}")
    check("a rate-limited after-read still reports the wait",
          'if _after.get("rate_limited")' in _seg and "wait about" in _seg,
          "the rate-limit path does not tell the user to wait")
    check("a rate-limited after-read keeps the write receipt on screen",
          "memory_delta(selected, _pending)" in _seg.split('if _after.get("rate_limited")')[0],
          "the delta is gated behind a successful re-read")
    check("a rate-limited after-read says the stored signal is fine",
          "Your signal is stored" in _seg,
          "a 429 reads as a lost write")

    # 3. No cached read: the counts and the new row, without a hidden LLM call.
    check("the no-cached-read branch still shows what grew",
          "Memory has grown." in _seg,
          "the no-cached-read branch reports nothing")
    _fn = src.split("def memory_delta")[1].split("\ndef ")[0]
    # Scoped to the call expressions, not the bare field names: those also
    # appear in this function's own docstring, so a name-only match passed even
    # with the caption stripped of the field.
    for _field in ("signal_count", "fact_count", "last_write_at"):
        check(f"the delta reads {_field} out of the bank record",
              f"bank.get('{_field}'" in _fn,
              f"{_field} is never read from the record, so it cannot be shown")
    # Window rather than paren-split: the caption's own argument list contains
    # bank.get(...) parentheses, so splitting on ")" truncates mid-expression.
    _cap = _fn.split("st.caption(")[1][:400] if "st.caption(" in _fn else ""
    check("the delta caption renders all three values, not just one",
          all(f"bank.get('{f}'" in _cap for f in
              ("signal_count", "fact_count", "last_write_at")),
          f"caption: {_cap[:200]!r}")
    check("the delta shows the new signal as a dated, typed row",
          "row.get('date')" in _fn and "row.get('signal_type')" in _fn,
          "the new timeline row is not rendered")
    check("the delta reads the row back from Hindsight instead of echoing the POST",
          "fetch_timeline(competitor)" in _fn,
          "the row is echoed from the write response, so a lost write looks stored")
    check("the no-cached-read branch does not auto-run a read",
          'if st.button("🧠 Generate read to see what changed")' in _seg,
          "an unrequested LLM call is made on ingest")


def _prompt_budget_checks() -> None:
    """A prompt is a budget, and going over it must be visible, not silent.

    The cap itself is unremarkable. What matters is the second half: when
    signals are left out, every figure the model is allowed to cite is computed
    from the signals it can actually see, so a silently truncated prompt yields
    a fluent analysis of a fragment that reports itself as the whole timeline.
    The coverage line and the completeness validator are what stop that, and
    both are asserted here.
    """
    from datetime import date as _date

    from backend import facts as _facts_mod
    from backend import synthesis as _syn
    from backend import validators as _val
    from backend.config import MAX_SIGNALS_IN_PROMPT
    from backend.models import Signal as _Sig

    check("MAX_SIGNALS_IN_PROMPT is a positive integer",
          isinstance(MAX_SIGNALS_IN_PROMPT, int) and MAX_SIGNALS_IN_PROMPT > 0,
          f"got {MAX_SIGNALS_IN_PROMPT!r}")
    check("MAX_SIGNALS_IN_PROMPT is documented as an env knob in config",
          "MAX_SIGNALS_IN_PROMPT" in
          (REPO_ROOT / "backend" / "config.py").read_text()
          and "MAX_SIGNALS_IN_PROMPT" in (REPO_ROOT / ".env.example").read_text(),
          "the cap is not configurable from the environment")

    def _chain(n: int) -> list[_Sig]:
        """A long timeline of a single signal type.

        Note this is a pathological shape under the current window rule: a run
        of identical types is one transition repeated N-1 times, so every signal
        is a participant and nothing may be dropped. The over-cap checks below
        use `_mixed` instead, and say why.
        """
        return [
            _Sig(competitor="Budget Co", date=f"2026-{1 + i // 28:02d}-"
                                             f"{1 + i % 28:02d}",
                 signal_type="pricing",
                 summary=f"Budget Co moved on step {i} of the pricing chain.")
            for i in range(n)
        ]

    def _mixed() -> list[_Sig]:
        """Eight signals whose transition structure is known exactly.

        Consecutive types, with the feature->pricing pair occurring twice:

            1 feature  2 pricing  3 hiring  4 feature
            5 pricing  6 messaging  7 feature  8 hiring

        Pairs: (feature,pricing) x2 [signals 1,2 and 4,5], and one-offs at
        3, 6, 7, 8. At cap=3 the window must keep signal 1 (the first), 6,7,8
        (the most recent), and 1,2,4,5 (the repeat) -- leaving only signal 3
        droppable.
        """
        types = ["feature", "pricing", "hiring", "feature",
                 "pricing", "messaging", "feature", "hiring"]
        return [
            _Sig(competitor="Budget Co", date=f"2026-01-{i + 1:02d}",
                 signal_type=t,
                 summary=f"Step {i + 1}: a {t} signal from Budget Co.")
            for i, t in enumerate(types)
        ]

    # Under the cap: nothing is dropped, and the prompt says so.
    _under = _chain(5)
    _win, _drop = _syn.fit_prompt_window(_under)
    check("a timeline under the cap is passed whole", _drop == 0 and len(_win) == 5,
          f"dropped={_drop} kept={len(_win)}")
    _p_under = _syn.build_prompt("Budget Co", _under, today=_date(2026, 3, 1))
    check("a complete read is labelled COMPLETE",
          "EVIDENCE COVERAGE: COMPLETE" in _p_under,
          _p_under[:0] or "the coverage line is missing")
    check("a complete read says how many signals it saw",
          f"All 5 signals" in _p_under, "")

    # The three-part rule, on a timeline small enough to verify by hand.
    _mx = _mixed()
    _win, _drop = _syn.fit_prompt_window(_mx, limit=3)
    _kept_dates = [s.date for s in _win]
    check("the window is returned in date order",
          _kept_dates == sorted(_kept_dates), f"{_kept_dates}")
    check("the first signal survives the cap",
          _win[0].date == _mx[0].date,
          f"first kept {_win[0].date}, first overall {_mx[0].date}")
    check("the most recent signals survive the cap",
          all(s.date in _kept_dates for s in _mx[-3:]),
          f"missing {[s.date for s in _mx[-3:] if s.date not in _kept_dates]}")
    check("both instances of a repeated transition survive the cap",
          all(s.date in _kept_dates for s in (_mx[0], _mx[1], _mx[3], _mx[4])),
          "a repeated transition was cut in half by the window")
    check("only the one-off interior signal is dropped",
          _drop == 1 and _mx[2].date not in _kept_dates,
          f"dropped={_drop} kept={_kept_dates}")
    check("the counts and the window always add up to the bank",
          len(_win) + _drop == len(_mx), f"{len(_win)}+{_drop} vs {len(_mx)}")

    # The cap is a floor for recency, not a hard ceiling. A timeline that is one
    # repeating cycle end to end keeps everything, because dropping any signal
    # would turn an observed repeat into an apparent one-off. Pinned here so the
    # behaviour is deliberate: an over-long prompt over an understated timeline
    # is the right side of that trade, and it is a real bound callers must know.
    _repeating = _chain(MAX_SIGNALS_IN_PROMPT + 12)
    _win, _drop = _syn.fit_prompt_window(_repeating)
    check("a timeline that is one repeated transition is never truncated",
          _drop == 0 and len(_win) == len(_repeating),
          f"dropped={_drop} -- the window cut a repeat into an apparent one-off")

    # Over the cap, on a timeline whose transitions are all DISTINCT so that
    # something is actually droppable. This needs care: signal_type has five
    # values, so a long timeline repeats transitions by pigeonhole no matter
    # how it is arranged, and every signal then becomes protected. The fixture
    # below is the longest arrangement found with no repeated pair (nine
    # distinct pairs over ten signals), and the cap is lowered to 6 so the
    # window is over-long relative to the cap rather than to the 40 default.
    _distinct = ["pricing", "pricing", "feature", "pricing", "hiring",
                 "pricing", "messaging", "pricing", "funding", "pricing"]
    _over = [
        _Sig(competitor="Budget Co", date=f"2026-01-{i + 1:02d}",
             signal_type=t, summary=f"Budget Co step {i}, a {t} signal.")
        for i, t in enumerate(_distinct)
    ]
    _cap = 6
    _win, _drop = _syn.fit_prompt_window(_over, limit=_cap)
    _kept_dates = [w.date for w in _win]
    check("a timeline over the cap is truncated", _drop == 3,
          f"dropped={_drop}, expected 3")
    check("the first signal is kept even on an over-long timeline",
          _win[0].date == _over[0].date,
          f"first kept {_win[0].date} vs first overall {_over[0].date}")
    check("the most recent signals are kept on an over-long timeline",
          all(s.date in _kept_dates for s in _over[-_cap:]),
          f"missing {[s.date for s in _over[-_cap:] if s.date not in _kept_dates]}")
    check("the window is larger than the cap by exactly the first signal",
          len(_win) == _cap + 1, f"kept={len(_win)}")
    check("the dropped signals are the interior ones, not a tail",
          _over[1].date not in _kept_dates and _over[2].date not in _kept_dates,
          f"kept={_kept_dates}")

    # The honest bound on the rule, pinned as a test rather than left for
    # someone to rediscover. `limit` is a floor for recency, not a ceiling:
    # with five signal types, a timeline of any real length repeats transitions
    # by pigeonhole, so in practice the cap stops being a cap and the prompt
    # carries the whole bank. The prompt-budget machinery still pays for itself
    # -- the coverage line, the provenance fields and the validator all report
    # the real numbers -- but nobody should believe MAX_SIGNALS_IN_PROMPT bounds
    # this prompt's size. Widening signal_type, or capping the number of
    # protected repeats, is the fix if that ever matters.
    _real = [
        _Sig(competitor="Budget Co", date=f"2026-{1 + i // 28:02d}-"
                                         f"{1 + i % 28:02d}",
             signal_type=["pricing", "feature", "hiring", "messaging",
                          "funding"][i % 5],
             summary=f"Budget Co step {i}.")
        for i in range(MAX_SIGNALS_IN_PROMPT + 12)
    ]
    # Separate names: _win/_drop still describe _over, and the checks below
    # build their prompt and facts from that window. Overwriting them here
    # silently pointed the coverage and validator checks at the wrong timeline.
    _win_real, _drop_real = _syn.fit_prompt_window(_real)
    check("with five signal types the cap is not a hard ceiling",
          _drop_real == 0 and len(_win_real) == len(_real),
          f"dropped={_drop_real} kept={len(_win_real)} -- if this now "
          f"truncates, the bound changed and this test needs re-reading, not "
          f"deleting")
    _p_real = _syn.build_prompt("Budget Co", _real, today=_date(2026, 3, 1))
    check("a bank carried whole is still labelled COMPLETE",
          "EVIDENCE COVERAGE: COMPLETE" in _p_real
          and f"All {len(_real)} signals" in _p_real, "")

    _original_cap = _syn.MAX_SIGNALS_IN_PROMPT
    _syn.MAX_SIGNALS_IN_PROMPT = _cap
    try:
        _p_over = _syn.build_prompt("Budget Co", _over, today=_date(2026, 3, 1))
        _facts_partial = _facts_mod.build_facts(
            _over, today=_date(2026, 3, 1), omitted=_drop,
            omitted_span=_syn.omitted_span(_over, _win, _cap), window=_win)
    finally:
        _syn.MAX_SIGNALS_IN_PROMPT = _original_cap

    check("a truncated read is labelled PARTIAL",
          "EVIDENCE COVERAGE: PARTIAL" in _p_over, "")
    check("a truncated read states how many signals it saw and how many exist",
          f"{len(_win)} of {len(_over)}" in _p_over,
          "the coverage line does not give both counts")
    check("a truncated read says what the window keeps",
          all(t in _p_over for t in ("the first signal", "the most recent",
                                     "repeated transition")),
          "the model is not told how the window was chosen")
    check("a truncated read states the cap it used",
          f"the {_cap} most recent" in _p_over,
          "the coverage line names a cap the caller never used")
    check("a truncated read does not claim the oldest are simply missing",
          "OLDEST" not in _p_over,
          "the coverage line still describes a tail, but the window keeps the first")
    check("the omitted signals are absent from the rendered timeline",
          all(s.summary not in _p_over for s in _over
              if s.date not in [w.date for w in _win]),
          "an omitted signal is still in the prompt")

    # The validator: a window described as the whole history is a defect.
    _claims = {
        "patterns": "Across their entire history they repriced on a cadence.",
        "inferred_intent": "Monetisation pressure.",
        "predicted_next_move": "Another price rise.",
        "recommendation": "Watch pricing.",
        "missing_evidence": "",
    }
    check("a completeness claim over a window is rejected",
          bool(_val.check_no_partial_claims(_claims, _facts_partial)),
          "the validator accepted a whole-history claim built from a window")
    _reject = _val.check_no_partial_claims(_claims, _facts_partial)[0]
    check("the rejection names the shown count and the total",
          str(len(_win)) in _reject and str(len(_over)) in _reject,
          f"got {_reject!r}")
    check("the rejection names the omitted span, not a before-date",
          _facts_partial.omitted_span in _reject and "nothing before" not in _reject,
          f"got {_reject!r}")
    _disclosed = dict(_claims, patterns=(
        f"Across the {len(_win)} signals shown, excluding {_drop} between "
        f"{_facts_partial.omitted_span}, they repriced on a cadence."))
    check("a completeness claim that discloses the window is accepted",
          _val.check_no_partial_claims(_disclosed, _facts_partial) == [],
          f"got {_val.check_no_partial_claims(_disclosed, _facts_partial)}")
    _facts_full = _facts_mod.build_facts(_real, today=_date(2026, 3, 1))
    check("with nothing omitted, completeness language is fine",
          _val.check_no_partial_claims(_claims, _facts_full) == [],
          "the validator fired on a complete timeline")

    # Cadence is measured on the full timeline. A window is a subset of the
    # history, not a shorter one, and measuring across a gap the selection
    # created would invent a silence the company never had.
    _gap_claim = _facts_mod.build_facts(
        _over, today=_date(2026, 3, 1), omitted=_drop, window=_win)
    check("the facts still measure the whole timeline, not the window",
          _gap_claim.n == len(_over) and _gap_claim.shown == len(_win),
          f"n={_gap_claim.n} shown={_gap_claim.shown}")
    check("the reported cadence is the bank's, not the window's",
          _gap_claim.intervals == _facts_mod.build_facts(
              _over, today=_date(2026, 3, 1)).intervals,
          "the intervals were measured across the selection's own gaps")

    # A cap of zero must fail loudly rather than produce an empty prompt.
    try:
        _syn.fit_prompt_window(_over, limit=0)
    except ValueError:
        _raised = True
    else:
        _raised = False
    check("a non-positive cap raises instead of analysing nothing", _raised, "")

    # The read reports its own provenance.
    check("SynthesisResponse carries the prompt window counts",
          {"prompt_signal_count", "signals_omitted_from_prompt",
           "prompt_omitted_span"} <= set(
              __import__("backend.models", fromlist=["SynthesisResponse"])
              .SynthesisResponse.model_fields),
          "the response does not report what the model actually saw")

    # The reader is told, not just the model.
    _app_src = (REPO_ROOT / "frontend" / "app.py").read_text()
    check("the UI reports the prompt window when signals were omitted",
          "signals_omitted_from_prompt" in _app_src
          and "prompt_signal_count" in _app_src,
          "a truncated read renders as a whole one")
    check("the UI names the omitted span",
          "prompt_omitted_span" in _app_src,
          "the reader is not told where the evidence is missing")
    check("the UI does not describe the window as the newest signals",
          "most recent of" not in _app_src,
          "the UI still describes a window the code no longer builds")

    # The startup model check: findings, not logging, and no exceptions.
    from backend import llm_client as _llm
    from backend.main import bootstrap as _boot

    check("verify_configured_models returns a list of findings",
          isinstance(_llm.verify_configured_models(), list),
          "the startup model check does not return findings")
    check("the startup path calls the model check",
          "verify_configured_models" in (REPO_ROOT / "backend" / "main.py").read_text(),
          "bootstrap never verifies the configured models")
    check("the model check cannot stop the service booting",
          "except Exception" in (REPO_ROOT / "backend" / "main.py").read_text()
          and "verify_configured_models" in
          (REPO_ROOT / "backend" / "main.py").read_text(),
          "a probe failure is not contained in bootstrap")

    # The three outcomes, driven through the double rather than the real API.
    import requests as _rq

    def _probe_with(payload, status=200, boom=False):
        class _R:
            status_code = status
            text = "probe"

            def json(_self):
                if boom:
                    raise ValueError("not json")
                return {"data": [{"id": i} for i in payload]}
        return _R()

    _real_get = _llm.client._session.get
    try:
        _llm.client._models_probed = False
        _llm.client._models_cache = None
        # A healthy account: both configured models served.
        _llm.client._models_probed = False
        _llm.client._session.get = lambda *a, **k: _probe_with(
            [_llm.PRIMARY_MODEL, _llm.FALLBACK_MODEL])
        check("a servable primary model reports no problem",
              _llm.verify_configured_models() == [],
              f"got {_llm.verify_configured_models()}")
        _llm.client._models_probed = False
        _llm.client._session.get = lambda *a, **k: _probe_with(
            [_llm.PRIMARY_MODEL])
        check("an unserved fallback is reported even when the primary is fine",
              any(_llm.FALLBACK_MODEL in p
                  for p in _llm.verify_configured_models()),
              "a missing fallback passes unnoticed")

        _llm.client._models_probed = False
        _llm.client._session.get = lambda *a, **k: _probe_with(
            ["some/other-model"])
        _probs = _llm.verify_configured_models()
        check("an unserved primary model is reported by name",
              any(_llm.PRIMARY_MODEL in p for p in _probs),
              f"got {_probs}")
        check("an unserved primary with no usable fallback says reads will fail",
              any("will fail" in p for p in _probs), f"got {_probs}")
        check("the served models are listed so the operator can pick one",
              any("some/other-model" in p for p in _probs), f"got {_probs}")

        _llm.client._models_probed = False
        _llm.client._session.get = lambda *a, **k: _probe_with(
            [_llm.FALLBACK_MODEL])
        _probs = _llm.verify_configured_models()
        check("an unserved primary with a served fallback says it falls back",
              any("fall back" in p for p in _probs), f"got {_probs}")

        _llm.client._models_probed = False
        _llm.client._session.get = lambda *a, **k: _probe_with(
            [_llm.PRIMARY_MODEL], status=401)
        _probs = _llm.verify_configured_models()
        check("a rejected key is reported as a key problem, not a model problem",
              any("rejected the API key" in p for p in _probs), f"got {_probs}")

        def _boom(*a, **k):
            raise _rq.RequestException("connection reset")
        _llm.client._models_probed = False
        _llm.client._session.get = _boom
        _probs = _llm.verify_configured_models()
        check("a network failure is reported as unverified, not as misconfiguration",
              any("Could not verify" in p for p in _probs)
              and not any("rejected" in p for p in _probs), f"got {_probs}")

        _llm.client._models_probed = False
        _llm.client._session.get = lambda *a, **k: _probe_with([], boom=True)
        _probs = _llm.verify_configured_models()
        check("an unparseable model list is reported, not raised",
              any("Could not verify" in p for p in _probs), f"got {_probs}")
    finally:
        _llm.client._session.get = _real_get
        _llm.client._models_probed = False
        _llm.client._models_cache = None


def _recall_checks() -> None:
    """Recall answers a question. It must never become the analysis substrate.

    The rule that matters is negative and structural: synthesis must never
    import or call recall. Recall returns the k most semantically similar
    memories, so a synthesis built on it would analyse a biased slice of the
    timeline and report the bias as a finding about the company. That is the
    failure the timeline method's docstring was written to prevent, and it is
    worth an executable assertion rather than a code-review convention.
    """
    # --- the separation itself, asserted against the real module ----------
    _syn = (REPO_ROOT / "backend" / "synthesis.py").read_text()
    _syn_code = "\n".join(
        ln for ln in _syn.splitlines() if not ln.strip().startswith("#")
    )
    check("synthesis.py neither calls nor imports recall",
          not synthesis_uses_recall(_syn),
          f"synthesis.py uses recall: "
          f"{_RECALL_USE.findall(_syn)[:3]}")
    _probe = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0,'.')\n"
         "import backend.synthesis as s\n"
         "names=[n for n in dir(s) if 'recall' in n.lower()]\n"
         "src=open('backend/synthesis.py').read()\n"
         "print(names, 'recall_signals' in src)\n"],
        capture_output=True, text=True,
    )
    check("importing synthesis exposes no recall symbol",
          _probe.returncode == 0 and _probe.stdout.strip().endswith("[] False"),
          f"got {_probe.stdout.strip()!r} {_probe.stderr[-160:]}")

    # And the real retrieval path is still the complete timeline.
    check("synthesis still retrieves via get_timeline",
          "get_timeline" in _syn_code, "synthesis no longer uses get_timeline")

    # --- client behaviour, against the double ------------------------------
    import json as _json
    import urllib.error as _ue
    import urllib.request as _ur

    _hc = _json.dumps  # noqa: F841  (readability only)
    from backend import hindsight_client as hcl

    def _recall_raw(bank: str, body: dict) -> dict:
        req = _ur.Request(
            f"{BASE}/v1/default/banks/{bank}/memories/recall",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer hsk_selftest"},
            method="POST",
        )
        with _ur.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    # Self-seeding: earlier sections may have emptied or reshaped the shared
    # double, so this one writes the fixture it needs and removes it after,
    # rather than inheriting whatever state it happens to run in.
    import hindsight_double as _d
    from backend.models import Signal as _Sig
    _bank = "competitor-recall-fixture"
    _fixture_signals = [
        _Sig(competitor="Recall Fixture", date="2026-03-02", signal_type="pricing",
             summary="Recall Fixture raised its minimum contract to $40,000.",
             source="probe"),
        _Sig(competitor="Recall Fixture", date="2026-07-19", signal_type="feature",
             summary="Recall Fixture shipped automatic regional failover.",
             source="probe"),
    ]
    # Writing a signal registers the display name in data/competitors.json --
    # a gitignored file, so a leaked test fixture there is invisible to
    # `git status` and outlives the run. Snapshot it BEFORE the write.
    _REG = hcl._read_registry()
    _REG_BEFORE = dict(_REG)
    hcl.client.write_signals(_fixture_signals)
    _UNITS_BEFORE = len(_d.UNITS)
    # The spec's default tags_match is "any", which INCLUDES untagged rows.
    _loose = _recall_raw(_bank, {"query": "failover", "tags": ["signal"]})
    _strict = _recall_raw(_bank, {"query": "failover", "tags": ["signal"],
                                  "tags_match": "all_strict"})

    check("the double reproduces the spec default that leaks untagged rows",
          len(_loose["results"]) >= len(_strict["results"]),
          f"loose={len(_loose['results'])} strict={len(_strict['results'])}")
    check("the client sends tags_match=all_strict explicitly",
          "all_strict" in (REPO_ROOT / "backend" / "hindsight_client.py").read_text(),
          "the client omits tags_match, so Hindsight's 'any' default applies")
    check("strict scoping returns a subset of the loose scope",
          len(_strict["results"]) <= len(_loose["results"]),
          f"strict={len(_strict['results'])} loose={len(_loose['results'])}")

    # A recall result has no `date` field, per the spec. If the double ever
    # grew one, this stops testing the real mapping hazard.
    # The real RecallResult has no `date`; the double must not invent one, or a
    # client reading result["date"] would work here and raise KeyError against
    # the live API. occurred_start/mentioned_at are the real date carriers.
    check("a recall result carries no date field (real 0.10.1 shape)",
          _strict["results"] and "date" not in _strict["results"][0],
          f"keys: {sorted(_strict['results'][0])}")
    check("a recall result carries a real date via occurred_start",
          bool(_strict["results"][0].get("occurred_start")
               or _strict["results"][0].get("mentioned_at")),
          f"result: {_strict['results'][0]}")

    # The signal_uid guard, not the tags, is what keeps derived observations
    # out: the double's own notes record that observations INHERIT tags.
    _d.UNITS.append({
        "id": "obs-check", "bank_id": _bank,
        "text": "Recall Fixture appears to be shifting failover strategy "
        "based on recent activity.",
        "context": "derived", "date": _d._now(), "fact_type": "observation",
        "document_id": None, "mentioned_at": _d._now(), "occurred_start": None,
        "state": "valid", "tags": ["signal", "type:feature"], "metadata": {},
        "entities": "Signal Stack",
    })
    try:
        _noisy = _recall_raw(_bank, {"query": "failover", "tags": ["signal"],
                                     "tags_match": "all_strict"})
        _derived_in_api = [
            r for r in _noisy["results"]
            if not (r.get("metadata") or {}).get("signal_uid")
        ]
        check("a derived observation with no signal_uid reaches the API response",
              len(_derived_in_api) >= 1,
              "the fixture did not produce a derived row, so the guard is untested")
        _client_out = hcl.client.recall_signals("Recall Fixture", "failover")
        check("the client drops it: every returned Signal is a real signal",
              _client_out and all(s.uid for s in _client_out),
              f"returned {len(_client_out)} rows")
        check("the client returns strictly fewer rows than the API did",
              len(_client_out) < len(_noisy["results"]),
              f"client={len(_client_out)} api={len(_noisy['results'])}")
        check("recall results are typed and dated Signals",
              all(s.date and s.signal_type and s.summary for s in _client_out),
              "a returned row is missing date, type or summary")
        check("recall dedupes by signal_uid",
              len({s.uid for s in _client_out}) == len(_client_out),
              "the same signal came back more than once")
    finally:
        _d.UNITS[:] = [u for u in _d.UNITS if u.get("id") != "obs-check"]
        assert len(_d.UNITS) == _UNITS_BEFORE, "fixture leaked into the double"

    # Empty match and the cap.
    _none = hcl.client.recall_signals("Recall Fixture", "zzzqqq nonexistent term")
    check("a query matching nothing returns an empty list, not an error",
          _none == [], f"got {len(_none)} rows")
    check("the result cap is enforced",
          len(hcl.client.recall_signals("Recall Fixture", "signal", limit=2)) <= 2,
          "limit was ignored")

    # --- endpoint validation, over HTTP ------------------------------------
    from fastapi import HTTPException as _HE
    from backend import routes as _routes

    def _call(fn, *a, **kw) -> tuple[int, object]:
        """Invoke a route function and report the status it would answer with.

        The suite has no running ASGI app (TestClient needs httpx, which is not
        a runtime dependency), so the route body is called directly and its
        HTTPException status captured. That is what the 422/404 behaviour
        actually consists of; what it does not cover is FastAPI's own parameter
        validation, which is asserted separately below.
        """
        try:
            return 200, fn(*a, **kw)
        except _HE as e:
            return e.status_code, e.detail

    def _get(q: str, competitor: str = "Recall Fixture") -> tuple[int, object]:
        return _call(_routes.recall_signals, competitor, q)

    _st, _body = _get("failover")
    check("GET /recall with a q returns 200", _st == 200, f"got {_st} {_body}")
    check("the response labels itself as secondary retrieval",
          getattr(_body, "retrieval", "") == "semantic-secondary", f"body: {_body}")
    check("the response echoes the query it answered",
          getattr(_body, "query", "") == "failover", f"body: {_body}")
    check("the response is not the timeline shape (no signal_count-only payload)",
          hasattr(_body, "signals") and hasattr(_body, "retrieval"),
          f"body: {_body}")
    _st, _det = _get("   ")
    check("a blank q is rejected with 422", _st == 422, f"got {_st}")
    _st, _det = _get("x" * 201)
    check("a q over 200 characters is rejected with 422", _st == 422, f"got {_st}")
    check("the length limit is the documented 200",
          "200" in str(_det), f"detail: {_det}")
    _st, _det = _get("anything", "competitor-does-not-exist")
    check("an unknown bank is 404, not an empty list", _st == 404, f"got {_st}")
    check("the 404 says which bank was missing",
          "does-not-exist" in str(_det), f"detail: {_det}")

    # FastAPI's own validation, which the direct call above bypasses: q is
    # required and bounded at the schema level, so a missing or over-long q is
    # refused before the handler body runs at all.
    _rp = _routes.router.routes
    _recall_route = [x for x in _rp if x.path == "/recall/{competitor}"]
    check("the recall route is registered as a GET",
          _recall_route and "GET" in (_recall_route[0].methods or set()),
          f"routes: {[x.path for x in _rp if 'recall' in x.path]}")
    _sig = inspect.signature(_routes.recall_signals)
    # FastAPI here hands the Query(...) marker to the parameter default, and
    # the MinLen/MaxLen constraints hang off that object's .metadata -- not off
    # the annotation, and not as attributes of the marker itself.
    _qparam = _sig.parameters["q"]
    _qdef = _qparam.default
    from pydantic_core import PydanticUndefined as _Undef
    _qfield = [f for f in _recall_route[0].dependant.query_params
               if f.name == "q"]
    check("q is a required query parameter, not a defaulted body field",
          _qparam.default is not inspect.Parameter.empty
          and type(_qdef).__name__ == "Query"
          and _qdef.default is _Undef
          and _qfield and _qfield[0].get_default() is _Undef,
          f"default: {_qdef!r} (type {type(_qdef).__name__}), "
          f"fastapi default: {_qfield[0].get_default() if _qfield else None!r}")
    check("q is the only query parameter on the route",
          len(_recall_route[0].dependant.query_params) == 1,
          f"params: {[f.name for f in _recall_route[0].dependant.query_params]}")
    _qm = {type(c).__name__: c for c in getattr(_qdef, "metadata", []) or []}
    check("q is bounded at 200 characters in the schema",
          getattr(_qm.get("MaxLen"), "max_length", None) == 200,
          f"constraints: {_qm}")
    check("a blank-only q is admitted by the schema and caught in the body",
          getattr(_qm.get("MinLen"), "min_length", None) == 1,
          f"constraints: {_qm}")
    check("q is documented in the OpenAPI schema",
          bool(getattr(_qdef, "description", None)),
          f"description: {getattr(_qdef, 'description', None)!r}")
    check("the schema bound is the shared constant, not a second literal",
          getattr(_qm.get("MaxLen"), "max_length", None)
          == hcl.RECALL_MAX_QUERY_CHARS,
          f"schema={getattr(_qm.get('MaxLen'), 'max_length', None)} "
          f"constant={hcl.RECALL_MAX_QUERY_CHARS}")

    # --- UI ----------------------------------------------------------------
    _app = (REPO_ROOT / "frontend" / "app.py").read_text()
    check("the UI offers a memory question box",
          "ss_recall_q" in _app and "Search memory" in _app,
          "no recall input in the UI")
    check("the UI states the slice is not the full timeline",
          "not** the full" in _app or "not the full" in _app,
          "the recall box does not distinguish itself from the strategic read")
    check("the UI renders each result with its date and type",
          "_s['date']" in _app and "_s['signal_type']" in _app,
          "results are rendered without date or type")
    check("the UI does not run summaries through markdown",
          "unsafe_allow_html=False" in _app,
          "stored summaries may be rendered as markdown/HTML")
    check("the write key is not attached to reads",
          "WRITE_HEADERS if method.upper() not in" in _app,
          "the API key rides on GETs, widening its exposure")

    # Teardown: the fixture bank and its registry entry are ours, not the
    # suite's. Restoring the file rather than popping one key means a test that
    # added several names cannot leave any of them behind. The assertions come
    # AFTER the restore, because "the suite leaves the project as it found it"
    # is the property worth proving.
    _d.BANKS.pop(_bank, None)
    _d.UNITS[:] = [u for u in _d.UNITS if u.get("bank_id") != _bank]
    hcl._write_registry(_REG_BEFORE)
    _seed_names = len(json.loads(
        (REPO_ROOT / "data" / "seed_signals.json").read_text())["competitors"])
    check("the recall fixture is not left in the double after teardown",
          _bank not in _d.BANKS
          and not [u for u in _d.UNITS if u["bank_id"] == _bank],
          f"bank={_bank} still present")
    check("the recall fixture is not left in data/competitors.json",
          "recall-fixture" not in hcl._read_registry(),
          f"got {sorted(hcl._read_registry())}")
    check("the registry is restored to exactly its pre-section contents",
          hcl._read_registry() == _REG_BEFORE,
          f"before={sorted(_REG_BEFORE)} after={sorted(hcl._read_registry())}")
    check("the restored registry holds exactly the real competitors",
          len(hcl._read_registry()) == _seed_names,
          f"got {len(hcl._read_registry())}, want {_seed_names}")


def _auth_checks() -> None:
    """Optional write auth, both modes.

    The guard is called directly (it is a plain dependency that raises
    HTTPException) and, separately, the routes are checked for actually
    declaring it. Calling the helper alone would pass even if a route dropped
    the dependency, which is the failure that matters, so both halves are
    pinned. API_KEY is read at import time by routes.py, so both that binding
    and config's are swapped per case.
    """
    import backend.config as cfg
    from backend import routes as rt
    from fastapi import HTTPException

    def with_key(key: str):
        real_cfg, real_rt = cfg.API_KEY, rt.API_KEY
        cfg.API_KEY = rt.API_KEY = key

        def restore() -> None:
            cfg.API_KEY, rt.API_KEY = real_cfg, real_rt

        return restore

    def rejects(header) -> bool:
        try:
            rt.require_write_key(x_api_key=header)
        except HTTPException as exc:
            return exc.status_code == 401
        return False

    # --- API_KEY unset: unchanged behaviour, no header needed ------------
    restore = with_key("")
    try:
        check("API_KEY unset: write with no header is allowed (unchanged behaviour)",
              not rejects(None), "an unauthenticated write was refused")
        check("API_KEY unset: write_auth_required() is False",
              not cfg.write_auth_required(), "got True")
        check("API_KEY unset: even a wrong header is allowed",
              not rejects("anything"), "a header was rejected while disabled")
    finally:
        restore()

    # --- API_KEY set: writes need the matching header ---------------------
    KEY = "selftest-write-key"
    restore = with_key(KEY)
    try:
        check("API_KEY set: write_auth_required() is True",
              cfg.write_auth_required(), "got False")
        check("API_KEY set: write with no header is 401",
              rejects(None), "a missing key was allowed")
        check("API_KEY set: write with an empty header is 401",
              rejects(""), "an empty key was allowed")
        check("API_KEY set: write with a wrong key is 401",
              rejects("wrong-key"), "a wrong key was allowed")
        check("API_KEY set: write with a key differing only in case is 401",
              rejects(KEY.upper()), "a case-variant key was allowed")
        check("API_KEY set: write with the correct key is allowed",
              not rejects(KEY), "the correct key was refused")
        check("API_KEY set: surrounding whitespace in the header is tolerated",
              not rejects(f"  {KEY}  "), "a padded key was refused")
    finally:
        restore()

    # The routes that write to Hindsight must actually declare the guard.
    # This is the check that catches someone deleting `dependencies=[...]`.
    guarded = set()
    for route in rt.router.routes:
        methods = getattr(route, "methods", set()) or set()
        if "POST" not in methods:
            continue
        if any(getattr(d, "dependency", None) is rt.require_write_key
               for d in (getattr(route, "dependencies", None) or [])):
            guarded.add((route.path, "POST"))
    check("POST /competitors declares the write guard",
          ("/competitors", "POST") in guarded, f"guarded routes: {sorted(guarded)}")
    check("POST /signals declares the write guard",
          ("/signals", "POST") in guarded, f"guarded routes: {sorted(guarded)}")
    # /signals/explicit is the seeder's structured write. It is left open on
    # purpose (AUTOSEED and the offline double need it), and this pins that
    # decision so it is a choice rather than an oversight. Revisit it before
    # exposing a real deploy.
    check("POST /signals/explicit is knowingly left unguarded (seeder path)",
          ("/signals/explicit", "POST") not in guarded,
          "it is now guarded; update this check and the docs together")

    # The frontend sends the same variable name the backend reads, so setting
    # it once on both services is the whole configuration story.
    src = (REPO_ROOT / "frontend" / "app.py").read_text()
    check("frontend reads API_KEY and sends it as X-API-Key",
          'os.getenv("API_KEY")' in src and "X-API-Key" in src,
          "frontend does not forward the write key")

    # /demo/reset deletes every bank. It is the most destructive route in the
    # app, so it must be behind the same write key as ingestion -- a demo
    # convenience is not a good enough reason to let an open server wipe
    # memory. In THIS process the flag is off (the suite does not set it), so
    # the route is absent by design; the enabled-mode assertions below run it
    # in a subprocess and check the wiring there.
    check("POST /demo/reset is absent by default, not merely blocked",
          not any(r.path == "/demo/reset" for r in rt.router.routes),
          "the route is registered with the flag off")

    # The UI must warn before it offers the button, and must not offer it on
    # one click. A destructive control behind a single unconfirmed press is how
    # a demo loses its data on stage.
    check("the UI warns that logging a signal writes to the bank",
          "writes to the seeded memory bank" in src,
          "no warning about the write in the ingestion expander")
    check("the UI offers a reset action",
          "/demo/reset" in src, "no reset call in the UI")
    check("the reset is behind an explicit confirmation, not one click",
          "I understand this deletes all stored memory" in src,
          "the destructive button is not gated")
    check("the UI documents the offline reset command",
          "scripts/seed_data.py --reset --verify" in src,
          "the CLI reset path is not surfaced in the UI")
    check("the UI hides the reset control unless the backend enables it",
          "demo_reset_enabled" in src,
          "the reset button is shown without asking the backend")

    # The route must not merely refuse when disabled -- it must not exist.
    # A present-but-403 endpoint invites retries with different parameters,
    # and "not found" is the honest description of an absent capability.
    # Asserted below over real HTTP, so the status code clients actually get
    # is what is under test, not the route table.
    import os as _os
    import subprocess
    import urllib.error
    import urllib.request

    def _routes_with(env: dict) -> list[str]:
        code = (
            "import sys; sys.path.insert(0,'.')\n"
            "from backend import routes\n"
            "print([r.path for r in routes.router.routes])\n"
        )
        _e = dict(_os.environ)
        _e.update(env)
        res = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, env=_e,
        )
        if res.returncode != 0:
            return [f"ERROR: {res.stderr.strip().splitlines()[-1] if res.stderr else '?'}"]
        return eval(res.stdout.strip().splitlines()[-1])

    _off = _routes_with({"ENABLE_DEMO_RESET": "0"})
    _on = _routes_with({"ENABLE_DEMO_RESET": "1"})
    check("ENABLE_DEMO_RESET unset/0: POST /demo/reset does not exist (404)",
          "/demo/reset" not in _off, f"routes: {_off}")
    check("ENABLE_DEMO_RESET=1: POST /demo/reset is registered",
          "/demo/reset" in _on, f"routes: {_on}")
    check("the other routes are unaffected by the flag",
          set(_off) == set(_on) - {"/demo/reset"},
          f"off={sorted(_off)} on={sorted(_on)}")
    check("the default is off with no env var at all",
          "/demo/reset" not in _routes_with({}),
          "the route exists without ENABLE_DEMO_RESET being set")

    # Both gates, independently. The write key alone is not enough: with
    # API_KEY unset (the local default) that check is a no-op, so a route
    # guarded only by it is effectively unguarded in the default config.
    _both = _routes_with({"ENABLE_DEMO_RESET": "1", "API_KEY": ""})
    check("ENABLE_DEMO_RESET=1 with no API_KEY still registers the route",
          "/demo/reset" in _both, f"routes: {_both}")

    # End to end, over HTTP. A dedicated double on its own port keeps the
    # suite's shared double untouched, and banks named competitor-alpha /
    # competitor-beta cannot exist in any real account, so a passing delete
    # is itself the proof that this path never left the loopback interface.
    #
    # BANKS is a module global, so a second serve() still reads the same dict:
    # the state is snapshotted and restored below, otherwise these cases would
    # quietly delete the fixtures that later checks in this same suite rely on.
    _saved_banks = dict(double.BANKS)
    _dbl = double.serve(0)
    _DP = _dbl.server_address[1]
    threading.Thread(target=_dbl.serve_forever, daemon=True).start()
    _port = [8860]

    def _seed_two() -> None:
        double.BANKS.clear()
        for _b in ("competitor-alpha", "competitor-beta"):
            double.BANKS[_b] = {
                "bank_id": _b, "name": _b, "facts": [],
                "created_at": double._now(), "last_write_at": double._now(),
                "enable_observations": True,
            }

    def _boot(flag: str, api_key: str):
        _port[0] += 1
        env = {
            **_os.environ,
            "HINDSIGHT_BASE_URL": f"http://127.0.0.1:{_DP}",
            "HINDSIGHT_API_KEY": "double-key",
            "GROQ_BASE_URL": f"http://127.0.0.1:{_DP}/v1/openai/v1",
            "GROQ_API_KEY": "double-key",
            "AUTOSEED": "0",
            "ENABLE_DEMO_RESET": flag,
            "API_KEY": api_key,
        }
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1",
             "--port", str(_port[0]), "--log-level", "warning"],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(150):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{_port[0]}/health", timeout=1).read()
                return proc
            except Exception:  # noqa: BLE001
                time.sleep(0.2)
        proc.kill()
        raise SystemExit("selfcheck: uvicorn child did not boot")

    def _post_reset(port: int, key: str | None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/demo/reset", data=b"{}", method="POST",
            headers={"Content-Type": "application/json",
                     **({"X-API-Key": key} if key else {})},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def _get_health(port: int) -> dict:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as r:
            return json.loads(r.read())

    try:
        # Off: absent, so 404 and the banks are untouched.
        _seed_two()
        _p = _boot("0", "")
        try:
            _st, _body = _post_reset(_port[0], None)
            _hf = _get_health(_port[0])
            check("flag off over HTTP: POST /demo/reset is 404, not 401/403/200",
                  _st == 404, f"got HTTP {_st} {_body[:80]}")
            check("flag off over HTTP: /health reports the route unavailable",
                  _hf.get("demo_reset_enabled") is False, f"health: {_hf}")
        finally:
            _p.kill(); _p.wait(); time.sleep(0.3)
        check("flag off: nothing was deleted", sorted(double.BANKS) ==
              ["competitor-alpha", "competitor-beta"], f"banks: {sorted(double.BANKS)}")

        # On, no key configured: it works locally, and really deletes.
        _seed_two()
        _p = _boot("1", "")
        try:
            _st, _body = _post_reset(_port[0], None)
            _hf = _get_health(_port[0])
            _count = json.loads(_body).get("count") if _st == 200 else None
            check("flag on, no key: POST /demo/reset is 200 and deletes the banks",
                  _st == 200 and _count == 2 and not double.BANKS,
                  f"HTTP {_st} count={_count} banks={sorted(double.BANKS)}")
            check("flag on over HTTP: /health reports the route available",
                  _hf.get("demo_reset_enabled") is True, f"health: {_hf}")
        finally:
            _p.kill(); _p.wait(); time.sleep(0.3)

        # On, key configured, wrong key: the second gate still holds and the
        # deletion does not happen.
        _seed_two()
        _p = _boot("1", "correct-key")
        try:
            _st, _body = _post_reset(_port[0], "wrong-key")
            check("flag on, wrong key: POST /demo/reset is 401",
                  _st == 401, f"got HTTP {_st} {_body[:80]}")
        finally:
            _p.kill(); _p.wait(); time.sleep(0.3)
        check("flag on, wrong key: the banks survived the rejected call",
              sorted(double.BANKS) == ["competitor-alpha", "competitor-beta"],
              f"banks: {sorted(double.BANKS)}")

        # And the correct key is accepted, so the 401 is the key and not a
        # route that is simply broken.
        _seed_two()
        _p = _boot("1", "correct-key")
        try:
            _st, _body = _post_reset(_port[0], "correct-key")
            check("flag on, correct key: POST /demo/reset is 200 and deletes",
                  _st == 200 and not double.BANKS,
                  f"HTTP {_st} {_body[:80]} banks={sorted(double.BANKS)}")
        finally:
            _p.kill(); _p.wait(); time.sleep(0.3)
    finally:
        _dbl.shutdown()
        double.BANKS.clear()
        double.BANKS.update(_saved_banks)

    # /health advertises the capability, which is what the UI keys off.
    check("HealthResponse exposes demo_reset_enabled",
          "demo_reset_enabled" in (REPO_ROOT / "backend" / "models.py").read_text(),
          "the flag is not in the health schema")
    _health_src = (REPO_ROOT / "backend" / "routes.py").read_text()
    check("the health handler reports the flag",
          "demo_reset_enabled=ENABLE_DEMO_RESET" in _health_src,
          "/health does not report demo_reset_enabled")

    # The deploy default must be off, with the reason next to it.
    _render = (REPO_ROOT / "render.yaml").read_text()
    check("render.yaml ships ENABLE_DEMO_RESET=0",
          re.search(r"ENABLE_DEMO_RESET\s*\n\s*value:\s*[\"']?0", _render) is not None,
          "ENABLE_DEMO_RESET is not pinned to 0 in render.yaml")
    check("render.yaml explains the flag is demo-only",
          "throwaway demo" in _render or "demo-only" in _render,
          "no rationale next to the flag")

    # The enabled-but-keyless combination is the one where only a single gate
    # is doing any work: the route exists and nothing is checking who is
    # asking. It is allowed -- that is the local single-operator case -- but
    # never silently, so bootstrap has to say so out loud.
    def _boot_log(flag: str, api_key: str) -> str:
        env = {**_os.environ,
               "HINDSIGHT_BASE_URL": "http://127.0.0.1:9", "HINDSIGHT_API_KEY": "k",
               "GROQ_API_KEY": "k", "AUTOSEED": "0",
               "ENABLE_DEMO_RESET": flag, "API_KEY": api_key}
        r = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0,'.')\n"
             "from backend.main import bootstrap\n"
             "bootstrap()\n"],
            capture_output=True, text=True, env=env,
        )
        return r.stdout + r.stderr

    _nokey = _boot_log("1", "")
    check("reset enabled with no API_KEY warns that the route is unauthenticated",
          "UNAUTHENTICATED" in _nokey and "/demo/reset" in _nokey,
          f"bootstrap said: {_nokey[-260:]!r}")
    check("that warning names the fix, not just the risk",
          "API_KEY" in _nokey and "ENABLE_DEMO_RESET=0" in _nokey,
          f"bootstrap said: {_nokey[-260:]!r}")
    _withkey = _boot_log("1", "a-key")
    check("reset enabled with a key also warns, naming the key-holding blast radius",
          "write key" in _withkey,
          f"bootstrap said: {_withkey[-260:]!r}")
    _off_log = _boot_log("0", "a-key")
    check("reset disabled logs no reset warning at all",
          "/demo/reset" not in _off_log and "ENABLE_DEMO_RESET=1" not in _off_log,
          f"bootstrap said: {_off_log[-260:]!r}")

    # --reset has to mean "wipe the banks", not "delete the banks the seed file
    # happens to mention". An earlier version iterated the seed file, so a
    # bank created by anything else survived a command that printed "reset"
    # and exited 0 -- which is how a stray probe bank stayed in a real
    # account through a full reset. Run the real cmd_reset against the double
    # with one seeded bank and one bank the seed file never mentions.
    _seed_dirty = dict(double.BANKS)
    _seeder_src = (REPO_ROOT / "scripts" / "seed_data.py").read_text()
    check("cmd_reset lists banks instead of trusting the seed file",
          "list_banks()" in _seeder_src.split("def cmd_reset")[1].split("def ")[0],
          "cmd_reset does not enumerate existing banks")
    check("cmd_reset names the banks it is about to delete that are not seeded",
          "not in the seed file" in _seeder_src,
          "stray banks are destroyed without being announced first")
    _cd = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0,'.')\n"
         "from backend.hindsight_client import bank_id_for\n"
         "print(bank_id_for('Vertex Cloud'))\n"],
        capture_output=True, text=True, env=dict(_os.environ),
    )
    _seeded_bank = _cd.stdout.strip().splitlines()[-1] if _cd.returncode == 0 else "?"
    double.BANKS.clear()
    double.BANKS[_seeded_bank] = {
        "bank_id": _seeded_bank, "name": "Vertex Cloud", "facts": [],
        "created_at": double._now(), "last_write_at": double._now(),
        "enable_observations": True,
    }
    double.BANKS["competitor-curl-probe-co"] = {
        "bank_id": "competitor-curl-probe-co", "name": "curl-probe-co", "facts": [],
        "created_at": double._now(), "last_write_at": double._now(),
        "enable_observations": True,
    }
    _grouped_stub = {"Vertex Cloud": []}
    _reset_out = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0,'.')\n"
         "import scripts.seed_data as sd\n"
         "sd.cmd_reset({'Vertex Cloud': []})\n"],
        capture_output=True, text=True,
        env={**_os.environ, "HINDSIGHT_BASE_URL": BASE, "HINDSIGHT_API_KEY": "double-key"},
    )
    check("cmd_reset deletes a bank the seed file never mentions",
          "competitor-curl-probe-co" not in double.BANKS,
          f"banks left: {sorted(double.BANKS)} | {_reset_out.stdout[-200:]}"
          f"{_reset_out.stderr[-200:]}")
    check("cmd_reset still deletes the seeded banks", not double.BANKS,
          f"banks left: {sorted(double.BANKS)}")
    check("cmd_reset announces the stray bank before deleting it",
          "competitor-curl-probe-co" in _reset_out.stdout
          and "not in the seed file" in _reset_out.stdout,
          f"stdout: {_reset_out.stdout[-240:]!r}")
    double.BANKS.clear()
    double.BANKS.update(_seed_dirty)


if __name__ == "__main__":
    raise SystemExit(main())
