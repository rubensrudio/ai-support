"""Daily demo restore (TASK-146, CT-71, DEMO-07, DEMO-12, DEMO-91)."""

import uuid
from collections import Counter
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.chat.models import Conversation, Message
from api.maintenance.restore import RestoreReport, restore_demo
from api.seed.commerce import seed_commerce
from api.seed.personas import PERSONAS, seed_personas
from api.tickets.models import Ticket
from api.tools.models import AuditLog, EmailOutbox, Order, OrderStatus
from tests.integration.api.maintenance.conftest import make_conversation, make_customer

pytestmark = pytest.mark.integration

PASSWORD = "persona-password-for-tests"
NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


async def _count(session: AsyncSession, model: type[object]) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def _statuses(session: AsyncSession, customer_id: uuid.UUID) -> Counter[str]:
    rows = await session.scalars(select(Order.status).where(Order.customer_id == customer_id))
    return Counter(rows.all())


async def _conversations_of(session: AsyncSession, ids: list[uuid.UUID]) -> int:
    statement = select(func.count()).select_from(Conversation)
    return int(await session.scalar(statement.where(Conversation.customer_id.in_(ids))) or 0)


async def test_restore_resets_orders_and_deletes_persona_conversations_only(
    db_session: AsyncSession,
) -> None:
    persona_ids = await seed_personas(db_session, PASSWORD)
    seeded = await seed_commerce(db_session, NOW)
    seeded_statuses = await _statuses(db_session, persona_ids[0])
    visitor = await make_customer(db_session)
    visitor_conversation_id = (await make_conversation(db_session, visitor.id)).id

    cancellable = await db_session.scalar(
        select(Order).where(
            Order.customer_id == persona_ids[0], Order.status == OrderStatus.PAID.value
        )
    )
    assert cancellable is not None
    cancellable.status = OrderStatus.CANCELLED.value
    cancellable.cancelled_at = NOW
    conversation = await make_conversation(db_session, persona_ids[0])
    await make_conversation(db_session, persona_ids[1])
    db_session.add(
        Ticket(
            conversation_id=conversation.id,
            customer_id=persona_ids[0],
            reason="all_providers_failed",
            trace_id=uuid.uuid4().hex,
        )
    )
    db_session.add(
        AuditLog(
            customer_id=persona_ids[0],
            tool="cancel_order",
            arguments={"order_id": cancellable.id},
            result={"status": "cancelled"},
            outcome="success",
            trace_id=uuid.uuid4().hex,
        )
    )
    db_session.add(
        EmailOutbox(
            customer_id=persona_ids[0],
            recipient="alex@example.com",
            subject="Your order was cancelled",
            body="Order cancelled.",
            trace_id=uuid.uuid4().hex,
        )
    )
    await db_session.flush()
    assert await _statuses(db_session, persona_ids[0]) != seeded_statuses
    before = {model: await _count(db_session, model) for model in (AuditLog, EmailOutbox, Ticket)}

    report = await restore_demo(db_session, NOW)
    db_session.expire_all()

    assert report == RestoreReport(conversations_deleted=2, orders_reset=seeded.orders)
    assert await _statuses(db_session, persona_ids[0]) == seeded_statuses
    assert await _conversations_of(db_session, persona_ids) == 0
    assert {model: await _count(db_session, model) for model in before} == before
    ticket = await db_session.scalar(select(Ticket).where(Ticket.customer_id == persona_ids[0]))
    assert ticket is not None and ticket.conversation_id is None
    assert await db_session.get(Conversation, visitor_conversation_id) is not None
    visitor_messages = await db_session.scalar(
        select(func.count())
        .select_from(Message)
        .where(Message.conversation_id == visitor_conversation_id)
    )
    assert visitor_messages == 1


async def test_restore_is_repeatable_and_keeps_the_seed(db_session: AsyncSession) -> None:
    persona_ids = await seed_personas(db_session, PASSWORD)
    await make_conversation(db_session, persona_ids[1])

    first = await restore_demo(db_session, NOW)
    second = await restore_demo(db_session, NOW)

    assert first.conversations_deleted == 1
    assert second.conversations_deleted == 0
    assert second.orders_reset == first.orders_reset > 0
    orders = await db_session.scalar(
        select(func.count()).select_from(Order).where(Order.customer_id.in_(persona_ids))
    )
    assert orders == first.orders_reset
    assert len(persona_ids) == len(PERSONAS)
