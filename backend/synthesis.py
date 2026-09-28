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

from . import hindsight_client, validators
from .config import MAX_SIGNALS_IN_PROMPT
from .facts import build_facts, days_overdue
from .llm_client import LLMError, RateLimitError, client as llm_client
from .models import Confidence, Signal, SynthesisResponse
from .validators import validate_response

log = logging.getLogger("signal_stack.synthesis")


# The prompt template mandated by the brief, used verbatim.
SYNTHESIS_PROMPT = """You are a competitive intelligence analyst. Below is the full chronological \
timeline of tracked signals for {competitor}:

{numbered_timeline}

TIME REFERENCE: Today is {today}. {age_sentence}

{facts_block}

Analyze this timeline and return ONLY valid JSON with these fields:
- "patterns": the patterns or ordering you observe across signal types, citing specific dates
- "inferred_intent": what strategic intent likely explains these moves
- "predicted_next_move": a specific, falsifiable prediction of what they will likely do next
- "recommendation": one concrete action our team should take in response
- "confidence": exactly one of "high", "medium", "low" (or "none" if refusing)
- "missing_evidence": ONE sentence naming the specific additional observation that would most raise confidence

Be specific. Reference actual signals and dates. Do not give generic advice."""

# Appended to the template above. Same instructions, plus the guardrails that
# keep the output honest about thin timelines.
GROUNDING_RULES = """

GROUNDING RULES (these override any habit of giving generic advice):
- Every claim must be anchored to a specific signal in the timeline above, cited
  by its date. A claim you cannot date is a claim you must not make.
- Every statement of a date, interval, length of time or count of occurrences
  must be copied from the FACTS block. Do NOT compute, average, round or estimate
  any figure yourself. The FACTS block already contains every interval between
  consecutive signals, the median, the range, the days since the last signal, the
  days overdue, and how many times each signal-type transition occurs. If you want
  to say "a 4-6 week rhythm", read the intervals from the FACTS block and describe
  only what is there. A cadence you inferred that does not appear in the FACTS
  block is a fabrication, however plausible it sounds.
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

REPETITION (the FACTS block counts every transition for you):
- Call a sequence "repeating", "recurring", "a loop", "a cycle" or "a pattern"
  ONLY IF the FACTS block shows that transition occurring at least twice. If a
  transition is listed under "seen only ONCE", you must describe it as "one
  observed instance" or "a single instance" — never as a repetition.
- "Two instances" is not a cadence. One prior instance and one prediction is
  still one instance: never count your own prediction as evidence for a pattern
  that already happened.

CALIBRATION (required fields — a response missing either is rejected):
- "confidence" is required: exactly one of "high", "medium", "low". Use "high"
  only when the FACTS block shows a transition repeating at least twice AND the
  stream is not overdue. Use "medium" when the evidence supports one clear
  reading. Use "low" when the read is a plausible inference from thin or
  late evidence.
- "missing_evidence" is required: ONE sentence naming the specific additional
  observation that would most raise confidence — e.g. "a second consecutive
  X would confirm the cycle" or "any signal after {date} would tell us whether
  the stream resumed". Name the thing, not the absence of confidence.

TIME ANCHORING (you cannot infer the current date — it is given above):
- Forecast from TODAY, never from the last date in the timeline. When you write
  "within 2 weeks", you mean 2 weeks from today, not 2 weeks after the final
  logged signal. The timeline's final date is evidence, not the present.
- NEVER present a date that is already in the past as a future prediction. If
  the cadence you measured from the timeline would land on a date before today,
  that tells you the pattern has either already played out or gone quiet — say
  which, and forecast from today instead of naming a stale deadline. If the next
  date your own cadence would produce has already passed, you MUST say that the
  expected signal did not arrive on schedule.
- Every date you predict must be after {today} (today's date is in the FACTS
  block).
- Respect the evidence-freshness note in the TIME REFERENCE line, and the
  "days overdue" figure in the FACTS block. If the stream is overdue,
  "predicted_next_move" MUST state how many days past its rhythm it is, and
  "confidence" MUST NOT be "high". A precisely-dated forecast built on an overdue
  stream is a failure of this rule even when the pattern itself is real.

CONSISTENCY WITH THE LATEST SIGNALS:
- "predicted_next_move" must be consistent with the three most recent signals in
  the FACTS block, and with the direction they show. If your prediction departs
  from that direction — for example the last three signals are pricing and
  messaging and you predict a hiring wave — you must say explicitly why you
  depart from it, in "predicted_next_move". Do not silently skip a stage of a
  cycle you yourself identified in "patterns".

The six fields are one argument. If "patterns" reports insufficient evidence,
"inferred_intent" and "predicted_next_move" must follow that conclusion rather
than quietly contradicting it, and "confidence" must then be "low" (or the
response must be a refusal, which is described below).

REFUSING:
- If the evidence cannot support a strategic read, you may return a refusal
  instead of a forecast: set "confidence" to "none", state plainly in
  "predicted_next_move" that no reliable prediction can be made, and use
  "missing_evidence" to name the specific signal that would change that. A
  refusal with a specific missing-evidence sentence is a high-value answer, not
  a failure.

Output raw JSON only. No preamble, no markdown fences, no commentary outside the
JSON object."""


def _window_indices(ordered: list[Signal], cap: int) -> set[int]:
    """Which positions of a date-sorted timeline belong in the prompt.

    Split out from `fit_prompt_window` so the selection has exactly one
    implementation. The omitted count and the omitted span have to describe the
    same set of signals; deriving them separately by matching objects or strings
    is how the prompt ends up saying "12 of 71" and "nothing before March" when
    it actually dropped two signals from the middle.
    """
    if len(ordered) <= cap:
        return set(range(len(ordered)))

    keep = {0, *range(len(ordered) - cap, len(ordered))}

    # A transition is a consecutive pair in the FULL timeline. Counting repeats
    # on the already-trimmed window would be circular: trimming is what decides
    # which pairs are consecutive.
    counts: dict[tuple[str, str], int] = {}
    for earlier, later in zip(ordered, ordered[1:]):
        key = (earlier.signal_type, later.signal_type)
        counts[key] = counts.get(key, 0) + 1
    for index, (earlier, later) in enumerate(zip(ordered, ordered[1:])):
        if counts[(earlier.signal_type, later.signal_type)] >= 2:
            keep.add(index)
            keep.add(index + 1)
    return keep


def fit_prompt_window(
    signals: list[Signal], limit: int | None = None
) -> tuple[list[Signal], int]:
    """The signals one prompt may carry, and how many were left out.

    A plain "most recent N" is the obvious rule and the wrong one. It throws
    away the *first* signal, which is where a strategy's origin is, and it can
    slice a repeat in half: a pricing->hiring transition that occurred twice,
    once in March and once last week, looks like it happened once. The whole
    point of a persistent timeline is the chain, so the window keeps three
    things and nothing else:

      1. the FIRST signal -- where the strategy started;
      2. the most recent `limit` signals -- where it is now;
      3. every signal in a REPEATED transition -- the evidence that licenses
         the word "repeats", which is the strongest claim the app makes and
         the easiest one to accidentally make unsupported.

    The result is deliberately not contiguous, and `limit` is therefore a floor
    for recency rather than a hard ceiling: a timeline that is one repeating
    cycle from start to finish keeps every signal, because dropping any of them
    would turn an observed repeat into an apparent one-off. That is the correct
    trade -- an over-long prompt over an understated timeline -- but it is a
    real bound the caller should know about, so the window reports its own size
    and the caller discloses it.

    That non-contiguity is also why the cadence figures are measured on the full
    timeline rather than on this window, and why the disclosure describes the
    omitted signals as a span in the middle of the history rather than as a tail
    before some date.

    Returns the dropped count alongside the window rather than hiding it. The
    caller has to write that count into the prompt, because a model told "12
    signals" when the bank holds 71 produces a confident, well-formed analysis
    of twelve signals and reports it as the lot. See `build_prompt`, which turns
    the count into an explicit EVIDENCE COVERAGE line, and
    `validators.check_no_partial_claims`, which blocks the model from describing
    the window as the whole.
    """
    cap = MAX_SIGNALS_IN_PROMPT if limit is None else limit
    if cap <= 0:
        # A zero or negative cap is a configuration mistake, not an instruction
        # to send an empty prompt. Refusing is better than analysing nothing.
        raise ValueError(f"MAX_SIGNALS_IN_PROMPT must be positive, got {cap}")
    ordered = sorted(signals, key=lambda s: s.date)
    keep = _window_indices(ordered, cap)
    window = [ordered[i] for i in sorted(keep)]
    return window, len(ordered) - len(window)


def omitted_span(signals: list[Signal], window: list[Signal]) -> str:
    """The date range the omitted signals cover, or "" if nothing is omitted.

    The window keeps the first signal and the most recent ones, so what is left
    out sits in the middle. "Nothing before 2026-05-01" would be false; the
    honest description is the span the gaps actually cover.
    """
    cap = max(MAX_SIGNALS_IN_PROMPT, len(window))
    ordered = sorted(signals, key=lambda s: s.date)
    keep = _window_indices(ordered, cap)
    missing = sorted(ordered[i].date for i in range(len(ordered)) if i not in keep)
    if not missing:
        return ""
    if len(missing) == 1:
        return missing[0]
    return f"{missing[0]} to {missing[-1]}"


def format_timeline(signals: list[Signal]) -> str:
    """Render signals as a numbered, dated list — the model's whole context."""
    return "\n".join(
        f"{index}. [{signal.date}] ({signal.signal_type}) {signal.summary}"
        for index, signal in enumerate(signals, start=1)
    )


def timeline_window(signals: list[Signal]) -> str:
    """The date span only.

    This used to include the signal count ("2026-01-14 to 2026-08-11 (9
    signals)"), and the UI already renders the count immediately before it —
    "Built from **9 signals** (2026-01-14 to 2026-08-11 (9 signals))". Two
    reports of the same number, inside each other, with the parentheses nested.
    The count belongs to whoever is presenting; the span is what this describes.
    """
    if not signals:
        return "no signals recorded"
    if len(signals) == 1:
        return signals[0].date
    return f"{signals[0].date} to {signals[-1].date}"


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
    window, omitted = fit_prompt_window(signals)
    # Cadence is measured on the full timeline: a window is a subset of the
    # history, not a shorter history, and measuring across a gap the selection
    # created would invent a silence that never happened.
    clock = evidence_clock(signals, today=today)
    span = omitted_span(signals, window)
    facts = build_facts(signals, today=clock.today, staleness=clock.staleness,
                        omitted=omitted, omitted_span=span, window=window)
    coverage = (
        f"- EVIDENCE COVERAGE: COMPLETE. All {len(signals)} signals in this "
        f"bank are shown below.\n"
        if not omitted
        else (
            f"- EVIDENCE COVERAGE: PARTIAL. {len(window)} of {len(signals)} "
            f"signals are shown, chosen as the first signal, the {MAX_SIGNALS_IN_PROMPT} "
            f"most recent, and every signal in a repeated transition. The other "
            f"{omitted} are NOT shown"
            + (f" (they fall between {span})" if span else "")
            + ". A pattern that appears only in the missing stretch would be "
            "invisible to you, and a gap between two listed signals does not "
            "mean nothing happened. Say which stretch you cannot see rather "
            "than reporting the visible run as the whole history.\n"
        )
    )
    return SYNTHESIS_PROMPT.format(
        competitor=competitor,
        numbered_timeline=format_timeline(window),
        today=clock.today.isoformat(),
        age_sentence=clock.age_sentence,
        facts_block=coverage + facts.render() + facts.overdue_instruction(),
    ) + GROUNDING_RULES


def _first_date(signals: list[Signal]) -> str:
    return signals[0].date if signals else "(none)"


def _coerce_confidence(value: object) -> Confidence:
    """Map whatever the model produced onto the enum.

    An unrecognised label becomes "low" rather than "none": a read that
    returned a forecast has *made* an assertion, and downgrading its certainty
    is the honest reading. "none" is reserved for an actual refusal, which is
    what `is_refusal` decides — and because it decides that, this function
    must not hand out "none" by accident.
    """
    text = str(value or "").strip().lower()
    for candidate in Confidence:
        if text == candidate.value:
            return candidate
    # Tolerate a little decoration: "Confidence: high (one caveat)".
    #
    # Never for "none", though. Unlike "high" or "low" it is an ordinary English
    # word, so the substring match reads "None of the evidence supports a
    # forecast" as a refusal when the model made exactly the forecast it was
    # describing. The exact match above already covers the decorated form that
    # matters ("none", "Confidence: none"); anything longer is a phrase, and a
    # phrase in this field is not a decision.
    for candidate in Confidence:
        if candidate is Confidence.none:
            continue
        if candidate.value in text:
            return candidate
    return Confidence.low


def is_refusal(parsed: dict) -> bool:
    """Did the model decline to forecast? The structured field is the answer.

    A read is a refusal if and only if it reports ``confidence: "none"``. Nothing
    else gets a vote.

    This used to scan the prose for "insufficient evidence", "cannot determine"
    and similar, on the reasoning that a refusal is a claim about the evidence.
    That was the wrong kind of rule: it read the model's vocabulary instead of
    its decision, so the same competitor could be classified differently
    depending on which words it happened to reach for. In the live sweep a
    competitor with 11 signals was classified as a refusal because its
    narrative happened to contain "evidence is insufficient" — a sentence
    describing the timeline, not declining to forecast. The first attempt was
    rejected as "a refusal must report confidence 'none'" for a read that had
    been perfectly willing to forecast, and the retry then talked it into
    refusing. The outcome was defensible; the path to it was a coin flip.

    A structured field exists precisely so this decision does not need a
    heuristic. If the model means to decline, it says so in `confidence`, and
    the same field is what the reader and the audit see.
    """
    return _coerce_confidence(parsed.get("confidence")) is Confidence.none


def _correction_notice(problems: list[str], facts) -> str:
    """Turn a rejection into a targeted second request.

    Every instruction here has to be one the validators would accept, or the
    retry is guaranteed to fail the same way. Two were not, and both cost live
    reads:

    - The notice said confidence must be "one of high/medium/low" while the
      validator requires "none" for a refusal. A refusal was rejected, told to
      report one of the three, and rejected again. The allowed set now comes
      from `validators.allowed_confidence_values`, the same function the
      validator uses, and `tests/selfcheck.py` asserts the two agree.
    - The notice listed the authoritative figures but never said the model could
      not use others, and a retry that fixed one wrong number wrote a different
      wrong one. It now names the permitted figures explicitly and says to state
      no figure rather than an unlisted one.
    """
    lines = [
        "YOUR PREVIOUS RESPONSE WAS REJECTED. It will not be shown to a reader. "
        "The following problems are exactly what the application checks, and the "
        "response must not contain any of them:",
    ]
    for problem in problems:
        lines.append(f"  - {problem}")
    lines.append("")
    if facts.median_interval:
        lines.append(
            f"Authoritative figures, copied from the FACTS block: intervals (days) = "
            f"{facts.intervals}; median = {facts.median_interval}; range = "
            f"{facts.min_interval}-{facts.max_interval}; days since last signal = "
            f"{facts.age_days}; days overdue = {facts.days_overdue}."
        )
        # The permitted set, so a retry cannot invent a replacement figure. These
        # are the numbers `check_interval_claims` will accept as a statement about
        # this stream; anything else has to be omitted or quoted from a signal.
        permitted = sorted(facts.day_interval_claims())
        if permitted:
            lines.append(
                "If you state a day/week/month figure, it must be one of these "
                f"numbers of days: {permitted} — the measured intervals, the median, "
                "the extremes, the age of the evidence and how far overdue it is. "
                "Do not state any other interval figure, in digits or in words "
                "('two weeks' is a figure too). The safest choice is to state no "
                "interval figure at all: the FACTS block already carries them."
            )
        repeated = facts.repeated_transitions()
        single = facts.single_transitions()
        if repeated:
            lines.append(
                "Transitions that DO repeat (you may call these repeating/cycles): "
                + ", ".join(f"{a}->{b} x{c}" for (a, b), c in sorted(repeated.items()))
            )
        if single:
            lines.append(
                "Transitions seen ONCE (you may NOT call these repeating/cycles/"
                "patterns — say 'one observed instance'): "
                + ", ".join(f"{a}->{b}" for a, b in sorted(single.items()))
            )
    sufficient = facts.evidence_sufficient
    forecast_values = validators.allowed_confidence_values(facts, refused=False)
    listed = ", ".join(forecast_values)
    if sufficient:
        lines.append(
            f"CONFIDENCE: report \"confidence\" as one of: {listed}. The FACTS block "
            f"records this evidence as {facts.sufficiency_note}, so a refusal "
            f"(\"confidence\": \"none\") is REJECTED — forecast."
        )
        if not facts.repeated_transitions():
            lines.append(
                "No transition type repeats in this timeline, so do not use "
                "\"repeats\", \"cycle\", \"loop\" or \"recurring\" for anything. Say "
                "\"one observed instance\" instead. \"missing_evidence\" must say that "
                "nothing has repeated yet, or name the occurrence that is missing — "
                "either \"no transition has repeated; a second feature->hiring would be "
                "the first repeat\" or \"a second feature->hiring would be the first "
                "repeat\" is enough. \"medium\" is the ceiling here."
            )
    else:
        lines.append(
            f"CONFIDENCE: report \"confidence\" as one of: {listed}. The FACTS block "
            f"records this evidence as {facts.sufficiency_note}, so you must refuse: "
            f"say plainly in \"predicted_next_move\" that no reliable prediction can be "
            f"made, and name in \"missing_evidence\" the specific signal that would change "
            f"that. A forecast is REJECTED."
        )
    lines.append(
        f"Every predicted date must be after {facts.today.isoformat()}. You must "
        f"include both \"confidence\" and \"missing_evidence\" (one sentence naming the "
        f"specific observation that would most raise your confidence). "
        f"\"missing_evidence\" names the thing you still need, not the absence of "
        f"confidence."
    )
    if sufficient and facts.days_overdue > 0:
        lines.append(
            f"The stream is {facts.days_overdue} day(s) overdue. Your "
            f"\"predicted_next_move\" must say so, and \"confidence\" must not be \"high\"."
        )
    return "\n".join(lines)


def _unvalidated_response(
    competitor: str,
    signals: list[Signal],
    clock: EvidenceClock,
    facts,
    reason: str,
) -> SynthesisResponse:
    """Shown when the model failed validation twice.

    Not an error page and not a stub: the deterministic facts plus an explicit
    statement of what was rejected. A reader sees the real intervals, the real
    overdue figure, and the reason no narrative is being offered — which is more
    useful than a fluent forecast that failed its own arithmetic checks.
    """
    breakdown: dict[str, int] = {}
    for signal in signals:
        breakdown[signal.signal_type] = breakdown.get(signal.signal_type, 0) + 1
    counts = ", ".join(f"{n} {name}" for name, n in sorted(breakdown.items()))

    rhythm = (
        f"the median gap between consecutive signals is {facts.median_interval} days "
        f"(range {facts.min_interval}-{facts.max_interval})"
        if facts.median_interval
        else "there is not enough history to measure a rhythm"
    )
    overdue = (
        f"The stream is {facts.days_overdue} day(s) past that rhythm, so this timeline "
        f"is {clock.staleness}."
        if facts.days_overdue > 0
        else f"The stream is within its usual rhythm, so this timeline is {clock.staleness}."
    )

    return SynthesisResponse(
        competitor=competitor,
        patterns=(
            f"{facts.n} signals recorded ({counts}). Measured intervals in days: "
            f"{facts.intervals} — {rhythm}. {overdue} Narrative synthesis was generated "
            f"twice and rejected by the application's validators ({reason}), so it is "
            f"withheld rather than shown unverified. The measurements above are exact."
        ),
        inferred_intent=(
            "Not inferred. The model did not produce a claim about intent that passed "
            "the evidence checks, and an unsupported intent is worse than none."
        ),
        predicted_next_move=(
            f"Not predicted. The last signal is dated {clock.as_of} "
            f"({clock.age_days} day(s) ago) and {overdue.lower()}"
            if facts.days_overdue > 0
            else f"Not predicted. The last signal is dated {clock.as_of} ({clock.age_days} day(s) ago)."
        ),
        recommendation=(
            f"Re-run the strategic read, or check the LLM response against the "
            f"measured facts above: intervals {facts.intervals}, median "
            f"{facts.median_interval} days."
        ),
        confidence=Confidence.none,
        missing_evidence=(
            f"A model response that cites only these measured intervals "
            f"({facts.intervals}) and acknowledges the "
            f"{facts.days_overdue}-day overdue gap would raise confidence from none."
        ),
        narrative_withheld=True,
        signal_count=len(signals),
        timeline_window=timeline_window(signals),
        model_used=f"rejected by validators ({reason[:80]})",
        data_as_of=clock.as_of,
        evidence_age_days=clock.age_days,
        evidence_staleness=clock.staleness,
        days_overdue=facts.days_overdue,
    )


def _fallback_response(
    competitor: str,
    signals: list[Signal],
    reason: str,
    clock: EvidenceClock | None = None,
    rate_limited: bool = False,
    retry_after_seconds: float | None = None,
) -> SynthesisResponse:
    """Deterministic read used when the LLM is unavailable.

    Still a real answer: it reports the chronological span, the per-type
    counts, and says plainly that the narrative could not be generated.

    A rate limit is called out separately from a broken provider because the
    advice differs completely: one is "come back in 20 seconds", the other is
    "check GROQ_API_KEY". Folding them into one message sends people to debug
    a key that is working fine.
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
        recommendation=(
            "The LLM rate limit was reached for this minute. Wait for the "
            "cooldown below and re-run the read — the stored signals are "
            "unaffected and no key needs changing."
            if rate_limited
            else "Check GROQ_API_KEY / Groq availability, then re-run the read."
        ),
        confidence=Confidence.none,
        missing_evidence=(
            "A reachable LLM would let the timeline be read; the stored signals "
            "themselves are complete."
        ),
        signal_count=len(signals),
        timeline_window=timeline_window(signals),
        model_used="fallback (no LLM)",
        data_as_of=clock.as_of,
        evidence_age_days=clock.age_days,
        evidence_staleness=clock.staleness,
        days_overdue=days_overdue(clock.age_days, clock.cadence_days),
        rate_limited=rate_limited,
        retry_after_seconds=retry_after_seconds,
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
            confidence=Confidence.none,
            missing_evidence="Any signals at all — a single hiring or pricing signal would start the timeline.",
            signal_count=0,
            timeline_window=timeline_window(signals),
            model_used="no retrieval",
            data_as_of=clock.as_of,
            evidence_age_days=clock.age_days,
            evidence_staleness=clock.staleness,
            days_overdue=0,
        )

    # The same window the prompt is built from, so the validators and the
    # prompt agree about what was shown. The FACTS figures stay full-timeline on
    # purpose: they are measurements the application makes and discloses, and
    # the model is told not to recompute them, so a median interval derived from
    # a signal it did not see is a fact the prompt showed it -- not a guess.
    # What it may not do is *quote* the hidden stretch, and that is enforced
    # below by restricting the quote check to the rendered window.
    window, omitted = fit_prompt_window(signals)
    facts = build_facts(signals, today=clock.today, staleness=clock.staleness,
                        omitted=omitted, omitted_span=omitted_span(signals, window),
                        window=window)
    timeline_text = " ".join(
        f"{signal.date} {signal.signal_type} {signal.summary}" for signal in window
    )
    prompt = build_prompt(competitor, signals, today=today)

    # One retry, then stop trusting the model. The validators exist because the
    # prompt alone let all seven forecasting reads through uncalibrated, so a
    # second attempt is worth its cost; a third is not, and returning a
    # low-confidence read built on the timeline beats returning a bad forecast
    # dressed up as a good one.
    problems: list[str] = []
    parsed: dict = {}
    model_used = ""
    for attempt in range(2):
        try:
            parsed, model_used = llm_client.call_llm_json(prompt)
        except RateLimitError as exc:
            # Caught before its parent: this is the one failure where "retry
            # immediately" is guaranteed to fail again, and the only one with a
            # concrete wait to hand the user.
            log.warning(
                "synthesis rate limited for %s, retry_after=%.0fs",
                competitor, exc.retry_after or 0.0,
            )
            return _fallback_response(
                competitor, signals, str(exc), clock,
                rate_limited=True, retry_after_seconds=exc.retry_after,
            )
        except LLMError as exc:
            log.warning("synthesis LLM call failed for %s: %s", competitor, exc)
            return _fallback_response(competitor, signals, str(exc), clock)

        if attempt == 0 and problems:
            pass
        problems = validate_response(
            parsed, facts, timeline_text, refused=is_refusal(parsed)
        )
        if not problems:
            break
        log.warning(
            "synthesis read for %s rejected on attempt %d: %s",
            competitor, attempt + 1, "; ".join(problems[:4]),
        )
        if attempt == 0:
            # Tell the model exactly what was wrong rather than hoping a rerun
            # lands differently.
            prompt = prompt + "\n\n" + _correction_notice(problems, facts)

    if problems:
        return _unvalidated_response(
            competitor, signals, clock, facts,
            reason="; ".join(problems[:3]),
        )

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
        confidence=_coerce_confidence(parsed.get("confidence")),
        missing_evidence=field("missing_evidence"),
        signal_count=len(signals),
        prompt_signal_count=facts.n,
        signals_omitted_from_prompt=facts.omitted,
        prompt_omitted_span=facts.omitted_span,
        timeline_window=timeline_window(signals),
        model_used=model_used,
        data_as_of=clock.as_of,
        evidence_age_days=clock.age_days,
        evidence_staleness=clock.staleness,
        days_overdue=facts.days_overdue,
    )
