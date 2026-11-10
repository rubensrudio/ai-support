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

Implementation notes (TASK-145):

- The chat routes resolve the principal with `require_customer` and the session through
  `get_chat_service`, while `POST /triage` takes a persona or the operator
  (`require_customer_or_operator`) and `get_db_session`. One FastAPI dependency cannot declare
  both, so `/triage` uses `enforce_triage_limits`, which applies the same rules
  (`apply_chat_limits`) to a `Principal`. In production every one of them is the request
  session (`get_db_session` is cached per request), so the limits share its transaction.
- Order: authentication (401) first, as a sub-dependency. FastAPI runs dependencies before it
  validates the body, so each message dependency peeks at the (already read) body and, when it
  is invalid (schema, blank or too long message), does nothing: the endpoint then answers 422
  and the message consumes neither the rate limit nor the cap check.
"""

import json
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Request
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.deps import (
    CurrentCustomer,
    OperatorPrincipal,
    Principal,
    require_customer,
    require_customer_or_operator,
)
from api.chat.schemas import ChatRequest
from api.chat.service import ChatService, get_chat_service
from api.config import get_settings
from api.db.engine import get_db_session
from api.errors import AtlasError, ErrorCode
from api.limits.rate_limit import RateLimiter
from api.limits.spend_cap import SpendCap
from api.messages import Lang
from api.observability.logging import log_event
from api.triage.schemas import TriageRequest


def utc_now() -> datetime:
    return datetime.now(UTC)


async def enforce_chat_limits(
    request: Request,
    customer: Annotated[CurrentCustomer, Depends(require_customer)],
    service: Annotated[ChatService, Depends(get_chat_service)],
) -> None:
    """`POST /chat` and `POST /chat/stream`: rate limit, then spend cap."""
    message = await _parsed_body(request, ChatRequest)
    if message is None or not _is_valid_message(message.message, service):
        return  # the endpoint answers 422; an invalid message is not counted
    await apply_chat_limits(service.session, customer)


async def enforce_triage_limits(
    request: Request,
    principal: Annotated[Principal, Depends(require_customer_or_operator)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> None:
    """`POST /triage`: persona -> rate limit and spend cap; operator -> spend cap (LAC-35)."""
    if await _parsed_body(request, TriageRequest) is None:
        return  # the endpoint answers 422
    await apply_chat_limits(session, principal)


async def enforce_llm_budget(
    customer: Annotated[CurrentCustomer, Depends(require_customer)],
    service: Annotated[ChatService, Depends(get_chat_service)],
) -> None:
    """Confirmation routes: only the spend cap; a confirmation is not a new message."""
    await _check_spend_cap(service.session, utc_now(), customer.language)


async def apply_chat_limits(session: AsyncSession, principal: Principal) -> None:
    """Rate limit (persona only, committed so the event counts), then the spend cap."""
    now = utc_now()
    if isinstance(principal, OperatorPrincipal):
        await _check_spend_cap(session, now, None)
        return
    decision = await RateLimiter(get_settings()).check_and_record(session, principal.id, now)
    # Commits the event (or just releases the advisory lock when refused) before the LLM runs.
    await session.commit()
    if not decision.allowed:
        log_event("rate_limited")
        raise AtlasError(
            ErrorCode.RATE_LIMITED, 429, {"retry_at": decision.retry_at}, lang=principal.language
        )
    await _check_spend_cap(session, now, principal.language)


async def _check_spend_cap(session: AsyncSession, now: datetime, lang: Lang | None) -> None:
    decision = await SpendCap(get_settings()).check(session, now)
    if not decision.allowed:
        log_event("daily_cap_reached")
        raise AtlasError(
            ErrorCode.DAILY_CAP_REACHED, 429, {"retry_at": decision.resets_at}, lang=lang
        )


async def _parsed_body[M: BaseModel](request: Request, model: type[M]) -> M | None:
    """The request body as `model`, or `None` when FastAPI is about to answer 422."""
    try:
        return model.model_validate(json.loads(await request.body()))
    except (ValueError, ValidationError):
        return None


def _is_valid_message(message: str, service: ChatService) -> bool:
    """Same rules as `ChatService` (CHAT-90/91): not blank and within the length limit."""
    return bool(message.strip()) and len(message) <= service.settings.max_message_chars
