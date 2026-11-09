"""PII detection and masking (CT-53, GRD-07, OBS-91, LAC-14). Masks, never blocks.

Kinds: `card` (13-19 digits, spaces/dashes allowed, valid Luhn) -> `[CARD]`; `national_id`
(CPF with valid check digits, formatted or not; US SSN `ddd-dd-dddd`) -> `[ID]`; `email` ->
`[EMAIL]`; `phone` (BR and US formats) -> `[PHONE]`. Order numbers such as `#1234` are not PII.
`findings` holds the spans in the ORIGINAL text, sorted by start, never overlapping.
"""

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


def mask_pii(text: str) -> MaskResult:
    raise NotImplementedError("TASK-101")
