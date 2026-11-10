"""Input guardrail (CT-54, GRD-01, GRD-06, GRD-90, GRD-93, LAC-13, LAC-28).

`InputGuard(settings).check(text)`: masks PII first (`mask_pii`), then looks for injection in the
text (`find_injection`). Injection -> `block` with the pattern names as reasons; otherwise `allow`
with the masked text. Any internal exception -> `block` with reason `guard_failure` (fail-closed).
`masked_text` is always the masked text (also when blocked), so nothing downstream sees raw PII.

Injection is searched in the masked text AND in the original text, so masking can neither hide
an attack nor create one. When masking itself fails, `masked_text` is empty: the raw text is
never returned.

Phase 14 (TASK-133, OBS-01): every check is one `guardrail` span (CT-62) with the type (`input`),
the verdict and the reasons; never the text.
"""

import contextlib
from dataclasses import dataclass, field
from typing import Literal

from api.config import Settings
from api.guardrails.patterns import find_injection
from api.guardrails.pii import mask_pii
from api.observability.logging import log_event
from api.observability.tracing import start_span

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
        with start_span("guardrail", **{"atlas.guardrail.type": "input"}) as span:
            verdict = self._check(text)
            span.set_attribute("atlas.guardrail.verdict", verdict.action)
            span.set_attribute("atlas.guardrail.reasons", list(verdict.reasons))
            return verdict

    def _check(self, text: str) -> InputVerdict:
        masked_text = ""
        try:
            masked_text = mask_pii(text).text
            reasons = find_injection(masked_text)
            reasons += [name for name in find_injection(text) if name not in reasons]
        except Exception as exc:  # fail-closed (GRD-93, LAC-28): never re-raise
            with contextlib.suppress(Exception):
                log_event("guardrail.input.failure", error_type=type(exc).__name__)
            return InputVerdict(action="block", masked_text=masked_text, reasons=[GUARD_FAILURE])
        if reasons:
            return InputVerdict(action="block", masked_text=masked_text, reasons=reasons)
        return InputVerdict(action="allow", masked_text=masked_text)
