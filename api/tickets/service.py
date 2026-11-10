"""Ticket opening (CT-61, RES-06). Records conversation, customer, reason and trace; notifies no
one. Flushes in the caller's transaction (no commit)."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from api.tickets.models import Ticket


async def open_ticket(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    customer_id: uuid.UUID,
    reason: str,
    trace_id: str,
) -> Ticket:
    ticket = Ticket(
        conversation_id=conversation_id,
        customer_id=customer_id,
        reason=reason,
        trace_id=trace_id,
    )
    session.add(ticket)
    await session.flush()
    return ticket
