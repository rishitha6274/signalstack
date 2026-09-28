"""The single final live sweep: all ten competitors, one pass, no retries of our own.

Deliberately outside the repository: this is a one-shot verification harness, not
something the project should carry. Each response is written to /tmp/audit/after/
for the auditor, and the raw log of validator decisions comes from the API.
"""
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "http://127.0.0.1:8000"
OUT = Path("/tmp/audit/after")


def competitors() -> list[str]:
    with urllib.request.urlopen(f"{API}/competitors", timeout=60) as fh:
        data = json.load(fh)
    rows = data["competitors"] if isinstance(data, dict) and "competitors" in data else data
    return [r["name"] for r in sorted(rows, key=lambda r: r["name"])]


def synthesize(name: str) -> dict:
    req = urllib.request.Request(
        f"{API}/synthesize",
        data=json.dumps({"competitor": name}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=600) as fh:
        return json.load(fh)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    names = competitors()
    print(f"sweeping {len(names)} competitors\n" + "=" * 78)
    summary = []
    for name in names:
        started = time.time()
        try:
            read = synthesize(name)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(f"### {name}: FAILED after {time.time() - started:.0f}s: {exc}")
            summary.append({"competitor": name, "error": str(exc)})
            continue
        path = OUT / f"{name.replace(' ', '_')}.json"
        path.write_text(json.dumps(read, indent=2, ensure_ascii=False))
        summary.append(
            {
                "competitor": name,
                "confidence": read.get("confidence"),
                "signals": read.get("signal_count"),
                "overdue": read.get("days_overdue"),
                "staleness": read.get("evidence_staleness"),
                "model_used": read.get("model_used"),
                "seconds": round(time.time() - started, 1),
            }
        )
        print(
            f"{name:<20} conf={str(read.get('confidence')):<7} "
            f"signals={read.get('signal_count'):<3} "
            f"overdue={str(read.get('days_overdue')):<4} "
            f"({time.time() - started:.0f}s)"
        )
        time.sleep(2)  # stay well inside the per-minute window

    (OUT / "_sweep.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    print("=" * 78)
    print(f"saved {len(summary)} responses to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
