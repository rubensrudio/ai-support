"""Input guardrail (CT-54, GRD-01, GRD-06, GRD-90, GRD-93, LAC-13, LAC-28).

`InputGuard(settings).check(text)`: masks PII first (`mask_pii`), then looks for injection in the
text (`find_injection`). Injection -> `block` with the pattern names as reasons; otherwise `allow`
with the masked text. Any internal exception -> `block` with reason `guard_failure` (fail-closed).
`masked_text` is always the masked text (also when blocked), so nothing downstream sees raw PII.
"""

from dataclasses import dataclass, field
from typing import Literal

from api.config import Settings

GUARD_FAILURE = "guard_failure"


@dataclass(frozen=True)
class InputVerdict:
    action: Literal["allow", "block"]
    masked_text: str
    reasons: list[str] = field(default_factory=list)


class InputGuard:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def check(self, text: str) -> InputVerdict:
        raise NotImplementedError("TASK-102")
