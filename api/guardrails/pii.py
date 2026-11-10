"""PII detection and masking (CT-53, GRD-07, OBS-91, LAC-14). Masks, never blocks.

Kinds: `card` (13-19 digits, spaces/dashes allowed, valid Luhn) -> `[CARD]`; `national_id`
(CPF with valid check digits, formatted or not; US SSN `ddd-dd-dddd`) -> `[ID]`; `email` ->
`[EMAIL]`; `phone` (BR and US formats) -> `[PHONE]`. Order numbers such as `#1234` are not PII.
`findings` holds the spans in the ORIGINAL text, sorted by start, never overlapping.

Overlapping candidates are resolved by priority: e-mail (a single token that may contain
digits) > card > national ID > phone. Amounts, dates, order numbers and short numbers never
match: every numeric pattern needs a minimum digit count and is not glued to `#`, `$` or a word;
bare 10-11 digit phones must have a valid BR area code (mobile `9`) or a valid US NANP shape and
must not follow "order"/"pedido".
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

PiiKind = Literal["card", "national_id", "email", "phone"]
MASKS: dict[PiiKind, str] = {
    "card": "[CARD]",
    "national_id": "[ID]",
    "email": "[EMAIL]",
    "phone": "[PHONE]",
}


@dataclass(frozen=True)
class PiiFinding:
    kind: PiiKind
    start: int
    end: int


@dataclass(frozen=True)
class MaskResult:
    text: str
    findings: list[PiiFinding] = field(default_factory=list)


# Not glued to a word, an order marker, a currency sign or another number-ish token.
_START = r"(?<![\w#$@.,/:+-])"
_END = r"(?![\w@]|[.,/:-]\d)"

_EMAIL = re.compile(
    r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"
)
_CARD = re.compile(rf"{_START}\d(?:[ -]?\d){{12,18}}{_END}")
_CPF = re.compile(rf"{_START}(?:\d{{3}}\.\d{{3}}\.\d{{3}}-\d{{2}}|\d{{11}}){_END}")
_SSN = re.compile(rf"{_START}(?!000|666|9)\d{{3}}-(?!00)\d{{2}}-(?!0000)\d{{4}}{_END}")
_PHONES = (
    # BR: +55 11 912345678, +55 (11) 91234-5678
    re.compile(rf"(?<![\w+])\+55[\s-]?(?:\(\d{{2}}\)|\d{{2}})[\s-]?9?\d{{4}}[\s-]?\d{{4}}{_END}"),
    # BR: (11) 91234-5678, (11) 3456-7890
    re.compile(rf"(?<![\w(])\(\d{{2}}\)\s?9?\d{{4}}[\s-]?\d{{4}}{_END}"),
    # BR: 11 91234-5678
    re.compile(rf"{_START}\d{{2}}[\s-]9?\d{{4}}-\d{{4}}{_END}"),
    # US: +1 415-555-0132, +1 (415) 555-0132
    re.compile(rf"(?<![\w+])\+1[\s.-]?(?:\(\d{{3}}\)|\d{{3}})[\s.-]?\d{{3}}[\s.-]?\d{{4}}{_END}"),
    # US: (415) 555-0132
    re.compile(rf"(?<![\w(])\(\d{{3}}\)\s?\d{{3}}[\s.-]?\d{{4}}{_END}"),
    # US: 415-555-0132, 415.555.0132
    re.compile(rf"{_START}\d{{3}}([.-])\d{{3}}\1\d{{4}}{_END}"),
)
# Bare numbers right after "order"/"pedido" are order numbers, not phones.
_NOT_ORDER = r"(?<![Oo]rder\s)(?<![Pp]edido\s)(?<![Ii]nvoice\s)(?<![Ff]atura\s)"
# BR with area code, unformatted or space-separated: 11912345678, 11 91234 5678, 1191234-5678.
_BR_PLAIN = re.compile(rf"{_NOT_ORDER}{_START}\d{{2}}[\s-]?9?\d{{4}}[\s-]?\d{{4}}{_END}")
# US 10 digits, unformatted or space-separated: 4155550132, 415 555 0132.
_US_PLAIN = re.compile(rf"{_NOT_ORDER}{_START}\d{{3}}[\s.-]?\d{{3}}[\s.-]?\d{{4}}{_END}")


def _digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def _luhn_ok(digits: str) -> bool:
    total = 0
    for index, ch in enumerate(reversed(digits)):
        value = int(ch)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _is_card(value: str) -> bool:
    digits = _digits(value)
    return 13 <= len(digits) <= 19 and len(set(digits)) > 1 and _luhn_ok(digits)


def _cpf_ok(value: str) -> bool:
    digits = [int(ch) for ch in _digits(value)]
    if len(digits) != 11 or len(set(digits)) == 1:
        return False
    for size in (9, 10):
        total = sum(d * w for d, w in zip(digits[:size], range(size + 1, 1, -1), strict=True))
        check = (total * 10) % 11 % 10
        if check != digits[size]:
            return False
    return True


def _always(value: str) -> bool:
    return True


def _br_phone_ok(value: str) -> bool:
    """Area code 11-99; 11 digits = mobile (9 after the area code); 10 = landline (2-5)."""
    digits = _digits(value)
    if "0" in digits[:2]:
        return False
    if len(digits) == 11:
        return digits[2] == "9"
    return len(digits) == 10 and digits[2] in "2345"


def _us_phone_ok(value: str) -> bool:
    """NANP: area code and exchange never start with 0 or 1."""
    digits = _digits(value)
    return len(digits) == 10 and digits[0] not in "01" and digits[3] not in "01"


_Detector = tuple[PiiKind, re.Pattern[str], Callable[[str], bool]]

# Priority order: earlier detectors win overlaps.
_DETECTORS: tuple[_Detector, ...] = (
    ("email", _EMAIL, _always),
    ("card", _CARD, _is_card),
    ("national_id", _CPF, _cpf_ok),
    ("national_id", _SSN, _always),
    *(("phone", pattern, _always) for pattern in _PHONES),
    ("phone", _BR_PLAIN, _br_phone_ok),
    ("phone", _US_PLAIN, _us_phone_ok),
)


def _overlaps(finding: PiiFinding, accepted: list[PiiFinding]) -> bool:
    return any(finding.start < other.end and other.start < finding.end for other in accepted)


def mask_pii(text: str) -> MaskResult:
    accepted: list[PiiFinding] = []
    for kind, pattern, valid in _DETECTORS:
        for match in pattern.finditer(text):
            finding = PiiFinding(kind=kind, start=match.start(), end=match.end())
            if valid(match.group()) and not _overlaps(finding, accepted):
                accepted.append(finding)
    findings = sorted(accepted, key=lambda f: f.start)

    parts: list[str] = []
    cursor = 0
    for finding in findings:
        parts.append(text[cursor : finding.start])
        parts.append(MASKS[finding.kind])
        cursor = finding.end
    parts.append(text[cursor:])
    return MaskResult(text="".join(parts), findings=findings)
