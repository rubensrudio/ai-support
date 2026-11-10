"""Feedback on assistant messages (CT-65, OBS-02, OBS-92).

Contract (orchestrator skeleton; TASK-136 implements it without changing the route):

`POST /messages/{message_id}/feedback` with body `{"rating": "up" | "down"}` and the persona
session cookie (`current_customer`, CT-16):

- 204 with no body: the message exists, has `role == "assistant"` and belongs to a conversation
  of the session customer. The feedback row is upserted (one per message; a second call replaces
  the rating) with the message `trace_id`, and the score (up = 1, down = -1) is pushed to
  Langfuse in the background (`LangfuseApi.push_score`, best effort; a failure never changes the
  response).
- 404 `MESSAGE_NOT_FOUND`: the message does not exist, is not an assistant message or belongs to
  another customer. Same body for all three cases and nothing is written (OBS-92).
- 401 `AUTH_REQUIRED`: no valid session. 422 for an invalid body.

Ownership (AS-1) is decided by a single query that joins `conversations` and filters on the
session customer and the assistant role, so the three not-found cases are indistinguishable.
"""

from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Path, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.deps import CurrentCustomer, require_customer
from api.chat.models import Conversation, Message
from api.config import get_settings
from api.db.engine import get_db_session
from api.errors import AtlasError, ErrorCode
from api.feedback.models import Feedback, Rating
from api.observability.langfuse_api import LangfuseApi
from api.observability.logging import log_event

feedback_router = APIRouter(tags=["feedback"])

MAX_BIGINT = 2**63 - 1
SCORE_BY_RATING: dict[Rating, int] = {"up": 1, "down": -1}


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rating: Rating


@lru_cache(maxsize=1)
def _langfuse_api() -> LangfuseApi:
    """One Langfuse client per process, built from the settings on first use."""
    return LangfuseApi(get_settings())


def get_langfuse_api() -> LangfuseApi:
    """FastAPI dependency: the shared Langfuse client. Tests override it with a fake."""
    return _langfuse_api()


async def close_langfuse_api() -> None:
    """Close the shared Langfuse client if it was created (API lifespan shutdown)."""
    if _langfuse_api.cache_info().currsize:
        await _langfuse_api().aclose()
        _langfuse_api.cache_clear()


def score_id_for(message_id: int, trace_id: str) -> str:
    """Deterministic Langfuse score id: a changed rating upserts the same score.

    The trace id is part of the key so a recreated database (restarted `messages.id`
    sequence) never upserts over a score that Langfuse keeps for another trace.
    """
    return f"feedback-{trace_id}-{message_id}"


async def _push_score(langfuse: LangfuseApi, trace_id: str, value: int, score_id: str) -> None:
    """Best effort: any failure is logged by type and never reaches the client."""
    try:
        await langfuse.push_score(trace_id, value, score_id=score_id)
    except Exception as exc:  # background best effort (OBS-02)
        log_event("feedback_score_push_failed", error_type=type(exc).__name__)


@feedback_router.post("/messages/{message_id}/feedback", status_code=204)
async def post_feedback(
    message_id: Annotated[int, Path(ge=1, le=MAX_BIGINT)],
    body: FeedbackRequest,
    background: BackgroundTasks,
    customer: Annotated[CurrentCustomer, Depends(require_customer)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    langfuse: Annotated[LangfuseApi, Depends(get_langfuse_api)],
) -> Response:
    trace_id = await session.scalar(
        select(Message.trace_id)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Message.id == message_id,
            Message.role == "assistant",
            Conversation.customer_id == customer.id,
        )
    )
    if trace_id is None:
        raise AtlasError(ErrorCode.MESSAGE_NOT_FOUND, 404)

    statement = insert(Feedback).values(
        message_id=message_id,
        customer_id=customer.id,
        rating=body.rating,
        trace_id=trace_id,
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[Feedback.message_id],
            set_={"rating": statement.excluded.rating, "created_at": func.now()},
        )
    )
    await session.commit()

    background.add_task(
        _push_score,
        langfuse,
        trace_id,
        SCORE_BY_RATING[body.rating],
        score_id_for(message_id, trace_id),
    )
    return Response(status_code=204)
