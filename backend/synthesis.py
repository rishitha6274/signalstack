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
from datetime import date

from . import hindsight_client
from .llm_client import LLMError, client as llm_client
from .models import Signal, SynthesisResponse

log = logging.getLogger("signal_stack.synthesis")


# The prompt template mandated by the brief, used verbatim.
SYNTHESIS_PROMPT = """You are a competitive intelligence analyst. Below is the full chronological \
timeline of tracked signals for {competitor}:

{numbered_timeline}

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


def build_prompt(competitor: str, signals: list[Signal]) -> str:
    return SYNTHESIS_PROMPT.format(
        competitor=competitor, numbered_timeline=format_timeline(signals)
    ) + GROUNDING_RULES


def _fallback_response(competitor: str, signals: list[Signal], reason: str) -> SynthesisResponse:
    """Deterministic read used when the LLM is unavailable.

    Still a real answer: it reports the chronological span, the per-type
    counts, and says plainly that the narrative could not be generated.
    """
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
    )


def generate_strategic_read(competitor: str) -> SynthesisResponse:
    """Full-timeline retrieval from Hindsight -> cross-signal strategic narrative."""
    competitor = competitor.strip()
    signals = hindsight_client.client.get_timeline(competitor)

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
        )

    prompt = build_prompt(competitor, signals)
    try:
        parsed, model_used = llm_client.call_llm_json(prompt)
    except LLMError as exc:
        log.warning("synthesis LLM call failed for %s: %s", competitor, exc)
        return _fallback_response(competitor, signals, str(exc))

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
    )
