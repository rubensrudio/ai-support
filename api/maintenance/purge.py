"""Seven-day retention purge, Langfuse traces included (CT-72, DEMO-10, DEMO-12, AS-5, AS-3).

Contract (orchestrator skeleton; TASK-147 implements it without changing signatures):

- `purge_expired(session, now, retention_days, langfuse)`: cutoff = `now - retention_days`.
  Deletes rows with `created_at < cutoff` from `conversations` (messages, feedback and pending
  confirmations by cascade), `audit_log`, `email_outbox`, `tickets` and `llm_calls`, then calls
  `langfuse.delete_traces_older_than(cutoff)` when `langfuse` is given and enabled (LAC-36).
  Never deletes customers, sessions, the seed tables, documents or chunks. Idempotent: a
  second run deletes nothing. Returns `PurgeReport` with the count per target; a Langfuse
  failure only lowers `langfuse_traces` (the client never raises). The caller commits.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import delete

from api.chat.models import Conversation
from api.limits.models import LlmCall, RateLimitEvent
from api.tickets.models import Ticket
from api.tools.models import AuditLog, EmailOutbox

if TYPE_CHECKING:
    from sqlalchemy.engine import CursorResult
    from sqlalchemy.ext.asyncio import AsyncSession

    from api.observability.langfuse_api import LangfuseApi


@dataclass(frozen=True)
class PurgeReport:
    conversations: int
    audit_rows: int
    emails: int
    tickets: int
    llm_calls: int
    langfuse_traces: int


async def purge_expired(
    session: AsyncSession,
    now: datetime,
    retention_days: int,
    langfuse: LangfuseApi | None,
) -> PurgeReport:
    cutoff = now - timedelta(days=retention_days)

    async def purge(model: type[Any]) -> int:
        statement = delete(model).where(model.created_at < cutoff)
        result = await session.execute(statement, execution_options={"synchronize_session": False})
        return int(cast("CursorResult[Any]", result).rowcount)

    # Conversations first: their messages, feedback and pending confirmations go by cascade and
    # younger tickets keep their row with `conversation_id = NULL` (migration 0011).
    conversations = await purge(Conversation)
    audit_rows = await purge(AuditLog)
    emails = await purge(EmailOutbox)
    tickets = await purge(Ticket)
    llm_calls = await purge(LlmCall)
    await purge(RateLimitEvent)
    traces = 0
    if langfuse is not None and langfuse.enabled:
        traces = await langfuse.delete_traces_older_than(cutoff)
    return PurgeReport(
        conversations=conversations,
        audit_rows=audit_rows,
        emails=emails,
        tickets=tickets,
        llm_calls=llm_calls,
        langfuse_traces=traces,
    )
