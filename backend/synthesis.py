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

{facts_block}

Analyze this timeline and return ONLY valid JSON with these fields:
- "patterns": patterns you observe across signal types, citing specific dates
- "inferred_intent": what strategic intent likely explains these moves
- "predicted_next_move": a specific, falsifiable prediction of what they will likely do next
- "recommendation": one concrete action our team should take in response
- "confidence": one of "high", "medium", "low", or "none"
- "missing_evidence": one sentence naming the specific additional signal that would
  raise confidence in this read

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

CALIBRATION (required fields — a read without them is rejected):
- "confidence" must be exactly one of "high", "medium", "low", "none". Judge it
  against the FACTS block, not against how assertive the analysis sounds.
- "missing_evidence" is ONE sentence naming the single most useful signal not
  yet in the timeline that would raise confidence — e.g. "a second pricing move
  would show the discounting is deliberate rather than a one-off". Never write
  "none needed", and never restate the pattern back at me.
- Use "none" only when you offer no prediction. A prediction always needs a
  level. If the evidence is insufficient to predict, set "none" AND still
  populate "missing_evidence" with what you are waiting for.

ARITHMETIC AND REPETITION (the FACTS block is authoritative):
- You may ONLY cite an interval, cadence, or gap number that appears in the
  FACTS block. Do not compute, estimate, average, or round your own. If the
  median interval is 26 days, say 26 days — do not widen it to "30-45".
- Do not list intervals selectively. When you enumerate them, the most recent
  one belongs in the list even when it breaks the rhythm you are describing.
- Call a sequence "repeating", "recurring", "a loop", "a cycle", or "a
  rotation" ONLY when the FACTS block shows that transition at least twice.
  When it appears once, write "one observed instance" or "a single instance".
  An audit caught "repeats three times" for a cycle seen once, and
  "consistently precedes" for a relationship that occurred zero times inside the
  window it claimed. When you do have a repeat, state the count the facts give.
- Any quoted phrase must be copied VERBATIM from a signal summary. Do not put
  quotation marks around a phrase you invented to name a pattern.

PREDICTION CONSISTENCY:
- "predicted_next_move" must be consistent with the three most recent signals
  quoted in the FACTS block. If your forecast departs from their direction, you
  MUST say why in that same field. A prediction that quietly skips a stage you
  identified yourself is a failure.
- Any date you name must fall AFTER today. A date already in the past, or a
  cadence continuation that lands in the past, is a failure: say whether the
  pattern has gone quiet or already played out, then forecast forward.
- If the FACTS block says the stream is OVERDUE, "predicted_next_move" MUST
  state how many days overdue it is and what that does to the forecast, and
  "confidence" must NOT be "high" — cap it at "medium" or "low". A precisely
  dated forecast that reads as though the last signal were recent is a failure.

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
    """The date window only.

    Deliberately excludes the signal count: the UI renders the count itself
    ("Built from 9 signals (2026-01-14 to 2026-08-11)"), and including it here
    produced the doubled, nested form "Built from 9 signals (2026-01-14 to
    2026-08-11 (9 signals))".
    """
    if not signals:
        return "no signals recorded"
    if len(signals) == 1:
        return signals[0].date
    return f"{signals[0].date} to {signals[-1].date}"


# --------------------------------------------------------------------------
# Computed facts
# --------------------------------------------------------------------------
# The audit found the model inventing plausible cadences that the dates
# contradict: "a 14-21 day cadence" over intervals of 6, 6, 9, ... 35 days; a
# "30-45 day rhythm" over a 21-day range; three hand-picked 42-day gaps listed
# while the most recent 27-day gap went unmentioned. Every one of those numbers
# was arithmetic the model was never given and got wrong.
#
# So it is no longer asked to. Everything numeric is computed here, from the
# timeline, and handed over as a FACTS block. The model may only cite numbers
# that appear in that block, and the validators reject any that do not. This
# also settles A and B: recurrence counts and overdue-ness become facts the
# model reads rather than judgements it makes.
@dataclass(frozen=True)
class Facts:
    """Every number the model is allowed to cite, computed from the timeline."""

    intervals: tuple[int, ...]  # days between consecutive signals
    median_interval: int | None
    min_interval: int | None
    max_interval: int | None
    days_since_last: int
    days_overdue: int  # days_since_last - median_interval, 0 if not overdue
    transitions: tuple[tuple[str, str, int], ...]  # (from_type, to_type, count)
    last_three: tuple[str, ...]  # the final signals, verbatim

    @property
    def repeatable_transitions(self) -> set[tuple[str, str]]:
        """Adjacent type pairs seen at least twice — the only ones that may be
        described as repeating, recurring, or a cycle."""
        return {(a, b) for a, b, count in self.transitions if count >= 2}

    @property
    def allowed_numbers(self) -> set[int]:
        """Numeric values the response may cite without a validation failure.

        The interval figures and their simple multiples, plus the derived
        counts. Deliberately generous: a validator that only permits one
        phrasing forces awkward prose, and a false positive costs a retry.
        Anything outside this set and unsupported by the timeline is a bug.
        """
        allowed: set[int] = set()
        for base in (self.intervals, (self.median_interval, self.min_interval,
                                      self.max_interval, self.days_since_last,
                                      self.days_overdue)):
            for value in base:
                if value is None:
                    continue
                allowed.add(abs(int(value)))
                for mult in (2, 3, 4, 6, 12, 52):
                    allowed.add(abs(int(value)) * mult)
                if int(value) >= 14:
                    allowed.add(abs(int(value)) // 7)  # "4 weeks" for 28 days
                if int(value) >= 60:
                    allowed.add(abs(int(value)) // 30)  # "2 months" for 60 days
        allowed.update({0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 100})
        return {value for value in allowed if value >= 0}


def _transitions(signals: list[Signal]) -> tuple[tuple[str, str, int], ...]:
    """Count adjacent signal-type pairs across the whole timeline.

    Only adjacent pairs count. A rule the model states must be about the
    sequence as it actually played out, not about pairs that happen to co-occur
    somewhere in the middle.
    """
    counts: dict[tuple[str, str], int] = {}
    for earlier, later in zip(signals, signals[1:]):
        key = (earlier.signal_type, later.signal_type)
        counts[key] = counts.get(key, 0) + 1
    return tuple((a, b, n) for (a, b), n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def compute_facts(signals: list[Signal], clock: EvidenceClock) -> Facts:
    intervals = tuple(
        (b - a).days
        for a, b in zip(signal_dates(signals), signal_dates(signals)[1:])
    )
    median = clock.cadence_days
    overdue = 0
    if median and clock.age_days > median:
        overdue = clock.age_days - median
    return Facts(
        intervals=intervals,
        median_interval=median,
        min_interval=min(intervals) if intervals else None,
        max_interval=max(intervals) if intervals else None,
        days_since_last=clock.age_days,
        days_overdue=overdue,
        transitions=_transitions(signals),
        last_three=tuple(
            f"[{s.date}] ({s.signal_type}) {s.summary}" for s in signals[-3:]
        ),
    )


def signal_dates(signals: list[Signal]) -> list[date]:
    parsed = [_parse_iso(signal.date) for signal in signals]
    return [value for value in parsed if value is not None]


def format_facts(facts: Facts) -> str:
    """The FACTS block. Everything here is computed; nothing is estimated."""
    lines = ["COMPUTED FACTS (calculated from the timeline above; these are the only",
             "numbers you may cite — do not compute, estimate, or round your own):"]

    if not facts.intervals:
        lines.append("- Intervals between signals: not enough signals to measure.")
    else:
        lines.append(f"- Intervals between consecutive signals, in days: {list(facts.intervals)}")
        lines.append(
            f"- Median interval: {facts.median_interval} days. "
            f"Shortest: {facts.min_interval} days. Longest: {facts.max_interval} days."
        )
        lines.append(
            f"- Days since the final signal, as of today: {facts.days_since_last}."
        )
        if facts.days_overdue > 0:
            lines.append(
                f"- OVERDUE BY {facts.days_overdue} DAYS: the stream has been silent for "
                f"{facts.days_since_last} days against a median interval of "
                f"{facts.median_interval} days. This is the single most important "
                f"caveat in this read."
            )
        else:
            lines.append(
                f"- Not overdue: {facts.days_since_last} days is within the "
                f"{facts.median_interval}-day median interval."
            )

    lines.append("")
    lines.append("Adjacent signal-type transitions and how many times each occurs:")
    for earlier, later, count in facts.transitions:
        verdict = (
            f"occurs {count} times — MAY be called repeating/recurring/a cycle"
            if count >= 2
            else f"occurs {count} time — describe as ONE OBSERVED INSTANCE, "
            f"never as repeating, recurring, a loop, or a cycle"
        )
        lines.append(f"  {earlier} -> {later}: {verdict}")
    if not facts.transitions:
        lines.append("  (no adjacent transitions: fewer than two signals)")

    lines.append("")
    lines.append("The three most recent signals, verbatim:")
    for item in facts.last_three:
        lines.append(f"  {item}")
    return "\n".join(lines)


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
    facts = compute_facts(signals, clock)
    return SYNTHESIS_PROMPT.format(
        competitor=competitor,
        numbered_timeline=format_timeline(signals),
        today=clock.today.isoformat(),
        age_sentence=clock.age_sentence,
        facts_block=format_facts(facts),
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
        confidence="none",
        missing_evidence=(
            "A successful LLM call: the timeline is intact in Hindsight and the "
            "narrative can be regenerated without re-logging anything."
        ),
        signal_count=len(signals),
        timeline_window=timeline_window(signals),
        model_used="fallback (no LLM)",
        data_as_of=clock.as_of,
        evidence_age_days=clock.age_days,
        evidence_staleness=clock.staleness,
    )


def _rejected_response(
    competitor: str,
    signals: list[Signal],
    failures: list[str],
    clock: EvidenceClock,
) -> SynthesisResponse:
    """What we show when a read cannot be trusted.

    Deliberately refusal-shaped rather than silently degraded: it states what
    went wrong and what would fix it, and declines to predict. Showing a
    rejected narrative with a warning attached would be worse, because the
    warning is easy to skim and the confident prose is not.
    """
    detail = "; ".join(failures[:3])
    return SynthesisResponse(
        competitor=competitor,
        patterns=(
            f"This read was withheld: it failed validation against the computed "
            f"facts for this timeline ({len(signals)} signals, median interval "
            f"{clock.cadence_days} days). Defects: {detail}. The timeline itself "
            "is complete and unchanged — only the narrative was rejected."
        ),
        inferred_intent=(
            "Not inferred. An intent read was generated but could not be checked "
            "against the timeline, so it is not shown."
        ),
        predicted_next_move=(
            "No prediction. A forecast was produced but failed validation, so it "
            "is withheld rather than shown unchecked."
        ),
        recommendation=(
            "Re-run the strategic read. Validation failures of this kind are "
            "usually transient model non-compliance; if they persist, the prompt "
            "and validator disagree about the timeline and the facts block in the "
            "server log is the place to look."
        ),
        confidence="none",
        missing_evidence=(
            "A regenerated read that cites only the intervals and transitions in "
            "the computed facts block."
        ),
        signal_count=len(signals),
        timeline_window=timeline_window(signals),
        model_used="rejected by validation",
        data_as_of=clock.as_of,
        evidence_age_days=clock.age_days,
        evidence_staleness=clock.staleness,
        signal_count_validated=False,
        validation_notes=failures,
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
            confidence="none",
            missing_evidence="At least four signals spanning more than one type; a single signal cannot show a sequence.",
            signal_count=0,
            timeline_window=timeline_window(signals),
            model_used="no retrieval",
            data_as_of=clock.as_of,
            evidence_age_days=clock.age_days,
            evidence_staleness=clock.staleness,
        )

    prompt = build_prompt(competitor, signals, today=today)
    facts = compute_facts(signals, clock)
    parsed, model_used, validation = _call_and_validate(
        prompt, facts, signals, clock, competitor
    )
    if parsed is None:
        if model_used == "rejected by validation":
            return _rejected_response(competitor, signals, validation.failures, clock)
        return _fallback_response(
            competitor, signals, "the LLM was unreachable", clock
        )

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
        confidence=field("confidence").lower(),
        missing_evidence=field("missing_evidence"),
        signal_count=len(signals),
        timeline_window=timeline_window(signals),
        model_used=model_used,
        data_as_of=clock.as_of,
        evidence_age_days=clock.age_days,
        evidence_staleness=clock.staleness,
        signal_count_validated=validation.ok,
        validation_notes=validation.failures,
    )


def _call_and_validate(
    prompt: str,
    facts: Facts,
    signals: list[Signal],
    clock: EvidenceClock,
    competitor: str,
):
    """Call the model, validate, and retry once with the defects quoted back.

    A read that fails validation twice is replaced rather than shown: the
    reader cannot see the validation, so an untrustworthy read reads exactly
    like a trustworthy one.
    """
    from . import validators

    last_failures: list[str] = []
    for attempt in (1, 2):
        try:
            parsed, model_used = llm_client.call_llm_json(prompt)
        except LLMError as exc:
            log.warning("synthesis LLM call failed for %s: %s", competitor, exc)
            return None, "fallback (no LLM)", validators.ValidationResult(ok=True)
        fields = {k: str(parsed.get(k) or "") for k in
                  ("patterns", "inferred_intent", "predicted_next_move",
                   "recommendation", "confidence", "missing_evidence")}
        validation = validators.validate_read(fields, facts, signals, clock.today)
        if validation.ok:
            return parsed, model_used, validation
        last_failures = validation.failures
        log.info(
            "synthesis validation rejected read for %s (attempt %s/%s): %s",
            competitor, attempt, 2, "; ".join(validation.failures),
        )
        if attempt == 1:
            prompt = validators.retry_prompt(prompt, last_failures)
    return None, "rejected by validation", validators.ValidationResult(
        ok=False, failures=last_failures
    )
