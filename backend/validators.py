"""Post-hoc validation of a strategic read.

The prompt tells the model the rules. Nothing in the prompt *enforces* them, and
the audit showed why: seven of seven forecasting reads came back with no
confidence label, and six stated a cadence their own timeline contradicted. A
model will comply with a rule it can see and ignore one it can weigh against
finishing the sentence.

So the rules that can be checked mechanically are checked mechanically here.
A read that fails is retried once with the failures quoted back, then replaced
by a refusal-shaped response. A read that cannot be trusted is worse than no
read, because the reader has no way to tell.

Each check maps to a failure mode from the audit:

  A  repetition  — a repeating/cycle/loop claim whose transition the facts
                   show fewer than twice
  B  staleness   — a forecast on an overdue stream that neither mentions the
                   gap nor drops below high confidence
  C  trend       — a prediction that skips a stage of the model's own declared
                   cycle without explaining why
  D  calibration — a missing confidence or missing_evidence field, or a quote
                   that is not verbatim in the timeline
  E  arithmetic  — a cadence, interval, or predicted date the facts do not
                   support
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from .models import Signal
from .synthesis import Facts

# Words that assert a sequence repeats. Deliberately narrow: "pattern" on its
# own can describe a single arrangement, and flagging it produced false failures
# on reads that were merely describing a shape once.
REPEAT_CLAIM = re.compile(
    r"\b(repeats?|repeated|repeating|recurring|recurs|a\s+loop|loops|cyclic|"
    r"a\s+cycle|cycles|rotation|rotates|every\s+\w+\s+days?|each\s+time)\b",
    re.I,
)
SINGLE_INSTANCE = re.compile(
    r"\bone\s+observed\s+instance|single\s+instance|one\s+instance|"
    r"one\s+occurrence|only\s+once|appears?\s+once", re.I
)
GAP_ACK = re.compile(
    r"\boverdue\b|\bsince\s+the\s+(?:last|final|most\s+recent)\b|"
    r"no\s+signal\s+(?:has\s+)?(?:since|for)\b|has\s+not\s+(?:produced|emitted|shipped|published)\b|"
    r"gone\s+quiet|quiet\s+for|quiet\s+since|\bsilence\b|\bsilent\b|"
    r"stal(?:e|eness)\b|\bgap\s+of\b|\b\d+\s+days?\s+(?:since|without|overdue)\b|"
    r"\bsince\s+\d{4}-\d{2}-\d{2}\b|"
    r"\bsince\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep)[a-z]*\s+\d{1,2}\b",
    re.I,
)
QUOTE = re.compile(r"['\"‘’“]([^'\"’”\n]{4,80})['\"’”]")
HYPOTHETICAL = re.compile(
    r"(e\.g\.|i\.e\.|such\s+as|for\s+example|like\s+['\"]|such\s+a)", re.I
)
# A named label the model coined for a sequence it is describing. These are not
# quotes from the timeline and must not be checked as such.
COINED = re.compile(
    r"^[\w\s-]{0,30}(cycle|monetisation|monetization|cadence|phase|stage|"
    r"pattern|shift|motion|move|moves|arc|build-then-monetize)[\w\s-]{0,20}$", re.I
)
ISO_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
NUM_UNIT = re.compile(r"\b(\d+)\s*[-–]?\s*(day|week|month|interval|signal)s?\b", re.I)
CONF_LEVELS = ("high", "medium", "low", "none")
WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}
UNIT_DAYS = {"day": 1, "week": 7, "month": 30}

# Signal-type vocabulary. Order matters: "messaging" must be tested before the
# bare "message" stem so a type is not mis-parsed as free prose.
TYPE_WORDS = (
    "funding", "hiring", "pricing", "messaging", "feature",
    "launch", "announce", "hire", "price", "release", "ship",
)
STAGE_PATTERNS = (
    ("funding", r"\bfund(?:ing|ed|raise|raises|round)\b"),
    ("hiring", r"\bhir(?:ing|e|ed|es)\b|\broles?\b|\bjobs?\b|\bheadcount\b"),
    ("pricing", r"\bpric(?:ing|e|ed|es|tier|tiers)\b|\bdiscount\b|\bbundle[ds]?\b|\bfee[s]?\b"),
    ("messaging", r"\bmessag(?:ing|e|ed|es)\b|\bcampaign\b|\brebrand(?:ing|ed)?\b|\bpositioning\b"),
    ("feature", r"\bfeature[s]?\b|\bship(?:s|ped|ping)?\b|\brelease[ds]?\b|\blaunch(?:es|ed)?\b"),
)


def _parse_iso(value: str) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def _number(token: str) -> int | None:
    token = token.strip().lower()
    if token.isdigit():
        return int(token)
    return WORD_NUMBERS.get(token)


@dataclass
class ValidationResult:
    ok: bool
    failures: list[str] = field(default_factory=list)
    modes: set[str] = field(default_factory=set)

    def note(self, mode: str, message: str) -> None:
        self.ok = False
        self.modes.add(mode)
        self.failures.append(f"[{mode}] {message}")


def is_refusal(text: str) -> bool:
    """A read that declines to predict. Treated as valid, not as a failure."""
    blob = " ".join(text).lower()
    return bool(
        re.search(
            r"evidence insufficient|insufficient evidence|insufficient signal|"
            r"cannot determine|cannot infer|no (?:concrete|reliable) prediction|"
            r"not predictable|cannot be made", blob
        )
    )


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text or "") if s.strip()]


def check_arithmetic(
    result: ValidationResult,
    text: str,
    facts: Facts,
    today: date,
    signal_dates: set[str],
) -> None:
    """E: every cited number and date must be supported by the facts."""
    allowed = facts.allowed_numbers

    for match in NUM_UNIT.finditer(text):
        raw, unit = match.group(1), match.group(2).lower()
        value = _number(raw)
        if value is None:
            continue
        days = value * UNIT_DAYS[unit]
        # A horizon ("within 6 weeks") is a forecast window, not a measurement
        # of the timeline. Those are checked against today below instead.
        context = text[max(0, match.start() - 30): match.end() + 12]
        if re.search(r"within|next\s+\w+\s+(?:to|-)\b|by\s+the", context, re.I):
            continue
        if days in allowed:
            continue
        # A claimed range: judge it by whether the real intervals fit inside it.
        low, high = _enclosing_range(text, match, unit)
        if low is not None:
            span = (high - low) * UNIT_DAYS[unit]
            if low * UNIT_DAYS[unit] <= span and _range_contains_all(low, high, unit, facts):
                continue
        result.note(
            "E",
            f'cites "{match.group(0).strip()}" but the computed intervals are '
            f"{list(facts.intervals)} (median {facts.median_interval}); "
            "only numbers in the FACTS block may be quoted",
        )

    for token in ISO_DATE.findall(text):
        parsed = _parse_iso(token)
        if parsed is None:
            result.note("E", f"cites an unparseable date {token!r}")
            continue
        if parsed > today:
            continue  # a forward-looking target: allowed
        if token not in signal_dates:
            result.note(
                "E",
                f"cites past date {token}, which is not a signal date; "
                "past dates may only be referenced as evidence that exists in the timeline",
            )

    # A cadence continuation that lands in the past while the model forecasts
    # forward. This is the Brightline failure: last signal + 28 days was six
    # weeks ago, and the read named a date three months out with no accounting.
    for match in re.finditer(
        r"(?:next\s+expected|would\s+be\s+due|the\s+next\s+signal[^.]{0,40})"
        r"[^.]{0,60}?(\d+)\s*days?\s*later", text, re.I
    ):
        span = int(match.group(1))
        if not facts.intervals or facts.days_since_last == 0:
            continue
        implied = facts.days_since_last - span
        if implied >= 0 and span == facts.median_interval:
            result.note(
                "E",
                f"describes a next signal {span} days after the last one, which is "
                f"{-implied} days ago (the stream is {facts.days_since_last} days old), "
                "then forecasts forward without accounting for the skipped cycles",
            )


def _enclosing_range(text: str, match: re.Match, unit: str) -> tuple[int, int] | None:
    window = text[max(0, match.start() - 12): match.end() + 12]
    pair = re.search(r"(\d+)\s*[-–to]+\s*(\d+)\s*" + unit, window, re.I)
    if pair:
        return int(pair.group(1)), int(pair.group(2))
    return None


def _range_contains_all(low: int, high: int, unit: str, facts: Facts) -> bool:
    """True when every real interval lies inside the claimed range."""
    mult = UNIT_DAYS[unit]
    if not facts.intervals:
        return False
    return all(low * mult - 1 <= gap <= high * mult + 1 for gap in facts.intervals)


def check_repetition(result: ValidationResult, text: str, facts: Facts) -> None:
    """A: a sequence may only be called repeating if the facts show it twice."""
    if not facts.transitions:
        return
    repeatable = facts.repeatable_transitions
    for sentence in _sentences(text):
        if not REPEAT_CLAIM.search(sentence):
            continue
        if SINGLE_INSTANCE.search(sentence):
            continue
        # Which transition is this sentence about? Look for a type pair, else
        # fall back to the single most frequent transition.
        cited = _transition_in_sentence(sentence)
        if cited is None:
            continue
        if cited in repeatable:
            continue
        count = next((n for a, b, n in facts.transitions if (a, b) == cited), 0)
        result.note(
            "A",
            f'"{sentence}" calls a {cited[0]} -> {cited[1]} sequence repeating, '
            f"but the computed transitions show it occurs {count} time(s); "
            "a sequence seen once must be called 'one observed instance'",
        )


def _transition_in_sentence(sentence: str) -> tuple[str, str] | None:
    """Find the (from, to) signal-type pair a sentence is about."""
    found = [(m.start(), m.lastindex) for m in re.finditer("|".join(TYPE_WORDS), sentence, re.I)]
    types = [sentence[m.start() if isinstance(m.start(), int) else 0] for m in []]
    # Simpler: walk the matched words in order and map them to signal types.
    words: list[tuple[int, str]] = []
    for word in TYPE_WORDS:
        for m in re.finditer(rf"\b{word}\w*\b", sentence, re.I):
            words.append((m.start(), word))
    words.sort()
    mapped: list[str] = []
    for _, word in words:
        for canonical, pattern in STAGE_PATTERNS:
            if re.fullmatch(pattern, word, re.I):
                mapped.append(canonical)
                break
    if len(mapped) >= 2:
        return (mapped[0], mapped[1])
    return None


def check_staleness(result: ValidationResult, fields: dict[str, str], facts: Facts) -> None:
    """B: an overdue stream must be acknowledged, and must not read as high confidence."""
    if facts.days_overdue <= 0:
        return
    prediction = fields.get("predicted_next_move", "")
    if not GAP_ACK.search(prediction):
        result.note(
            "B",
            f"the stream is {facts.days_overdue} days overdue against a "
            f"{facts.median_interval}-day median, but predicted_next_move does not "
            "acknowledge the gap: "
            f'"{_trim(prediction)}"',
        )
    confidence = (fields.get("confidence") or "").strip().lower()
    if confidence == "high":
        result.note(
            "B",
            f"confidence is 'high' on evidence {facts.days_overdue} days overdue; "
            "an overdue stream caps confidence at medium",
        )


def check_consistency(result: ValidationResult, fields: dict[str, str], facts: Facts) -> None:
    """C: a prediction may not silently skip a stage of the model's own cycle."""
    prediction = fields.get("predicted_next_move", "")
    if is_refusal(prediction) or not prediction:
        return
    declared = _declared_cycle(fields.get("patterns", ""))
    if not declared:
        return
    last_type = _last_signal_type(facts)
    if last_type is None or last_type not in declared:
        return
    expected = declared[(declared.index(last_type) + 1) % len(declared)]
    actual = _predicted_type(prediction)
    if actual is None or actual == expected:
        return
    if re.search(r"why|because|despite|although|however|even so|skip|depart", prediction, re.I):
        return  # it explained the departure
    result.note(
        "C",
        f'predicted_next_move predicts a {actual} signal, but the patterns field '
        f'declares the cycle {" -> ".join(declared)} and the last signal is '
        f"{last_type}, so the next stage is {expected}; the forecast does not say "
        f"why it departs: "{_trim(prediction)}"",
    )


def _last_signal_type(facts: Facts) -> str | None:
    for line in facts.last_three:
        match = re.search(r"\((\w+)\)", line)
        if match:
            return match.group(1).lower()
    return None


def _declared_cycle(patterns: str) -> list[str] | None:
    """Recover a stage order the model states in 'patterns'."""
    for length in (4, 3):
        words: list[tuple[int, str]] = []
        for canonical, pattern in STAGE_PATTERNS:
            for m in re.finditer(pattern, patterns, re.I):
                words.append((m.start(), canonical))
        words.sort()
        cycle: list[str] = []
        for _, canonical in words:
            if not cycle or cycle[-1] != canonical:
                cycle.append(canonical)
        if len(cycle) >= length:
            return cycle[:length]
    return None


def _predicted_type(prediction: str) -> str | None:
    for canonical, pattern in STAGE_PATTERNS:
        if re.search(pattern, prediction, re.I):
            return canonical
    return None


def check_calibration(
    result: ValidationResult,
    fields: dict[str, str],
    facts: Facts,
    signals: list[Signal],
) -> None:
    """D: the confidence fields must exist, and quotes must be verbatim."""
    confidence = (fields.get("confidence") or "").strip().lower()
    if confidence not in CONF_LEVELS:
        result.note(
            "D",
            f'"confidence" is {confidence or "missing"!r}; it must be one of '
            f"{list(CONF_LEVELS)}",
        )
    missing = (fields.get("missing_evidence") or "").strip()
    if len(missing) < 12:
        result.note(
            "D",
            '"missing_evidence" must be one sentence naming the additional signal '
            f'that would raise confidence; got {missing!r}',
        )
    elif re.search(r"none\s+needed|n/?a$|nothing", missing, re.I):
        result.note("D", f'"missing_evidence" is a non-answer: {missing!r}')

    blob = " ".join(
        f"{s.summary} {s.raw_notes or ''}" for s in signals
    ).lower()
    for field_name in ("patterns", "inferred_intent"):
        value = fields.get(field_name, "") or ""
        for match in QUOTE.finditer(value):
            quote = match.group(1)
            before = value[max(0, match.start() - 16): match.start()].lower()
            if HYPOTHETICAL.search(before):
                continue
            if COINED.match(quote.strip()):
                continue  # a label the model coined, not a quotation
            probe = re.sub(r"\s+", " ", re.sub(r"[^\w\s-]", "", quote)).strip().lower()
            if not probe or probe in blob:
                continue
            if _mostly_in(quote, blob):
                continue
            result.note(
                "D",
                f'{field_name} quotes "{quote}", which does not appear verbatim in '
                "any signal summary",
            )


def _mostly_in(quote: str, blob: str, threshold: float = 0.7) -> bool:
    """Tolerate light ellipsis inside a quote without waving real errors through."""
    words = [w for w in re.findall(r"\w+", quote.lower()) if len(w) > 3]
    if not words:
        return False
    hits = sum(1 for w in words if w in blob)
    return hits / len(words) >= threshold


def _trim(text: str, limit: int = 180) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def validate_read(
    fields: dict[str, str],
    facts: Facts,
    signals: list[Signal],
    today: date,
) -> ValidationResult:
    """Run every check. A refusal is exempt from A, B, C and E."""
    result = ValidationResult(ok=True)
    prediction = fields.get("predicted_next_move", "")
    if is_refusal([fields.get("patterns", ""), fields.get("inferred_intent", ""), prediction]):
        # A refusal still owes the reader a statement of what is missing.
        check_calibration(result, fields, facts, signals)
        return result

    signal_dates = {s.date for s in signals}
    narrative = " ".join(
        fields.get(name, "") for name in ("patterns", "inferred_intent")
    )
    check_repetition(result, narrative, facts)
    check_staleness(result, fields, facts)
    check_consistency(result, fields, facts)
    check_calibration(result, fields, facts, signals)
    check_arithmetic(result, " ".join(fields.values()), facts, today, signal_dates)
    return result


RETRY_INSTRUCTIONS = """
YOUR PREVIOUS RESPONSE WAS REJECTED BY VALIDATION. These are the exact defects:

{defects}

Return a corrected JSON object. Fix every item above. Do not argue the
findings and do not restate your previous answer. Keep what was already
correct, and recompute nothing: the FACTS block above is still the only
source of numbers."""


def retry_prompt(base_prompt: str, failures: list[str]) -> str:
    return base_prompt + RETRY_INSTRUCTIONS.format(
        defects="\n".join(f"- {f}" for f in failures)
    )
