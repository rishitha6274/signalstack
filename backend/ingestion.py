"""Raw text -> structured Signal, then persist it to Hindsight.

The LLM does the extraction. If the LLM is unavailable or returns something
unusable, a keyword/heuristic extractor takes over so that logging a signal
during a live demo never dead-ends on a provider hiccup.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from pathlib import Path

from . import hindsight_client
from .config import SEED_FILE, SIGNAL_TYPES
from .llm_client import LLMError, client as llm_client
from .models import Signal, normalise_date

log = logging.getLogger("signal_stack.ingestion")

EXTRACTION_PROMPT = """You extract structured competitive-intelligence signals.

Read the raw note below about {competitor} and return ONLY a JSON object with
exactly these fields:
- "signal_type": one of {allowed}
- "date": the date of the event in ISO format YYYY-MM-DD. If the note states no
  date, use today's date ({today}).
- "summary": ONE sentence, under 30 words, stating what happened and the
  concrete detail (numbers, role names, plan names). No hedging.
- "source": where this came from (e.g. "pricing page", "job board", "press
  release", "sales call", "Slack"). If not stated, use "manual entry".

RAW NOTE:
---
{raw_text}
---

Rules:
- Output raw JSON only. No preamble, no explanation, no markdown fences.
- Do not invent a detail that is not in the note.
- If the note contains several events, describe the single most significant one.
"""


# ---------------------------------------------------------------------------
# Heuristic fallback
# ---------------------------------------------------------------------------
_TYPE_HINTS: list[tuple[str, tuple[str, ...]]] = [
    ("pricing", ("price", "pricing", "discount", "plan cost", "per seat", "$", "cheaper", "expensive", "raise", "billing")),
    ("funding", ("series a", "series b", "series c", "series d", "funding", "raised", "raise of", "valuation", "investment round", "led by")),
    ("hiring", ("hiring", "job post", "we are hiring", "careers", "open role", "recruit", "headcount", "headcount", "sde", "sales engineer", "aae")),
    ("messaging", ("homepage", "landing page", "tagline", "messaging", "reposition", "slogan", "campaign", "website copy", "announce", "positioning")),
    ("feature", ("launch", "launched", "shipped", "released", "ga", "general availability", "new feature", "beta", "added support")),
]

_DATE_PATTERNS = (
    re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b"),
    re.compile(r"\b(\d{4})/(\d{1,2})/(\d{1,2})\b"),
    re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"),
    re.compile(
        r"\b(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})\b",
        re.IGNORECASE,
    ),
)


def _guess_type(text: str) -> str:
    lowered = text.lower()
    best, best_hits = "feature", 0
    for signal_type, hints in _TYPE_HINTS:
        hits = sum(1 for hint in hints if hint in lowered)
        if hits > best_hits:
            best, best_hits = signal_type, hits
    return best


def _guess_date(text: str) -> str:
    for pattern in _DATE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        parts = match.groups()
        # Try the same orders normalise_date accepts, so the heuristic and the
        # LLM path never disagree about a date we could have read.
        for order in ((0, 1, 2), (1, 0, 2), (2, 1, 0)):
            try:
                candidate = "-".join(str(parts[i]).zfill(2) for i in order)
                return normalise_date(candidate)
            except (ValueError, IndexError):
                continue
    return date.today().isoformat()


def _first_sentence(text: str) -> str:
    cleaned = " ".join(text.split())
    match = re.search(r"^(.{15,240}?[.!?])(\s|$)", cleaned)
    if match:
        return match.group(1).strip()
    return (cleaned[:237] + "...") if len(cleaned) > 240 else cleaned


def heuristic_extract(raw_text: str, competitor: str) -> Signal:
    """Deterministic extraction used when the LLM path is unavailable."""
    return Signal(
        competitor=competitor,
        date=_guess_date(raw_text),
        signal_type=_guess_type(raw_text),
        summary=_first_sentence(raw_text),
        raw_notes=raw_text,
        source="manual entry (heuristic extraction)",
    )


# ---------------------------------------------------------------------------
# LLM extraction
# ---------------------------------------------------------------------------
def extract_signal_fields(raw_text: str, competitor: str) -> dict:
    """Ask the LLM for the structured fields. Raises LLMError on failure."""
    prompt = EXTRACTION_PROMPT.format(
        competitor=competitor,
        allowed=list(SIGNAL_TYPES),
        today=date.today().isoformat(),
        raw_text=raw_text.strip(),
    )
    parsed, model_used = llm_client.call_llm_json(prompt)
    log.info("extracted signal via %s", model_used)
    return parsed


def extract_signal(raw_text: str, competitor: str) -> Signal:
    """Raw note -> Signal, persisted into the competitor's Hindsight bank.

    Never raises for a bad LLM response: it falls back to the heuristic
    extractor. A wrong-but-present signal is recoverable in the UI; a 500 on
    the demo button is not.
    """
    competitor = competitor.strip()
    raw_text = raw_text.strip()

    try:
        fields = extract_signal_fields(raw_text, competitor)
        signal = Signal(
            competitor=competitor,
            date=fields.get("date") or date.today().isoformat(),
            signal_type=fields.get("signal_type") or "feature",
            summary=fields.get("summary") or _first_sentence(raw_text),
            raw_notes=raw_text,
            source=fields.get("source") or "manual entry",
        )
    except (LLMError, ValueError) as exc:
        log.warning("LLM extraction failed (%s); using heuristic extraction", exc)
        signal = heuristic_extract(raw_text, competitor)

    # raw_notes lives in Hindsight metadata, and the summary is what the
    # timeline shows — so make the LLM's summary the authoritative text.
    signal = signal.model_copy(update={"raw_notes": raw_text})
    hindsight_client.client.write_signal(signal)
    return signal


def load_seed_file(path: Path | None = None) -> dict[str, list[Signal]]:
    """Parse the seed file into {competitor: [Signal, ...]}, oldest first.

    Lives here rather than in scripts/ so the API can seed itself on boot and
    the CLI stays a thin wrapper over one implementation.
    """
    seed_path = path or SEED_FILE
    if not seed_path.exists():
        raise FileNotFoundError(f"Seed file not found: {seed_path}")

    with seed_path.open(encoding="utf-8") as fh:
        payload = json.load(fh)

    grouped: dict[str, list[Signal]] = {}
    for block in payload.get("competitors", []):
        signals = [Signal(**entry) for entry in block.get("signals", [])]
        signals.sort(key=lambda s: s.date)
        grouped[block["name"]] = signals
    return grouped


def seed_from_file(path: Path | None = None) -> dict[str, int]:
    """Write the whole seed dataset to Hindsight. Idempotent per signal.

    document_id is derived from competitor + date + type, so re-running
    replaces rather than duplicates. Returns totals for logging.
    """
    grouped = load_seed_file(path)
    written = 0
    errors: list[str] = []
    for name, signals in grouped.items():
        try:
            result = hindsight_client.client.write_signals(signals)
        except hindsight_client.HindsightError as exc:
            errors.append(f"{name}: {exc}")
            continue
        written += result["written"]
        errors.extend(f"{name}: {e}" for e in result["errors"])

    return {
        "written": written,
        "competitors": len(grouped),
        "errors": len(errors),
        "detail": "; ".join(errors),
    }
