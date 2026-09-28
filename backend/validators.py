"""Post-hoc validators for a strategic read.

A prompt rule is a request. These are checks. Each one exists because the audit
of all ten competitors caught the model breaking the corresponding rule in the
wild, and each returns the reason it rejected so the caller can log something
actionable rather than "invalid".

The validators are deliberately conservative about *what counts as a number*.
A false rejection costs a retry and then a downgraded answer, so the parsers
accept anything plausibly derived from the FACTS block and reserve rejection for
numbers that are demonstrably absent from it. Being wrong in the permissive
direction is the lesser evil: a number that happens to be a coincidence rather
than a real fabrication passes, and the rest of the suite still holds.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Iterable

from .facts import TimelineFacts

CONFIDENCE_VALUES = ("high", "medium", "low", "none")

# A quoted run of words. Deliberately naive about which quote mark opened it:
# the point is to catch invented quotations, not to adjudicate typography.
_QUOTE = re.compile(r"['\"\u2018\u201c]([^'\"\u2019\u201d\n]{6,120})['\"\u2019\u201d]")

# Spelled-out amounts a duration can be written with. Defined before _INTERVAL
# because that pattern embeds them.
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "a": 1,
    "an": 1, "couple of": 2, "a couple of": 2,
}

# An integer, optionally with a decimal, adjacent to a day/week/month unit, or
# bare when the sentence is clearly about timing. Bare integers are NOT
# collected: "three times", "two weeks", "5 roles" and "the fourth signal" are
# prose, and catching those would reject nearly every good answer.
#
# The amount may be spelled out. It must be: this pattern was digits-only, so
# "2 weeks" was checked and "two weeks" was not, and the same claim got a
# different verdict by spelling. A live read was rejected for saying "14 days"
# while "two weeks" would have passed untouched.
_NUM = (r"\d+(?:\.\d+)?|"
        + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True)))
_INTERVAL = re.compile(
    rf"({_NUM})\s*(?:-|–|—|to)?\s*({_NUM})?\s*"
    r"(day|week|month|fortnight)s?\b",
    re.I,
)
# A month-like target, optionally with a day: "October 2026", "Oct 2026",
# "15 October 2026", "2026-10-15".
_MONTH = re.compile(
    r"\b(\d{4}-\d{2}-\d{2})\b"
    r"|\b(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+(\d{4})\b"
    r"|\b(January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+(\d{4})\b",
    re.I,
)
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}

# A duration the evidence actually states, in days. Deliberately requires a
# unit: "20 days" is a citation, "20" is a number, and the difference is the
# whole point (see check_interval_claims).
_DURATION = re.compile(
    r"(\d{1,4}(?:\.\d+)?|" + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True)) +
    r")\s*(hour|day|week|month|fortnight)s?\b",
    re.I,
)
_DURATION_DAYS = {
    "hour": 1 / 24, "day": 1, "week": 7, "fortnight": 14, "month": 30,
}

# Words that make a duration a claim about the competitor's rhythm rather than a
# forecast horizon. Only consulted for the indefinite form ("a month"), which is
# ambiguous on its own: "expect a launch within a month" is a horizon and "every
# month" is a cadence. A specific figure ("14 days", "two weeks") is a
# measurement whichever way it is spelled, so it is always checked.
_CADENCE_CUE = re.compile(
    r"cadence|rhythm|every|interval|spaced|spacing|apart|between|gap|median|"
    r"last signal|as of|past|since|apart from",
    re.I,
)


def _as_amount(value: str | None) -> float | None:
    """A duration amount as a number, whether written as a digit or a word."""
    if value is None:
        return None
    text = value.strip().lower()
    if text in _NUMBER_WORDS:
        return float(_NUMBER_WORDS[text])
    try:
        return float(text)
    except ValueError:
        return None


def _stated_durations(evidence: str) -> set[int]:
    """Every duration the evidence states, normalised to whole days.

    Expressed as a set of day values so a claim is exempt only when the
    evidence gives that figure a unit. Years, prices, percentages and headcounts
    therefore do not license a cadence, and a bare number is never a citation.
    """
    out: set[int] = set()
    for match in _DURATION.finditer(_norm(evidence or "").lower()):
        raw, unit = match.group(1), match.group(2).lower()
        amount = _NUMBER_WORDS.get(raw, None)
        if amount is None:
            try:
                amount = float(raw)
            except ValueError:
                continue
        out.add(int(round(amount * _DURATION_DAYS[unit])))
    return out

# Hedging language that legitimately accompanies an unquoted, coined label.
# All four narrative fields are scanned, not just the two evidence fields: an
# invented quotation in the recommendation is exactly as misleading to a reader
# as one in the patterns, and the rule as written only covered half the answer.
_NARRATIVE_FIELDS = ("patterns", "inferred_intent", "predicted_next_move",
                     "recommendation")


def _norm(value: str) -> str:
    """Fold the unicode punctuation models actually emit into ASCII."""
    return (
        value.replace("\u2011", "-")
        .replace("\u2013", "-")
        .replace("\u2014", "-")
        .replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u00a0", " ")
    )


def _as_date(match: re.Match) -> date | None:
    groups = match.groups()
    if groups[0]:
        try:
            return date.fromisoformat(groups[0])
        except ValueError:
            return None
    if groups[1] and groups[2] and groups[3]:
        month = _MONTHS.get(groups[2].lower())
        if not month:
            return None
        try:
            return date(int(groups[3]), month, int(groups[1]))
        except ValueError:
            return None
    if groups[4] and groups[5]:
        month = _MONTHS.get(groups[4].lower())
        if not month:
            return None
        try:
            return date(int(groups[5]), month, 1)
        except ValueError:
            return None
    return None


def allowed_confidence_values(refused: bool) -> tuple[str, ...]:
    """The confidence values a response may report, for this case.

    The single source of truth for the calibration contract. `synthesis.py` builds
    its correction notice from this function rather than restating the rule, and
    `tests/selfcheck.py` asserts the notice satisfies it. That assertion is not
    belt-and-braces: the first version of the notice hardcoded "high/medium/low"
    while the validator demanded "none" for a refusal, so a refusal was rejected,
    told to report one of the three, and rejected again on the retry. A live run
    lost two reads to exactly that, and a third to a refusal the notice had
    provoked. Two copies of a rule is one copy too many.
    """
    return ("none",) if refused else ("high", "medium", "low")


def check_confidence_present(response: dict, facts: TimelineFacts) -> list[str]:
    """D: the read must state how confident it is."""
    problems: list[str] = []
    value = response.get("confidence")
    if value is None or not str(value).strip():
        problems.append("confidence is absent")
    else:
        normalised = str(value).strip().lower()
        if normalised not in CONFIDENCE_VALUES:
            problems.append(
                f"confidence must be one of {CONFIDENCE_VALUES}, got {normalised!r}"
            )
    missing = response.get("missing_evidence")
    if missing is None or not str(missing).strip():
        problems.append("missing_evidence is absent")
    return problems


def check_refusal_calibrated(
    response: dict, facts: TimelineFacts
) -> list[str]:
    """D: a refusal is held to the calibration rules and nothing else.

    A refusal has no forecast to check against the timeline, so it is exempt
    from the interval, quote and date rules. It is not exempt from saying how
    confident it is, which for a refusal is "none" — and that is the whole rule.
    """
    required = allowed_confidence_values(refused=True)
    problems = check_confidence_present(response, facts)
    if str(response.get("confidence") or "").strip().lower() not in required:
        problems.append(f"a refusal must report confidence {required[0]!r}")
    return problems


def check_overdue_acknowledged(
    response: dict, facts: TimelineFacts, prediction: str
) -> list[str]:
    """B: an overdue stream must be acknowledged and capped below high."""
    if facts.days_overdue <= 0:
        return []
    problems: list[str] = []
    lowered = _norm(prediction).lower()
    mentions_gap = bool(
        re.search(
            r"stale|quiet|quietly|silence|overdue|gone (?:quiet|silent)|not (?:produced|emitted)"
            r"|has not|hasn't|since \w+ \d|since \d{4}-\d{2}-\d{2}|"
            rf"\b{facts.days_overdue}\s*day|\b{facts.age_days}\s*day|"
            r"\bno signals?\b|\bno further\b|\bnot produced\b",
            lowered,
        )
    )
    if not mentions_gap:
        problems.append(
            f"stream is {facts.days_overdue}d overdue but predicted_next_move does not "
            f"acknowledge the gap"
        )
    confidence = str(response.get("confidence") or "").strip().lower()
    if confidence == "high":
        problems.append(
            f"stream is {facts.days_overdue}d overdue but confidence is 'high'"
        )
    return problems


def check_interval_claims(
    response: dict, facts: TimelineFacts, *texts: str, evidence: str = ""
) -> list[str]:
    """E: a stated interval must be a real one, in either unit direction.

    Rather than re-deriving the model's arithmetic, this asks the narrow
    question the audit actually needed answered: is every day/week/month figure
    it states one the FACTS block can support? The 30-45 day claim for a
    21-36 day timeline fails because 45 is not within tolerance of any measured
    interval, median, min or max.

    Two deliberate tolerances, both because a false rejection costs a retry and
    then a downgraded answer:

    - The unit is honoured. "1 month" is a fair description of a 28-day rhythm
      and is converted loosely; "30 days" is a precise claim and is held to the
      exact figures. Checking both against one undifferentiated bag of integers
      was how a fabricated 30-day cadence slipped through: one occurrence x 30
      days/month happens to produce the number 30, so every "30 days" claim in
      the corpus passed.
    - A figure that the timeline evidence states as a duration is a citation,
      not a cadence claim: "nine roles in 20 days" comes out of the signals, and
      the model may restate it. The defect the audit found was a duration that
      appears nowhere in the evidence. The test is on *stated durations*, not on
      digits: an earlier version asked whether the digit string appeared anywhere
      in the evidence, which "20" satisfied via every 2026 date and licensed a
      fabricated 20-day cadence on all ten competitors. A price, a headcount, a
      percentage and a year are not durations and do not license a cadence.
    """
    exact = facts.day_interval_claims()
    cited = _stated_durations(evidence)
    problems: list[str] = []
    for text in texts:
        normalised = _norm(text or "")
        for match in _INTERVAL.finditer(normalised):
            unit = match.group(3).lower()
            # "a month" with no cadence cue nearby is a forecast horizon, not a
            # claim about the stream. See _CADENCE_CUE.
            if (match.group(1) or "").lower() in ("a", "an"):
                around = normalised[max(0, match.start() - 60):match.end() + 20]
                if not _CADENCE_CUE.search(around):
                    continue
            for value in (match.group(1), match.group(2)):
                # "a 28 day cadence": the article is not a quantity, and reading
                # it as one ("a" = 1 day) would reject the real figure next to
                # it. The article only carries meaning on its own, as "a month".
                if value is None:
                    continue
                if value.strip().lower() in ("a", "an") and match.group(2):
                    continue
                amount = _as_amount(value)
                if amount is None:
                    continue
                if unit.startswith("day"):
                    days, tolerance = {int(amount)}, 2
                elif unit.startswith("week"):
                    days, tolerance = {int(amount * 7)}, 2
                elif unit.startswith("fortn"):
                    days, tolerance = {int(amount * 14)}, 2
                else:  # month: the unit is inherently approximate
                    days, tolerance = {int(amount * 30), int(amount * 31)}, 4
                if any(
                    any(abs(candidate - real) <= tolerance for real in exact)
                    for candidate in days
                ):
                    continue
                if cited and cited.intersection(days):
                    continue  # quoted from the evidence, not a cadence claim
                problems.append(
                    f"interval claim '{match.group(0).strip()}' does not correspond to "
                    f"any measured interval (real intervals: {facts.intervals}, "
                    f"median {facts.median_interval})"
                )
    return problems


def check_quotes_verbatim(
    response: dict, facts: TimelineFacts, timeline_text: str
) -> list[str]:
    """D: a direct quote must appear word-for-word in the timeline.

    Coined labels are not quotations. "Build-then-Monetize" in single quotes is
    the model naming its own construct, not claiming the competitor said it, so
    a capitalised phrase with no matching timeline text is only rejected when it
    also looks like prose — i.e. it contains a lowercase function word.
    """
    haystack = _norm(timeline_text).lower()
    problems: list[str] = []
    for field in _NARRATIVE_FIELDS:
        body = _norm(str(response.get(field) or ""))
        for match in _QUOTE.finditer(body):
            quote = match.group(1).strip()
            if not quote:
                continue
            probe = re.sub(r"\s+", " ", quote).strip().lower()
            if probe in haystack:
                continue
            # Not in the timeline. Is it plausibly a coined label rather than a
            # claimed quotation? A prose quotation contains function words.
            if not re.search(r"\b(the|a|an|and|of|to|for|with|is|are|from|by|in|on)\b",
                             probe):
                continue
            problems.append(
                f"{field} quotes text that is not in the timeline: {quote[:70]!r}"
            )
    return problems


def check_predicted_date_future(
    response: dict, facts: TimelineFacts, prediction: str
) -> list[str]:
    """E: a forward-looking date must not already be past.

    Two kinds of past date appear in a prediction and only one is a defect.
    "After the 2026-04-15 pricing cut they rebuilt the tier" is citing evidence
    and is fine. "The next expected signal is a messaging announcement on
    2026-08-03" is a deadline the reader will act on, and it closed 56 days ago.
    Rejecting both would burn a retry and downgrade a correct answer, so the
    check requires three things together: the date is real, it is not a date
    with a real signal behind it, and it reads as a target (a deadline cue
    immediately before it) rather than as a narrative reference.
    """
    problems: list[str] = []
    real = set(facts.signal_dates) | {facts.as_of}
    for field in _NARRATIVE_FIELDS:
        # Only the prediction can carry a deadline. The other three fields
        # describe what happened, so a past date there is a citation.
        is_prediction = field == "predicted_next_move"
        body = _norm(prediction if is_prediction else str(response.get(field) or ""))
        for match in _MONTH.finditer(body):
            parsed = _as_date(match)
            if parsed is None or parsed > facts.today:
                continue  # a future prediction target is the point
            iso = parsed.isoformat()
            if iso in real:
                continue  # citing a real signal
            if not is_prediction or parsed == facts.today:
                continue  # evidence fields may cite any past date
            lead = body[max(0, match.start() - 40):match.start()].lower()
            if not re.search(r"\b(by|before|on|due|expected|deadline|target|around)\s*$",
                             lead):
                continue  # narrative reference, not a deadline
            problems.append(
                f"predicted_next_move names {iso} as a future target, but it is before "
                f"today ({facts.today.isoformat()}) and no real signal is dated then"
            )
    return problems


def validate_response(
    response: dict,
    facts: TimelineFacts,
    timeline_text: str,
    *,
    refused: bool = False,
) -> list[str]:
    """Run every check. Returns a list of problems; empty means acceptable."""
    problems: list[str] = []
    prediction = str(response.get("predicted_next_move") or "")
    patterns = str(response.get("patterns") or "")
    intent = str(response.get("inferred_intent") or "")

    if refused:
        # A refusal is held to the calibration rules and nothing else: there is
        # no forecast to check against the timeline.
        problems.extend(check_refusal_calibrated(response, facts))
        return problems

    problems.extend(check_confidence_present(response, facts))
    problems.extend(check_overdue_acknowledged(response, facts, prediction))
    problems.extend(check_interval_claims(response, facts, patterns, intent,
                                         prediction, evidence=timeline_text))
    problems.extend(check_quotes_verbatim(response, facts, timeline_text))
    problems.extend(check_predicted_date_future(response, facts, prediction))
    return problems
