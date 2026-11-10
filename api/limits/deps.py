"""FastAPI dependencies that enforce the demo limits before any LLM call (CT-70, AS-2).

Contract (orchestrator skeleton; TASK-145 implements it without changing names):

- `enforce_chat_limits`: for a persona (`CurrentCustomer`) runs the rate limit
  (`RateLimiter.check_and_record`, committed so the event counts) and then the spend cap
  (`SpendCap.check`); for the operator (`OperatorPrincipal`) only the spend cap (LAC-35).
  Used by `POST /chat`, `POST /chat/stream` and `POST /triage`.
- `enforce_llm_budget`: only the spend cap. Used by `POST /chat/confirmations/{id}` and its
  stream variant.
- Refused -> `AtlasError(RATE_LIMITED, 429, {"retry_at": <datetime>}, lang=<customer language>)`
  or `AtlasError(DAILY_CAP_REACHED, 429, {"retry_at": <next 00:00 UTC>}, lang=...)`; the
  envelope carries `retry_at` (ISO-8601) and the fixed translated message (`api.errors`). A
  refused request never reaches the LLM. The rate limit is checked first, so a refused
  message is not counted against the cap.
- Each dependency reads `now` from `utc_now()` (module function, patched by tests) and its
  settings from `get_settings()`.
"""

from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> datetime:
    return datetime.now(UTC)


async def enforce_chat_limits() -> None:
    raise NotImplementedError("TASK-145")


async def enforce_llm_budget() -> None:
    raise NotImplementedError("TASK-145")
