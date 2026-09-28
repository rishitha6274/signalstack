"""Render the four-read verification report from saved responses.

Re-derives the verdict from the validators themselves rather than from the
harness's own word scan, which flagged negated disclosures ("no transition
repeats") as unlicensed claims and made two clean reads look dirty.
"""
import json
import sys
from datetime import date
from pathlib import Path

REPO = Path("/Users/rishithal/Documents/SignalStack")
sys.path.insert(0, str(REPO))

from backend import hindsight_client  # noqa: E402
from backend.facts import build_facts  # noqa: E402
from backend.synthesis import evidence_clock, is_refusal  # noqa: E402
from backend import validators as V  # noqa: E402

TODAY = date(2026, 9, 28)
RUNS = {  # expected status, and the commit each read was actually served by
    "palisade-security": ("narrative", "3963eb4"),
    "ferrous-systems":   ("narrative", "3963eb4"),
    "nimbus-ai":         ("narrative", "2b150a7"),
    "vertex-cloud":      ("refusal",   "2b150a7"),
}

rows = []
for slug, (expected, commit) in RUNS.items():
    d = json.load(open(f"/tmp/audit/four/{slug}.json"))
    name = d["competitor"]
    signals = hindsight_client.client.get_timeline(name)
    clock = evidence_clock(signals, today=TODAY)
    facts = build_facts(signals, today=clock.today, staleness=clock.staleness)
    timeline = " ".join(f"{s.date} {s.signal_type} {s.summary}" for s in signals)

    refused = is_refusal(d)
    problems = V.validate_response(d, facts, timeline, refused=refused)
    status = "refusal" if refused else "narrative"
    rows.append({
        "name": name, "expected": expected, "status": status,
        "as_expected": status == expected,
        "conf": d.get("confidence"), "refused_field": refused,
        "n": facts.n, "sufficient": facts.evidence_sufficient,
        "spread": round(facts.interval_spread, 2),
        "priced": sorted(facts.repeated_transitions()),
        "overdue": clock.age_days,
        "missing": d.get("missing_evidence", ""),
        "unnegated": V._unnegated_repeat_words(
            " ".join(str(d.get(k) or "") for k in
                     ("patterns", "inferred_intent", "predicted_next_move",
                      "recommendation"))),
        "problems": problems, "commit": commit,
        "model": d.get("model_used", "")[:40],
        "predicts": str(d.get("predicted_next_move") or ""),
    })

for r in rows:
    print("=" * 78)
    print(f"{r['name']}   expected={r['expected']}  got={r['status']}  "
          f"{'OK' if r['as_expected'] else 'MISMATCH'}")
    print(f"  n={r['n']}  spread={r['spread']}  sufficient={r['sufficient']}  "
          f"overdue={r['overdue']}d  priced repeats={r['priced'] or 'none'}")
    print(f"  confidence={r['conf']!r}  is_refusal={r['refused_field']}  "
          f"served by {r['commit']}")
    print(f"  local re-run: {'CLEAN' if not r['problems'] else r['problems']}")
    print(f"  unnegated repeat claims: {r['unnegated'] or 'none'}")
    print(f"  missing_evidence: {r['missing'][:110]}")
    print(f"  predicted_next_move: {r['predicts'][:110]}")

print("=" * 78)
for r in rows:
    print(f"{r['name']:<22} {r['status']:<10} conf={r['conf']:<8} "
          f"{'as expected' if r['as_expected'] else 'MISMATCH'}")
print(f"\n{sum(r['as_expected'] for r in rows)}/{len(rows)} as expected, "
      f"{sum(not r['problems'] for r in rows)}/{len(rows)} validator-clean")
