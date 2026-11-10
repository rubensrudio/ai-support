"""Daily demo restore contract (CT-71, DEMO-07, DEMO-12, DEMO-91, LAC-34, AS-5).

`restore_demo(session, now)` puts the personas' orders, subscriptions and refunds back to the seed,
deletes every conversation of the personas (messages, feedback and pending confirmations go with
them) and keeps `audit_log`, `email_outbox`, `tickets` (with `conversation_id` NULL) and
`llm_calls`: those leave only through the 7-day purge. Customers that are not personas are never
touched.
"""

import uuid
from datetime import datetime, timedelta
from typing import Any

import pytest
from maintenance_contract_helpers import ActivityFn, ExistingFn, Persona
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.models import Customer
from api.chat.models import Conversation
from api.maintenance.restore import RestoreReport, restore_demo
from api.tickets.models import Ticket
from api.tools.models import Order, OrderStatus, Refund, Subscription

pytestmark = pytest.mark.integration

Snapshot = dict[str, Any]


async def _commerce(session: AsyncSession, customer_id: uuid.UUID) -> Snapshot:
    """Order-number independent view of a customer's commerce rows."""
    session.expire_all()
    orders = (await session.scalars(select(Order).where(Order.customer_id == customer_id))).all()
    subscriptions = (
        await session.scalars(select(Subscription).where(Subscription.customer_id == customer_id))
    ).all()
    refunds = await session.scalar(
        select(func.count()).select_from(Refund).where(Refund.customer_id == customer_id)
    )
    return {
        "orders": sorted(
            (
                order.status,
                order.total_cents,
                order.currency,
                repr(order.items),
                order.shipped_at,
                order.delivered_at,
                order.cancelled_at,
            )
            for order in orders
        ),
        "subscriptions": sorted(
            (sub.plan_code, sub.status, sub.price_cents, sub.renews_at) for sub in subscriptions
        ),
        "refunds": int(refunds or 0),
    }


async def _count_conversations(session: AsyncSession, customer_id: uuid.UUID) -> int:
    value = await session.scalar(
        select(func.count())
        .select_from(Conversation)
        .where(Conversation.customer_id == customer_id)
    )
    return int(value or 0)


async def _mess_up_commerce(session: AsyncSession, personas: dict[str, Persona]) -> None:
    """What a day of demo use does: cancelled orders, a refund, a lost subscription, a new order."""
    alex, bia = personas["alex"], personas["bia"]
    await session.execute(
        update(Order)
        .where(Order.customer_id == alex.id)
        .values(status=OrderStatus.CANCELLED.value, cancelled_at=func.now())
    )
    order_id = await session.scalar(select(Order.id).where(Order.customer_id == alex.id).limit(1))
    assert order_id is not None
    session.add(
        Refund(order_id=order_id, customer_id=alex.id, amount_cents=500, trace_id="t-refund")
    )
    await session.execute(
        update(Subscription).where(Subscription.customer_id == bia.id).values(status="cancelled")
    )
    session.add(
        Order(
            customer_id=bia.id,
            status=OrderStatus.PAID.value,
            total_cents=99_999,
            currency="USD",
            items=[{"sku": "extra", "name": "Extra", "qty": 9}],
        )
    )
    await session.flush()


async def test_restore_puts_persona_commerce_back_to_the_seed(
    db_session: AsyncSession, personas: dict[str, Persona], now: datetime
) -> None:
    seeded = {name: await _commerce(db_session, p.id) for name, p in personas.items()}
    assert all(snapshot["orders"] for snapshot in seeded.values()), "seed has no orders"
    await _mess_up_commerce(db_session, personas)
    assert await _commerce(db_session, personas["alex"].id) != seeded["alex"]
    assert await _commerce(db_session, personas["bia"].id) != seeded["bia"]

    report = await restore_demo(db_session, now)
    await db_session.flush()

    assert isinstance(report, RestoreReport)
    for name, persona in personas.items():
        assert await _commerce(db_session, persona.id) == seeded[name], name
    seeded_orders = sum(len(snapshot["orders"]) for snapshot in seeded.values())
    assert report.orders_reset == seeded_orders


async def test_restore_deletes_persona_conversations_and_their_children(
    db_session: AsyncSession,
    personas: dict[str, Persona],
    now: datetime,
    make_activity: ActivityFn,
    existing: ExistingFn,
) -> None:
    alex_rows = await make_activity(personas["alex"], now - timedelta(hours=2))
    bia_rows = await make_activity(personas["bia"], now - timedelta(minutes=5))
    extra = Conversation(customer_id=personas["alex"].id)
    db_session.add(extra)
    await db_session.flush()

    report = await restore_demo(db_session, now)
    await db_session.flush()
    db_session.expire_all()

    assert report.conversations_deleted == 3
    for persona in personas.values():
        assert await _count_conversations(db_session, persona.id) == 0
    for rows in (alex_rows, bia_rows):
        left = await existing(rows)
        assert left.conversations == []
        assert left.messages == []
        assert left.feedback == []
        assert left.confirmations == []


async def test_restore_keeps_audit_emails_tickets_and_llm_calls(
    db_session: AsyncSession,
    personas: dict[str, Persona],
    now: datetime,
    make_activity: ActivityFn,
    existing: ExistingFn,
) -> None:
    rows = await make_activity(personas["alex"], now - timedelta(hours=1))

    await restore_demo(db_session, now)
    await db_session.flush()
    db_session.expire_all()

    left = await existing(rows)
    assert left.audit_rows == rows.audit_rows
    assert left.emails == rows.emails
    assert left.tickets == rows.tickets
    assert left.llm_calls == rows.llm_calls
    conversation_ids = (
        await db_session.scalars(select(Ticket.conversation_id).where(Ticket.id.in_(rows.tickets)))
    ).all()
    assert conversation_ids == [None], "the ticket stays, detached from the deleted conversation"


async def test_restore_never_touches_other_customers(
    db_session: AsyncSession,
    personas: dict[str, Persona],
    outsider: Persona,
    now: datetime,
    make_activity: ActivityFn,
    existing: ExistingFn,
) -> None:
    rows = await make_activity(outsider, now - timedelta(hours=1))
    commerce = await _commerce(db_session, outsider.id)

    await restore_demo(db_session, now)
    await db_session.flush()
    db_session.expire_all()

    assert await existing(rows) == rows
    assert await _commerce(db_session, outsider.id) == commerce
    assert await db_session.get(Customer, outsider.id) is not None
    assert await _count_conversations(db_session, outsider.id) == 1


async def test_restore_keeps_the_personas(
    db_session: AsyncSession, personas: dict[str, Persona], now: datetime
) -> None:
    await restore_demo(db_session, now)
    await db_session.flush()
    db_session.expire_all()

    for persona in personas.values():
        customer = await db_session.get(Customer, persona.id)
        assert customer is not None
        assert customer.username == persona.username
