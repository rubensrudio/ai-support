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
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
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
    raise NotImplementedError("TASK-147")
