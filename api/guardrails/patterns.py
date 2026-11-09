"""Bilingual guardrail patterns (CT-52, GRD-09, GRD-90). Pure regex, no LLM.

- `INJECTION_PATTERNS[lang]`: prompt-injection attempts in en and pt ("ignore previous
  instructions", "ignore as instruções anteriores", "you are now", "system prompt", "revele o
  prompt", requests for other customers' data...). Anchored so legitimate support phrases do not
  match (e.g. "Please ignore the duplicate charge", "ignore a cobrança duplicada").
- `ACTION_PROMISE_PATTERNS[tool][lang]`: sentences that claim an action of `tool` was done
  ("I have cancelled your order", "seu reembolso foi emitido"...), used by the output guard.
- `find_injection(text)`: names of the injection patterns that match `text` in any language
  (empty list = clean). Case- and accent-insensitive.
"""

import re

from api.messages import Lang

INJECTION_PATTERNS: dict[Lang, tuple[re.Pattern[str], ...]] = {"en": (), "pt": ()}
ACTION_PROMISE_PATTERNS: dict[str, dict[Lang, tuple[re.Pattern[str], ...]]] = {}


def find_injection(text: str) -> list[str]:
    raise NotImplementedError("TASK-100")
