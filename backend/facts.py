"""Deterministic facts about a signal timeline.

The audit of all ten competitors found the same root cause behind three separate
failure modes. Asked to describe a rhythm, the model *invented* one that the
dates contradicted — "a 30-45 day cadence" for a timeline whose intervals ran
21 to 36 days; "~42 days" for one whose last gap was 27; "2-3 months" for
feature-to-pricing pairs at 40, 139 and 25 days. It also asserted repetition that
the timeline does not contain ("repeats three times" for a cycle that occurred
once) and, when the data had gone quiet, forecast a future move as though the
last signal were recent.

None of that is a knowledge problem. It is arithmetic the model is bad at and we
are good at, performed on data we already hold. So we stop asking: every number,
every transition count, and the overdue gap are computed here, once, in Python,
and handed to the model as a FACTS block it may quote but may not recompute.

The model keeps the work only it can do — reading intent across a sequence, and
saying what that implies. It loses the work it was reliably getting wrong.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Sequence

from .models import Signal

# Signal types that count as a go-to-market / commercial move, used when
# counting transitions. Kept as data rather than a hard-coded branch so the
# prompt and the validator cannot drift apart.
GTM_TYPES: frozenset[str] = frozenset({"pricing", "hiring", "messaging", "funding"})

# -- the evidence floor ----------------------------------------------------
# Below this, a read is required to refuse; at or above it, a refusal is
# rejected. Named constants rather than literals scattered through the
# validators, so the rule has one home and one place to be wrong in.
#
# TUNED, NOT DERIVED. Five is the point that separates this corpus's designed
# refusals (3-4 signals) from its designed forecasts (6+), so it is a reading
# of ten synthetic competitors rather than a measured threshold. See the note
# in `evidence_sufficient`.
MIN_SIGNALS_FOR_EVIDENCE = 5
# The longest gap may not exceed this multiple of the median. A count alone is
# not enough: eight signals scattered across two years is not a rhythm, and the
# old rule — "refuse when no transition pair repeats" — had nothing to say about
# that case, so it waved it through into an unfounded forecast.
MAX_INTERVAL_SPREAD = 3


def parse_iso(value: str) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def intervals(signals: Sequence[Signal]) -> list[int]:
    """Days between consecutive signals, in timeline order.

    Non-positive gaps are dropped rather than included: a same-day pair is a
    data-entry artefact, and letting a 0 into the median would understate the
    rhythm. The alternative — including it — is how a "0-day cadence" claim gets
    made by accident.
    """
    out: list[int] = []
    for earlier, later in zip(signals, signals[1:]):
        start, end = parse_iso(earlier.date), parse_iso(later.date)
        if start and end:
            gap = (end - start).days
            if gap > 0:
                out.append(gap)
    return out


def cadence_days(signals: Sequence[Signal]) -> int | None:
    """Median gap — the timeline's own rhythm.

    Median, not mean: one long dormancy stretches a mean and makes an active
    competitor look quiet.
    """
    gaps = intervals(signals)
    return int(statistics.median(gaps)) if gaps else None


def transition_counts(signals: Sequence[Signal]) -> dict[tuple[str, str], int]:
    """How many times each consecutive signal-type pair occurs.

    This is what licenses the word "repeats". A model told "feature -> pricing
    occurred 1 time" cannot honestly write "a feature-to-pricing cycle repeats";
    told it occurred 3 times, it can. The count does not forbid the claim — it
    prices it, and the prompt requires the model to name the instance count.
    """
    counts: dict[tuple[str, str], int] = {}
    for earlier, later in zip(signals, signals[1:]):
        key = (earlier.signal_type, later.signal_type)
        counts[key] = counts.get(key, 0) + 1
    return counts


def days_overdue(age_days: int, cadence: int | None) -> int:
    """How far past its own rhythm the last signal sits. 0 when not overdue."""
    if cadence is None:
        return 0
    return max(0, age_days - cadence)


@dataclass(frozen=True)
class TimelineFacts:
    """Every number the model is allowed to cite, computed once."""

    today: date
    n: int
    intervals: list[int]
    median_interval: int | None
    min_interval: int | None
    max_interval: int | None
    age_days: int
    days_overdue: int
    as_of: str
    staleness: str
    last_three: list[Signal] = field(default_factory=list)
    transitions: dict[tuple[str, str], int] = field(default_factory=dict)
    # Every real signal date, so a validator can tell "citing a past event" from
    # "inventing a deadline that has already passed". Without this the two are
    # indistinguishable and the honest answer gets rejected.
    signal_dates: tuple[str, ...] = ()
    # How many signals exist in the bank but were left out of THIS prompt. Zero
    # means the model saw the complete timeline. Non-zero is the one case where
    # a fluent, confident, well-formed answer is still wrong as stated, so it
    # travels with the facts rather than living in a log line.
    omitted: int = 0
    # The dates the omitted signals span. The window keeps the first signal and
    # the most recent ones, so what is missing sits in the MIDDLE of the
    # history and a single "before X" date would describe a shape that no longer
    # exists.
    omitted_span: str = ""
    # How many signals were actually rendered into the prompt. Equals n when
    # nothing was omitted, and is deliberately separate from n: n is the true
    # length of the timeline, and every cadence figure below is measured over
    # all of it.
    shown: int = 0

    # -- the licence: numbers the response may state -----------------------
    def allowed_numbers(self) -> set[int]:
        """Every integer a citation could legitimately be checked against.

        Deliberately generous — the validator rejects a number outside this set,
        so a false rejection would punish a correct answer. It includes the raw
        intervals, the median and extremes, the age, the overdue figure, every
        transition count, and the common unit conversions of each (weeks to
        days, months to days) because "every 4 weeks" describes a 28-day
        interval and must not be read as an unlisted number.
        """
        base: set[int] = {1, 0, self.n}
        values: set[int] = {
            self.age_days,
            self.days_overdue,
            self.median_interval or 0,
            self.min_interval or 0,
            self.max_interval or 0,
            len(self.last_three),
        }
        values.update(self.intervals)
        values.update(self.transitions.values())
        for value in list(values):
            if value <= 0:
                continue
            # Unit conversions a prose citation may use for the same fact.
            values.add(value * 7)          # a value stated in weeks
            values.add(value // 7)         # ... or converted back from weeks
            values.add(value * 30)         # months
            values.add(round(value / 7))
            values.add(round(value / 30))
            # Rounded/approximate restatements ("about 14 days" for 14).
            for delta in (1, 2):
                values.add(value - delta)
                values.add(value + delta)
        return {v for v in values if v >= 0}

    def allowed_interval_claims(self) -> set[int]:
        """Interval figures in days, in both unit directions.

        Separate from `allowed_numbers` because these are the numbers a
        *rhythm* claim rests on. Kept as a distinct set so the validator can
        report a bad cadence more precisely than "unrecognised number".

        Age and the overdue figure belong here even though neither is an
        interval: the prompt *requires* the model to state how many days past
        its rhythm the stream is, and Nimbus's 26-day overdue gap is not one of
        its 11 measured intervals. Excluding them would have rejected the exact
        sentence the overdue rule asks for.

        Week-denominated figures (value // 7) are included for prose that
        counts in weeks, but `day_interval_claims` is what a "N days" claim is
        checked against: mixing the two let "10 day cadence" match Brightline's
        84-day age expressed as 12 weeks.
        """
        out = self.day_interval_claims()
        for value in list(self.intervals) + [self.median_interval, self.min_interval,
                                             self.max_interval, self.age_days,
                                             self.days_overdue]:
            if value:
                out.add(round(value / 7))   # "6 weeks" for a 42-day gap
        return out

    def day_interval_claims(self) -> set[int]:
        """Interval figures in days only, for claims stated in days.

        Kept separate from `allowed_interval_claims` so a day claim cannot be
        satisfied by a week figure. One bag for both units meant the tolerance
        window bridged them: "10 day cadence" passed for a 28-day rhythm because
        the stream's 84-day age is 12 weeks, and 10 is within 2 of 12.
        """
        out: set[int] = set()
        for value in self.intervals:
            out.add(value)
        for value in (self.median_interval, self.min_interval, self.max_interval,
                      self.age_days, self.days_overdue):
            if value:
                out.add(value)
        return out

    def repeated_transitions(self) -> dict[tuple[str, str], int]:
        return {k: v for k, v in self.transitions.items() if v >= 2}

    def single_transitions(self) -> dict[tuple[str, str], int]:
        return {k: v for k, v in self.transitions.items() if v == 1}

    # -- sufficiency: may this timeline be read at all? --------------------
    @property
    def interval_spread(self) -> float | None:
        """Longest gap as a multiple of the median, or None if unknowable.

        None rather than a large number when the rhythm cannot be measured —
        fewer than two signals, or every signal on the same day. An unknown
        spread is not a good spread, so callers treat it as failing the rule.
        """
        if not self.median_interval or self.max_interval is None:
            return None
        return self.max_interval / self.median_interval

    @property
    def evidence_sufficient(self) -> bool:
        """Is there enough here for a strategic read, or must it be refused?

        Sufficient means: at least `MIN_SIGNALS_FOR_EVIDENCE` signals, and a
        rhythm tight enough that the longest gap is within
        `MAX_INTERVAL_SPREAD` times the median.

        This rule replaces "refuse when no transition type repeats", and the
        reason it had to be replaced is that the old proxy was measuring the
        wrong thing. Transition repetition is a property of the *vocabulary*,
        not of the evidence: a competitor whose moves genuinely follow a
        repeating shape will still show no repeated pair when its types happen
        to vary, and a competitor with three signals will show no repeated pair
        because three signals cannot contain one. In the live sweep that
        mismatch cost two competent reads — Palisade (11 signals) and Ferrous
        (6) both refused solely because no adjacent type pair recurred, and both
        had timelines that support a forecast perfectly well. Meanwhile nothing
        in the old rule stopped a confident forecast built on a timeline that
        was merely long, so it spent its strictness in the wrong place.

        Note the asymmetry this creates, which is deliberate. Below the floor a
        refusal is the *only* accepted answer; at or above it a refusal is
        rejected. Both directions are enforced in `validators`, and the FACTS
        block states which side of the line this timeline falls on so the model
        is never guessing.
        """
        if self.n < MIN_SIGNALS_FOR_EVIDENCE:
            return False
        spread = self.interval_spread
        return spread is not None and spread <= MAX_INTERVAL_SPREAD

    @property
    def sufficiency_note(self) -> str:
        """One clause saying which side of the line, and why. Shown in FACTS."""
        if self.n < MIN_SIGNALS_FOR_EVIDENCE:
            return (f"INSUFFICIENT — {self.n} signal(s), below the "
                    f"{MIN_SIGNALS_FOR_EVIDENCE}-signal floor")
        spread = self.interval_spread
        if spread is None:
            return (f"INSUFFICIENT — {self.n} signals, but the intervals cannot be "
                    f"measured (all on the same day, or fewer than two signals), so "
                    f"there is no rhythm to read")
        if spread > MAX_INTERVAL_SPREAD:
            return (f"INSUFFICIENT — {self.n} signals clears the "
                    f"{MIN_SIGNALS_FOR_EVIDENCE}-signal floor, but the longest gap is "
                    f"{self.max_interval}d against a {self.median_interval}d median "
                    f"(spread {spread:.1f}x, above the {MAX_INTERVAL_SPREAD}x limit)")
        return (f"SUFFICIENT — {self.n} signals (floor "
                f"{MIN_SIGNALS_FOR_EVIDENCE}), longest gap {spread:.1f}x the "
                f"{self.median_interval}d median (limit {MAX_INTERVAL_SPREAD}x)")

    # -- rendering ----------------------------------------------------------
    def render(self) -> str:
        """The FACTS block injected into the prompt.

        Written for a reader who will be tempted to do arithmetic: every figure
        is pre-computed, and the instruction not to recompute is stated plainly
        at the top and bottom.
        """
        lines: list[str] = []
        lines.append("FACTS (computed by the application — use these, do not recompute them):")

        if not self.n:
            lines.append("- no signals on record")
            return "\n".join(lines)

        lines.append(f"- signal count: {self.n}")
        lines.append(f"- today's date: {self.today.isoformat()}")
        lines.append(f"- most recent signal: {self.as_of}")
        lines.append(f"- days since the most recent signal: {self.age_days}")
        if self.median_interval is not None:
            lines.append(
                f"- intervals in days between consecutive signals, in order: "
                f"{self.intervals}"
            )
            lines.append(
                f"- median interval: {self.median_interval} days"
                + (
                    f" (range {self.min_interval}-{self.max_interval} days)"
                    if self.min_interval is not None
                    else ""
                )
            )
            lines.append(
                f"- days overdue: {self.days_overdue} "
                + (
                    f"(the stream is {self.days_overdue} day(s) past its {self.median_interval}-day rhythm)"
                    if self.days_overdue > 0
                    else "(not overdue: the last signal is within its usual rhythm)"
                )
            )
        else:
            lines.append("- intervals between consecutive signals: not computable "
                         "(fewer than two signals, or all on the same day)")
            lines.append("- days overdue: not computable")

        if self.transitions:
            multi = self.repeated_transitions()
            single = self.single_transitions()
            if multi:
                rendered = ", ".join(f"{a} -> {b} occurs {c} time(s)" for (a, b), c in
                                     sorted(multi.items(), key=lambda kv: -kv[1]))
                lines.append(f"- transitions that DO repeat (>=2 occurrences): {rendered}")
            if single:
                rendered = ", ".join(f"{a} -> {b} occurs once" for (a, b), _ in
                                     sorted(single.items()))
                lines.append(f"- transitions seen only ONCE (say 'one observed instance', "
                             f"never 'repeating'/'a cycle'): {rendered}")

        # Sufficiency, stated before the model reads anything else it would use
        # to justify itself, and with the instruction that follows from it. The
        # old block listed only "transitions seen only ONCE" and left the model
        # to conclude that no licensable pattern meant no answer — which is how a
        # well-evidenced competitor ended up refusing.
        lines.append(f"- EVIDENCE SUFFICIENCY: {self.sufficiency_note}")
        if self.evidence_sufficient:
            lines.append(
                "  You MUST produce a forecast. A refusal (\"confidence\": \"none\") is "
                "REJECTED for this timeline — report a confidence of high, medium or "
                "low instead."
            )
            if not self.repeated_transitions():
                lines.append(
                    "  REPEAT LANGUAGE: no transition type repeats here. Do not use "
                    "\"repeats\", \"cycle\", \"loop\" or \"recurring\" for anything. "
                    "\"missing_evidence\" MUST state that no transition has repeated, "
                    "and \"confidence\" may not exceed \"medium\"."
                )
        else:
            lines.append(
                "  You MUST refuse: report \"confidence\": \"none\", say plainly in "
                "\"predicted_next_move\" that no reliable prediction can be made, and "
                "name in \"missing_evidence\" the specific signal that would change "
                "that. A forecast is REJECTED for this timeline."
            )

        if self.last_three:
            lines.append("- the three most recent signals, verbatim:")
            for signal in self.last_three:
                lines.append(f"    [{signal.date}] ({signal.signal_type}) {signal.summary}")

        return "\n".join(lines)

    def overdue_instruction(self) -> str:
        """A prompt clause that only appears when there is a real gap."""
        if self.days_overdue <= 0:
            return ""
        return (
            f"\n- The stream is {self.days_overdue} day(s) past its "
            f"{self.median_interval}-day rhythm. \"predicted_next_move\" MUST say how far "
            f"overdue it is, and \"confidence\" MUST NOT be \"high\"."
        )


def build_facts(signals: Sequence[Signal], today: date | None = None,
                staleness: str | None = None, *, omitted: int = 0,
                omitted_span: str = "", window: Sequence[Signal] | None = None
                ) -> TimelineFacts:
    """Measure the timeline, and describe the window the model was given.

    Every cadence figure -- intervals, median, transition counts, staleness --
    is computed over ALL the signals, not over the prompt window. A window is
    not a smaller history, it is a subset of one, and measuring cadence across a
    gap the selection itself created would invent a silence the company never
    had. `window` only says what was rendered, so the coverage line and the
    response can report it.
    """
    """Compute every figure the prompt is allowed to assert."""
    today = today or date.today()
    sigs = sorted(signals, key=lambda s: s.date)
    gaps = intervals(sigs)
    median = int(statistics.median(gaps)) if gaps else None
    as_of = sigs[-1].date if sigs else ""
    last = parse_iso(as_of)
    age = max((today - last).days, 0) if last else 0

    if staleness is None:
        if median is None:
            staleness = "unknown"
        elif age <= median:
            staleness = "fresh"
        elif age <= median * 2:
            staleness = "aging"
        else:
            staleness = "stale"

    return TimelineFacts(
        today=today,
        n=len(sigs),
        intervals=gaps,
        median_interval=median,
        min_interval=min(gaps) if gaps else None,
        max_interval=max(gaps) if gaps else None,
        age_days=age,
        days_overdue=days_overdue(age, median),
        as_of=as_of,
        staleness=staleness,
        last_three=list((list(window) if window is not None else sigs)[-3:]),
        transitions=transition_counts(sigs),
        signal_dates=tuple(s.date for s in sigs),
        omitted=omitted,
        omitted_span=omitted_span,
        shown=len(window) if window is not None else len(sigs),
    )
