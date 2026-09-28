"""Pydantic schemas for Signal Stack."""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

from .config import SIGNAL_TYPES

SignalType = Literal["pricing", "feature", "hiring", "messaging", "funding"]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def slugify_competitor(name: str) -> str:
    """'Nimbus AI' -> 'nimbus-ai'. Used for the Hindsight bank id."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "unknown"


def signal_uid(competitor: str, date_str: str, signal_type: str) -> str:
    """Stable id for a signal.

    Stability matters for two reasons: it is the Hindsight ``document_id``
    (so re-running the seeder replaces rather than duplicates), and it is the
    dedupe key when Hindsight's fact extractor splits one retained blob into
    more than one memory unit.
    """
    basis = f"{slugify_competitor(competitor)}|{date_str}|{signal_type}"
    digest = hashlib.sha1(basis.encode("utf-8")).hexdigest()[:10]
    return f"{slugify_competitor(competitor)}-{date_str}-{signal_type}-{digest}"


def normalise_date(value: str) -> str:
    """Coerce assorted date shapes to ISO ``YYYY-MM-DD``.

    Accepts ``2026-03-04``, ``03/04/2026``, ``4 March 2026``,
    ``2026-03-04T11:02:00Z``, ``March 4, 2026`` and ``today``.

    ``03/04/2026`` is genuinely ambiguous. It is read as US month-first
    (March 4), since these notes are analyst-typed, and only falls back to
    day-first when day-first is the only reading that works — ``13/04/2026``
    can only be 13 April. Real ambiguity in prose is best avoided by the
    extraction prompt asking for ISO, which it does.
    """
    raw = (value or "").strip()
    if not raw:
        raise ValueError("empty date")
    if raw.lower() in {"today", "now", "current"}:
        return date.today().isoformat()

    # Already ISO-ish.
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        pass

    # Collapse every separator to "-" so one format list covers 2026/03/04,
    # 03.04.2026 and 03-04-2026 alike. strptime's literal separators are
    # matched exactly, so without this a dash-joined candidate silently fails
    # every slash format.
    cleaned = re.sub(r"[./]", "-", raw).strip()
    for fmt in ("%Y-%m-%d", "%m-%d-%Y", "%d-%m-%Y", "%d %B %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(cleaned, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"unrecognised date: {value!r}")


# --------------------------------------------------------------------------
# Signal
# --------------------------------------------------------------------------
class Signal(BaseModel):
    """One tracked competitor event. The atom of Signal Stack's memory."""

    competitor: str
    date: str  # ISO YYYY-MM-DD
    signal_type: SignalType
    summary: str
    raw_notes: str = ""
    source: str = "manual entry"

    @field_validator("date", mode="before")
    @classmethod
    def _coerce_date(cls, v):
        return normalise_date(v if isinstance(v, str) else str(v))

    @field_validator("signal_type", mode="before")
    @classmethod
    def _normalise_type(cls, v):
        value = str(v or "").strip().lower()
        aliases = {
            "price": "pricing",
            "prices": "pricing",
            "pricing_change": "pricing",
            "feature_launch": "feature",
            "product": "feature",
            "release": "feature",
            "job": "hiring",
            "jobs": "hiring",
            "hiring_spike": "hiring",
            "headcount": "hiring",
            "message": "messaging",
            "positioning": "messaging",
            "marketing": "messaging",
            "fund": "funding",
            "investment": "funding",
            "raise": "funding",
        }
        value = aliases.get(value, value)
        if value not in SIGNAL_TYPES:
            raise ValueError(
                f"signal_type must be one of {list(SIGNAL_TYPES)} (got {v!r})"
            )
        return value

    @property
    def slug(self) -> str:
        return slugify_competitor(self.competitor)

    @property
    def uid(self) -> str:
        return signal_uid(self.competitor, self.date, self.signal_type)

    def as_memory_content(self) -> str:
        """Render the signal as the text we hand to Hindsight's extractor.

        Written as one declarative sentence because Hindsight extracts *facts*
        from retained content, and a single fact per signal is what keeps the
        timeline one-entry-per-signal instead of fragmenting.
        """
        return (
            f"On {self.date}, {self.competitor} ({self.signal_type}): "
            f"{self.summary.rstrip('.')}. Source: {self.source}."
        )


# --------------------------------------------------------------------------
# Ingestion
# --------------------------------------------------------------------------
class SignalIngestRequest(BaseModel):
    competitor: str = Field(..., min_length=1)
    raw_text: str = Field(..., min_length=1)

    @field_validator("competitor", "raw_text")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


# --------------------------------------------------------------------------
# Competitor
# --------------------------------------------------------------------------
class CompetitorOut(BaseModel):
    """A competitor plus the visible size of its accumulated memory."""

    name: str
    slug: str
    bank_id: str
    signal_count: int = 0
    fact_count: int = 0
    last_write_at: Optional[str] = None
    first_signal: Optional[str] = None
    last_signal: Optional[str] = None


class CompetitorCreate(BaseModel):
    name: str = Field(..., min_length=1)


# --------------------------------------------------------------------------
# Synthesis
# --------------------------------------------------------------------------
class SynthesisRequest(BaseModel):
    competitor: str


class Confidence(str, Enum):
    """How much weight the read's own conclusion can bear.

    "none" is reserved for a refusal. It is not a synonym for "low": a refusal
    asserts nothing, whereas a low-confidence read asserts something weakly, and
    the UI renders them differently because the difference is the point of the
    exercise — an honest "not enough signal" has more value than a confident
    guess, and more value than a timid guess.
    """

    high = "high"
    medium = "medium"
    low = "low"
    none = "none"


class SynthesisResponse(BaseModel):
    competitor: str
    patterns: str
    inferred_intent: str
    predicted_next_move: str
    recommendation: str
    # Calibration. The audit found every one of the ten reads stating a dated
    # forecast with no indication of how much to trust it, and none naming the
    # observation that would settle the question. These two fields close that
    # gap, and are enforced by backend.validators rather than trusted to the
    # prompt.
    confidence: Confidence = Confidence.none
    missing_evidence: str = ""
    # True when the model was asked twice, failed the validators both times, and
    # the narrative was withheld in favour of the deterministic read. The
    # response is still a 200 and still carries the measured facts — this flag
    # is how the UI and the audit tell that apart from a read the model wrote
    # and passed, or a refusal it wrote on purpose.
    narrative_withheld: bool = False
    # Provenance: what the read was built from, so the UI can be honest about it.
    signal_count: int = 0
    timeline_window: str = ""
    model_used: str = ""
    # Evidence freshness. A read built from a timeline whose last signal predates
    # today is still a valid read, but the reader has to be told how far behind
    # the evidence is or they will read a stale forecast as a live one.
    data_as_of: str = ""  # date of the most recent signal, ISO
    evidence_age_days: Optional[int] = None  # whole days between that and today
    evidence_staleness: str = ""  # fresh | aging | stale | unknown
    # How far past its own rhythm the last signal sits. Distinct from
    # evidence_age_days: 40 days is unremarkable for a quarterly competitor and
    # overdue for a weekly one.
    days_overdue: int = 0


# --------------------------------------------------------------------------
# API envelopes
# --------------------------------------------------------------------------
class HealthResponse(BaseModel):
    status: str
    hindsight_configured: bool
    groq_configured: bool
    problems: list[str] = []


class TimelineResponse(BaseModel):
    competitor: str
    bank_id: str
    signal_count: int
    retrieval: str = "chronological-complete"
    signals: list[Signal]
