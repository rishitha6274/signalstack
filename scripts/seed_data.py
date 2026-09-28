#!/usr/bin/env python3
"""Load data/seed_signals.json into Hindsight memory banks.

Run this BEFORE the demo so the timeline is already six months deep:

    python scripts/seed_data.py              # seed (idempotent, safe to re-run)
    python scripts/seed_data.py --reset      # delete our banks first, then seed
    python scripts/seed_data.py --verify     # read back the stored timelines

Idempotency: each signal is retained with a deterministic document_id derived
from competitor+date+type, so re-running replaces those documents rather than
duplicating them. --reset is belt-and-braces for a clean slate.

The API key is never written to disk by this script; it reads the environment
(.env is loaded by backend.config).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend import hindsight_client
from backend.ingestion import load_seed_file  # noqa: E402
from backend.config import (  # noqa: E402
    SEED_FILE,
    hindsight_configured,
    missing_config_report,
)
from backend.models import Signal  # noqa: E402

RESET = "\033[0m"
BOLD = "\033[1m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"


def load_signals(path: Path) -> dict[str, list[Signal]]:
    """Thin wrapper — the real parser lives in backend.ingestion so the API can
    seed itself on boot and there is only one implementation to keep correct."""
    try:
        return load_seed_file(path)
    except FileNotFoundError as exc:
        raise SystemExit(str(exc)) from exc


def cmd_reset(grouped: dict[str, list[Signal]]) -> None:
    for name in grouped:
        deleted = hindsight_client.client.delete_bank(name)
        status = "deleted" if deleted else "not present"
        print(f"  {CYAN}reset{RESET} {name}: {status}")


def cmd_seed(grouped: dict[str, list[Signal]]) -> bool:
    ok = True
    for name, signals in grouped.items():
        bank_id = hindsight_client.bank_id_for(name)
        print(f"\n{BOLD}{name}{RESET} -> bank {CYAN}{bank_id}{RESET} ({len(signals)} signals)")
        started = time.time()
        try:
            result = hindsight_client.client.write_signals(signals)
        except hindsight_client.HindsightError as exc:
            print(f"  {YELLOW}failed{RESET}: {exc}")
            ok = False
            continue

        if result["errors"]:
            ok = False
            for error in result["errors"]:
                print(f"  {YELLOW}partial{RESET}: {error}")
        print(
            f"  {GREEN}retained{RESET} {result['written']}/{len(signals)} "
            f"signals in {time.time() - started:.1f}s"
        )
    return ok


def cmd_verify(grouped: dict[str, list[Signal]]) -> bool:
    """Read every timeline back and compare it to the file.

    Returns False on any mismatch so drift fails the command instead of
    scrolling past as a yellow word. This check earns its keep: editing a
    seeded signal's *date* changes its uid, and the uid is the Hindsight
    document_id, so a plain re-seed adds the edited row and leaves the
    original behind as an orphan. The stored timeline then reads 13 signals
    where the file has 7, and -- because the orphans look like a deliberate
    "announce, then reinforce a week later" cadence -- the synthesis will
    confidently report a pattern built out of stale rows. --reset fixes it.
    """
    print(f"\n{BOLD}Read back from Hindsight{RESET} (chronological-complete retrieval)\n")
    clean = True
    for name, expected in grouped.items():
        try:
            stored = hindsight_client.client.get_timeline(name)
        except hindsight_client.HindsightError as exc:
            print(f"  {name}: {YELLOW}error{RESET} {exc}")
            clean = False
            continue
        matched = len(stored) == len(expected)
        clean = clean and matched
        flag = f"{GREEN}OK{RESET}" if matched else f"{YELLOW}MISMATCH{RESET}"
        print(f"  {BOLD}{name}{RESET}: {len(stored)}/{len(expected)} signals  {flag}")
        if not matched:
            expected_uids = {s.uid for s in expected}
            orphans = [s for s in stored if s.uid not in expected_uids]
            if orphans:
                print(
                    f"    {YELLOW}{len(orphans)} signal(s) in memory are not in the file"
                    f" (most likely orphaned by a date edit):{RESET}"
                )
                for signal in orphans:
                    print(f"      {signal.date}  {signal.signal_type:<10} {signal.summary[:76]}")
                print(f"    {YELLOW}run with --reset to rebuild this bank{RESET}")
            else:
                print(f"    {YELLOW}memory is missing signals the file defines; re-run{RESET}")
        for signal in stored:
            print(f"    {signal.date}  {signal.signal_type:<10} {signal.summary[:88]}")
    return clean


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true", help="delete our banks before seeding")
    parser.add_argument("--verify", action="store_true", help="read the stored timelines back")
    parser.add_argument("--seed-file", type=Path, default=SEED_FILE)
    args = parser.parse_args()

    if not hindsight_configured():
        print(f"{YELLOW}Hindsight is not configured.{RESET}")
        for problem in missing_config_report():
            print(f"  - {problem}")
        print("\nCopy .env.example to .env, add your keys, and try again.")
        return 1

    print(f"{BOLD}Signal Stack seeder{RESET}")
    print(f"  Hindsight: {hindsight_client.client.base_url}")
    print(f"  Seed file: {args.seed_file}")

    grouped = load_signals(args.seed_file)
    total = sum(len(v) for v in grouped.values())
    print(f"  Loaded {total} signals across {len(grouped)} competitors")
    for name, signals in grouped.items():
        print(f"    {name}: {len(signals)} signals, {signals[0].date} -> {signals[-1].date}")

    if args.reset:
        print(f"\n{BOLD}Reset{RESET}")
        cmd_reset(grouped)

    print(f"\n{BOLD}Seeding{RESET}")
    ok = cmd_seed(grouped)

    if args.verify or ok:
        # A read-back mismatch is a real failure, not advice: it means memory
        # and the file have diverged, and synthesis will reason over whatever
        # is actually stored there.
        verified = cmd_verify(grouped)
        ok = ok and verified

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
