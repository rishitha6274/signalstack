"""Timeline -> strategic narrative.

This is the differentiator, so it gets the disproportionate investment the
brief asks for. Two things make it work:

1. Complete context. The full chronological timeline is pulled from Hindsight
   (not a top-k recall slice) and rendered as a numbered, dated list, so the
   model can see the chain of events rather than the three most similar ones.

2. Grounding rules. The prompt forces every claim to cite a date that exists
   in the timeline, and forces an explicit "insufficient signal" answer when
   the timeline is too thin. That second rule is what makes the contrast demo
   work: asked about a competitor with four uncorrelated signals, the agent
   says so instead of manufacturing a strategy.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from datetime import date

from . import hindsight_client
from .llm_client import LLMError, client as llm_client
from .models import Signal, SynthesisResponse

log = logging.getLogger("signal_stack.synthesis")


# The prompt template mandated by the brief, used verbatim.
SYNTHESIS_PROMPT = """You are a competitive intelligence analyst. Below is the full chronological \
timeline of tracked signals for {competitor}:

{numbered_timeline}

TIME REFERENCE: Today is {today}. {age_sentence}

Analyze this timeline and return ONLY valid JSON with these fields:
- "patterns": recurring patterns or cadence you observe across signal types, citing specific dates
- "inferred_intent": what strategic intent likely explains these moves
- "predicted_next_move": a specific, falsifiable prediction of what they will likely do next
- "recommendation": one concrete action our team should take in response

Be specific. Reference actual signals and dates. Do not give generic advice."""

# Appended to the template above. Same instructions, plus the guardrails that
# keep the output honest about thin timelines.
GROUNDING_RULES = """

GROUNDING RULES (these override any habit of giving generic advice):
- Every claim must be anchored to a specific signal in the timeline above, cited
  by its date. A claim you cannot date is a claim you must not make.
- The signals above are stored in different signal types (pricing, feature,
  hiring, messaging, funding). Reason ACROSS types. The interesting finding is
  usually the *sequence* — e.g. a funding round followed weeks later by hiring
  in go-to-market, followed by a pricing move, followed by a messaging shift.
- If the timeline has fewer than 4 signals, or the signals show no repeated
  pattern and no ordering, you MUST say so. In "patterns" state that the
  evidence is insufficient and name what is missing. Do not invent a strategy
  to fill the shape of the answer. An honest "not enough signal yet" is the
  correct, high-value answer here.
- "predicted_next_move" must be falsifiable: name the action, rough timing, and
  the observable that would confirm or refute it. If you concluded in
  "patterns" that the evidence is insufficient, then "predicted_next_move" MUST
  say no prediction can be made and state what evidence would be needed. A
  hedged guess ("if they follow a typical quarterly cadence, they might...") is
  a failure of this rule: you cannot forecast a strategy you could not identify.
- "recommendation" must be one action, actionable this quarter, that follows
  from the pattern you actually found. If you found no pattern, the
  recommendation must be to keep collecting signals and state what to watch for
  — not a business decision premised on a strategy you invented.

TIME ANCHORING (you cannot infer the current date — it is given above):
- Forecast from TODAY, never from the last date in the timeline. When you write
  "within 2 weeks", you mean 2 weeks from today, not 2 weeks after the final
  logged signal. The timeline's final date is evidence, not the present.
- NEVER present a date that is already in the past as a future prediction. If
  the cadence you measured from the timeline would land on a date before today,
  that tells you the pattern has either already played out or gone quiet — say
  which, and forecast from today instead of naming a stale deadline.
- Respect the evidence-freshness note in the TIME REFERENCE line. If it says the
  evidence is stale, you may still predict, but "predicted_next_move" MUST state
  that it rests on evidence that has gone quiet, and "recommendation" MUST lead
  with the step that refreshes it (re-check the competitor's recent activity)
  before the step that acts on it. A confident, precisely-dated forecast built on
  stale evidence is a failure of this rule even when the underlying pattern is
  real and well-supported.

The four fields are one argument. If "patterns" reports insufficient evidence,
the other three must follow that conclusion rather than quietly contradicting it.

Output raw JSON only. No preamble, no markdown fences, no commentary outside the
JSON object."""


def format_timeline(signals: list[Signal]) -> str:
    """Render signals as a numbered, dated list — the model's whole context."""
    return "\n".join(
        f"{index}. [{signal.date}] ({signal.signal_type}) {signal.summary}"
        for index, signal in enumerate(signals, start=1)
    )


def timeline_window(signals: list[Signal]) -> str:
    if not signals:
        return "no signals recorded"
    if len(signals) == 1:
        return f"1 signal, {signals[0].date}"
    return f"{signals[0].date} to {signals[-1].date} ({len(signals)} signals)"


# --------------------------------------------------------------------------
# Evidence clock
# --------------------------------------------------------------------------
# The model is shown a timeline of dates and nothing else, so with no reference
# point it anchors every forecast to the *last logged signal*. When a
# competitor's data is three weeks old, that produces a confidently-worded
# prediction about a window that already closed — technically grounded in real
# signals, and still wrong in the way that matters to a reader.
#
# The fix is to hand the model the current date and tell it how far the evidence
# has drifted, using the timeline's own rhythm as the yardstick. "Stale" means
# "has gone quiet relative to how often this competitor normally speaks", not
# "is more than N days old" — a company that ships weekly and one that ships
# twice a year are not comparable against a fixed threshold.
@dataclass(frozen=True)
class EvidenceClock:
    """Where the timeline sits relative to today."""

    today: date
    as_of: str  # date of the most recent signal, ISO
    age_days: int  # today - as_of, clamped at 0
    cadence_days: int | None  # median gap between consecutive signals
    staleness: str  # fresh | aging | stale | unknown

    @property
    def age_sentence(self) -> str:
        """One line, in the model's own terms, telling it how current this is."""
        if not self.as_of:
            return "There are no signals on record yet."
        if self.staleness == "unknown" or self.cadence_days is None:
            return (
                f"The most recent signal is dated {self.as_of}, {self.age_days} day(s) ago. "
                "There is not enough history to judge how current that is."
            )
        rhythm = f"this competitor's usual ~{self.cadence_days}-day signal rhythm"
        if self.staleness == "fresh":
            return (
                f"The most recent signal is dated {self.as_of}, {self.age_days} day(s) ago — "
                f"in line with {rhythm}, so the evidence is current."
            )
        if self.staleness == "aging":
            return (
                f"The most recent signal is dated {self.as_of}, {self.age_days} day(s) ago, "
                f"which is past {rhythm} but not far past it. Treat the timeline as current "
                "but possibly incomplete at the recent end."
            )
        return (
            f"The most recent signal is dated {self.as_of}, {self.age_days} day(s) ago, well "
            f"beyond {rhythm}. The evidence is STALE: the timeline may no longer reflect what "
            "this competitor is doing, and a forecast drawn from it carries that uncertainty."
        )


def _parse_iso(value: str) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def _cadence_days(signals: list[Signal]) -> int | None:
    """Median gap between consecutive signals — the timeline's own rhythm.

    Median rather than mean: a single long dormancy stretches the mean and would
    make an active competitor look quiet.
    """
    gaps: list[int] = []
    for earlier, later in zip(signals, signals[1:]):
        start, end = _parse_iso(earlier.date), _parse_iso(later.date)
        if start and end and (end - start).days > 0:
            gaps.append((end - start).days)
    return int(statistics.median(gaps)) if gaps else None


def evidence_clock(signals: list[Signal], today: date | None = None) -> EvidenceClock:
    """Assess the timeline's freshness against today and its own cadence."""
    today = today or date.today()
    if not signals:
        return EvidenceClock(today, "", 0, None, "unknown")

    as_of = signals[-1].date
    last = _parse_iso(as_of)
    age = max((today - last).days, 0) if last else 0
    cadence = _cadence_days(signals)

    if not cadence:
        staleness = "unknown"
    elif age <= cadence:
        staleness = "fresh"
    elif age <= cadence * 2:
        staleness = "aging"
    else:
        staleness = "stale"
    return EvidenceClock(today, as_of, age, cadence, staleness)


def build_prompt(
    competitor: str, signals: list[Signal], today: date | None = None
) -> str:
    clock = evidence_clock(signals, today=today)
    return SYNTHESIS_PROMPT.format(
        competitor=competitor,
        numbered_timeline=format_timeline(signals),
        today=clock.today.isoformat(),
        age_sentence=clock.age_sentence,
    ) + GROUNDING_RULES


def _fallback_response(
    competitor: str,
    signals: list[Signal],
    reason: str,
    clock: EvidenceClock | None = None,
) -> SynthesisResponse:
    """Deterministic read used when the LLM is unavailable.

    Still a real answer: it reports the chronological span, the per-type
    counts, and says plainly that the narrative could not be generated.
    """
    clock = clock or evidence_clock(signals)
    counts: dict[str, int] = {}
    for signal in signals:
        counts[signal.signal_type] = counts.get(signal.signal_type, 0) + 1
    breakdown = ", ".join(f"{count} {name}" for name, count in sorted(counts.items()))
    return SynthesisResponse(
        competitor=competitor,
        patterns=(
            f"{len(signals)} signals recorded ({breakdown}). "
            "Narrative synthesis unavailable: the LLM call failed "
            f"({reason}). The timeline below is complete and is the evidence."
        ),
        inferred_intent="Not inferred — narrative generation unavailable in this run.",
        predicted_next_move=(
            "Not predicted. Re-run the strategic read once the LLM is reachable; "
            "the stored memory is intact."
        ),
        recommendation="Check GROQ_API_KEY / Groq availability, then re-run the read.",
        signal_count=len(signals),
        timeline_window=timeline_window(signals),
        model_used="fallback (no LLM)",
        data_as_of=clock.as_of,
        evidence_age_days=clock.age_days,
        evidence_staleness=clock.staleness,
    )


def generate_strategic_read(
    competitor: str, today: date | None = None
) -> SynthesisResponse:
    """Full-timeline retrieval from Hindsight -> cross-signal strategic narrative.

    ``today`` is injectable so the time-anchoring behaviour can be tested
    against a fixed clock instead of whatever day the suite happens to run on.
    """
    competitor = competitor.strip()
    signals = hindsight_client.client.get_timeline(competitor)
    clock = evidence_clock(signals, today=today)

    if not signals:
        return SynthesisResponse(
            competitor=competitor,
            patterns="No signals recorded yet. Log at least a few signals before asking for a read.",
            inferred_intent="Nothing to infer from an empty memory.",
            predicted_next_move="No basis for a prediction.",
            recommendation="Log the first signals — pricing, hiring, and messaging are the fastest to collect.",
            signal_count=0,
            timeline_window=timeline_window(signals),
            model_used="no retrieval",
            data_as_of=clock.as_of,
            evidence_age_days=clock.age_days,
            evidence_staleness=clock.staleness,
        )

    prompt = build_prompt(competitor, signals, today=today)
    try:
        parsed, model_used = llm_client.call_llm_json(prompt)
    except LLMError as exc:
        log.warning("synthesis LLM call failed for %s: %s", competitor, exc)
        return _fallback_response(competitor, signals, str(exc), clock)

    # Coerce defensively: a missing or null field should degrade one section,
    # not blank the whole read.
    def field(name: str) -> str:
        value = parsed.get(name)
        if isinstance(value, list):
            value = " ".join(str(item) for item in value)
        text = str(value or "").strip()
        return text or "Not stated by the model."

    return SynthesisResponse(
        competitor=competitor,
        patterns=field("patterns"),
        inferred_intent=field("inferred_intent"),
        predicted_next_move=field("predicted_next_move"),
        recommendation=field("recommendation"),
        signal_count=len(signals),
        timeline_window=timeline_window(signals),
        model_used=model_used,
        data_as_of=clock.as_of,
        evidence_age_days=clock.age_days,
        evidence_staleness=clock.staleness,
    )
