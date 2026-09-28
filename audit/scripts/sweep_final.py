"""The one additional live sweep, after the three audited defects were fixed.

Outside the repository on purpose: a one-shot verification harness, not something
the project should carry. One pass, ten competitors, in name order. No second
pass and no sweep of our own retries -- the only retry here is honouring a 429
by waiting exactly as long as the server says and asking once more, because
retrying into an exhausted per-minute window is what produced the stub reads
the first audit found.

Two things distinguish it from the previous harness:

- Spacing. The calls are 10s apart rather than 2s. Each read is one synthesis,
  which may itself be one corrective retry, so ten competitors is 10-20 calls
  against a per-minute window; the gap is what keeps the sweep off the wall.
- Retry-After. If the API hands back a 429 with a Retry-After, the harness
  sleeps for that long (or 30s if it says nothing) and asks once more, and
  records that it had to. It never shortens the wait.

Responses land in /tmp/audit/final/, the post-defect-fix sweep. The earlier
post-fix sweep is preserved untouched at /tmp/audit/after-pre-fix/, and the
pre-hardening baseline at /tmp/audit/before/.
"""
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "http://127.0.0.1:8000"
OUT = Path("/tmp/audit/final")
GAP = 10.0          # seconds between competitors
FALLBACK_WAIT = 30.0


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def competitors() -> list[str]:
    with urllib.request.urlopen(f"{API}/competitors", timeout=180) as fh:
        data = json.load(fh)
    rows = data["competitors"] if isinstance(data, dict) and "competitors" in data else data
    return [r["name"] for r in sorted(rows, key=lambda r: r["name"])]


def synthesize(name: str) -> tuple[dict, int]:
    """One synthesis. Returns (read, waited_flag) and honours a 429 once."""
    req = urllib.request.Request(
        f"{API}/synthesize",
        data=json.dumps({"competitor": name}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=900) as fh:
                return json.load(fh), attempt - 1
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt == 2:
                raise
            wait = FALLBACK_WAIT
            raw = exc.headers.get("Retry-After") if exc.headers else None
            if raw:
                try:
                    wait = max(wait, float(raw))
                except ValueError:
                    pass
            print(f"    429 from the API: waiting {wait:.0f}s as asked, then one more try")
            time.sleep(wait)
    raise RuntimeError("unreachable")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    names = competitors()
    print(f"sweeping {len(names)} competitors, {GAP:.0f}s apart, one pass\n" + "=" * 78)
    summary = []
    for i, name in enumerate(names):
        started = time.time()
        try:
            read, waited = synthesize(name)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            print(f"### {name}: FAILED after {time.time() - started:.0f}s: {exc}")
            summary.append({"competitor": name, "error": str(exc)})
            continue
        (OUT / f"{slug(name)}.json").write_text(
            json.dumps(read, indent=2, ensure_ascii=False))
        summary.append({
            "competitor": name,
            "confidence": read.get("confidence"),
            "signals": read.get("signal_count"),
            "overdue": read.get("days_overdue"),
            "staleness": read.get("evidence_staleness"),
            "narrative_withheld": read.get("narrative_withheld"),
            "model_used": read.get("model_used"),
            "waited_on_429": bool(waited),
            "seconds": round(time.time() - started, 1),
        })
        flag = " WITHHELD" if read.get("narrative_withheld") else ""
        print(f"{name:<20} conf={str(read.get('confidence')):<7} "
              f"signals={read.get('signal_count'):<3} "
              f"overdue={str(read.get('days_overdue')):<4}{flag} "
              f"({time.time() - started:.0f}s)")
        if i < len(names) - 1:
            time.sleep(GAP)

    (OUT / "_sweep.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    print("=" * 78)
    print(f"saved {len(summary)} responses to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
