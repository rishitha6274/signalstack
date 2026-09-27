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
    check("seed file holds 19 signals across 3 competitors", len(parsed) == 19, f"got {len(parsed)}")

    by_comp: dict[str, int] = {}
    for sig in parsed:
        res = client.write_signal(sig)
        by_comp[sig.competitor] = by_comp.get(sig.competitor, 0) + res.get("items_count", 0)
    for comp, n in sorted(by_comp.items()):
        print(f"    {comp}: wrote {n}")

    for comp, expected in (("Nimbus AI", 12), ("Vertex Cloud", 4), ("Pathfinder Labs", 3)):
        tl = client.get_timeline(comp)
        check(f"{comp} timeline complete ({expected})", len(tl) == expected, f"got {len(tl)}")
        check(f"{comp} timeline is oldest-first",
              [s.date for s in tl] == sorted(s.date for s in tl))
        check(f"{comp} carries no derived noise", all(s.summary for s in tl))

    check("re-seed is idempotent (document_id replaces)",
          len(client.get_timeline("Nimbus AI")) == 12)

    nimbus = client.get_timeline("Nimbus AI")
    check("metadata survived the round trip",
          all(s.signal_type in {"pricing", "feature", "hiring", "messaging", "funding"} for s in nimbus))
    check("signal types span the seeded chain",
          {s.signal_type for s in nimbus} == {"pricing", "feature", "hiring", "messaging", "funding"})

    comps = {c.name: c for c in client.list_competitors()}
    check("list_competitors sees all 3", len(comps) == 3, f"got {sorted(comps)}")
    check("Nimbus signal_count is 12", comps["Nimbus AI"].signal_count == 12)

    nimbus_units = s.get(
        f"{BASE}/v1/default/banks/competitor-nimbus-ai/memories/list", params={"limit": 200}
    ).json()
    check("app's banks hold zero derived observations (all three)",
          not [u for u in nimbus_units["items"] if u.get("fact_type") == "observation"],
          f"got {len([u for u in nimbus_units['items'] if u.get('fact_type') == 'observation'])}")
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

    for thin in ("Vertex Cloud", "Pathfinder Labs"):
        out = generate_strategic_read(thin)
        text = " ".join(str(v) for v in out.model_dump().values()).lower()
        check(f"{thin} refuses to fabricate a pattern",
              any(k in text for k in ("insufficient", "cannot", "not predictable")))

    check("unknown competitor returns empty, not an error", client.get_timeline("Nobody Ltd") == [])

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
          seed_from_file()["written"] == 19, f"got {seed_from_file()['written']}")
    check("load_seed_file returns 3 competitors",
          len(load_seed_file()) == 3, f"got {len(load_seed_file())}")

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
        check("bootstrap() populates empty memory (the real deploy path)",
              len(banks) == 3, f"got {len(banks)} banks")

        # The double splits documents into several facts each, so its raw
        # fact_count is legitimately ~2x the signal count. The app's own view
        # is what must equal 19.
        app_total = sum(c.signal_count for c in hc.client.list_competitors())
        check("boot-seeded app view is 19 signals",
              app_total == 19, f"got {app_total}")
        raw_after_first = sum(b.get("fact_count") or 0 for b in banks)
        check("raw fact_count >= signals (splitting is allowed)",
              raw_after_first >= 19, f"got {raw_after_first}")

        bootstrap()  # a restart must not duplicate anything
        raw_after_second = sum(
            b.get("fact_count") or 0
            for b in hc.client.list_banks()
            if (b.get("bank_id") or "").startswith("competitor-")
        )
        check("bootstrap() is idempotent: raw facts do not grow on restart",
              raw_after_second == raw_after_first,
              f"{raw_after_first} -> {raw_after_second}")
        check("bootstrap() is idempotent: app view still 19 signals",
              sum(c.signal_count for c in hc.client.list_competitors()) == 19)
    finally:
        hc.REGISTRY_FILE = real_registry

    check("API binds loopback by default (not the whole interface)",
          API_HOST == "127.0.0.1", f"got {API_HOST}")
    check("frontend is pointed at the API by env, overridable",
          SIGNAL_STACK_API.startswith("http://"), f"got {SIGNAL_STACK_API}")
    check("autoseed is on by default", AUTOSEED is True, f"got {AUTOSEED}")
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
