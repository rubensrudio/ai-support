"""Daily restore of the public demo (CT-71, DEMO-07, DEMO-12, DEMO-91, AS-5, AS-3).

Contract (orchestrator skeleton; TASK-146 implements it without changing signatures):

- `restore_demo(session, now)`: in the caller's transaction, deletes every conversation of the
  personas in `PERSONAS` (messages, pending confirmations and feedback go by cascade; tickets
  keep their row with `conversation_id = NULL`, migration 0011) and recreates their orders,
  subscriptions and refunds with `seed_commerce(session, now)`. It does NOT delete
  `audit_log`, `email_outbox`, `tickets`, `llm_calls` or Langfuse traces (DEMO-12, LAC-34):
  those only leave through the 7-day purge. Customers that are not personas are untouched.
  Returns `RestoreReport(conversations_deleted, orders_reset)`; the caller commits.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import delete, select

from api.auth.models import Customer
from api.chat.models import Conversation
from api.seed.commerce import seed_commerce
from api.seed.personas import PERSONAS

if TYPE_CHECKING:
    from sqlalchemy.engine import CursorResult
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class RestoreReport:
    conversations_deleted: int
    orders_reset: int


async def restore_demo(session: AsyncSession, now: datetime) -> RestoreReport:
    usernames = [persona.username for persona in PERSONAS]
    persona_ids = select(Customer.id).where(Customer.username.in_(usernames))
    statement = delete(Conversation).where(Conversation.customer_id.in_(persona_ids))
    result = await session.execute(statement, execution_options={"synchronize_session": False})
    conversations_deleted = int(cast("CursorResult[Any]", result).rowcount)
    seeded = await seed_commerce(session, now)
    return RestoreReport(conversations_deleted=conversations_deleted, orders_reset=seeded.orders)
