import uuid
from datetime import UTC, datetime

import pytest
from alembic.command import downgrade, upgrade
from alembic.config import Config
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.models import Customer
from api.tools.models import AuditLog, EmailOutbox, Order, OrderStatus, Refund, Subscription

pytestmark = pytest.mark.integration


async def _customer(session: AsyncSession) -> Customer:
    name = f"shopper-{uuid.uuid4().hex[:8]}"
    customer = Customer(
        id=uuid.uuid4(),
        username=name,
        display_name="Shopper",
        email=f"{name}@example.test",
        password_hash="x",
        language="en",
    )
    session.add(customer)
    await session.flush()
    return customer


def _order(customer: Customer, **overrides: object) -> Order:
    values: dict[str, object] = {
        "customer_id": customer.id,
        "status": OrderStatus.PAID,
        "total_cents": 4990,
        "currency": "USD",
        "items": [{"sku": "box-coffee", "qty": 1}],
    }
    values.update(overrides)
    return Order(**values)


async def test_all_commerce_tables_store_a_row(db_session: AsyncSession) -> None:
    customer = await _customer(db_session)
    order = _order(customer, delivered_at=datetime.now(UTC), status=OrderStatus.DELIVERED)
    db_session.add(order)
    await db_session.flush()
    db_session.add_all(
        [
            Subscription(
                customer_id=customer.id,
                plan_code="plus",
                status="active",
                price_cents=2990,
                currency="USD",
            ),
            Refund(
                order_id=order.id,
                customer_id=customer.id,
                amount_cents=order.total_cents,
                currency="USD",
                trace_id="t1",
            ),
            AuditLog(
                customer_id=customer.id,
                tool="issue_refund",
                arguments={"order_id": order.id},
                result={"ok": True},
                outcome="success",
                trace_id="t1",
            ),
            EmailOutbox(
                customer_id=customer.id,
                recipient=customer.email,
                subject="Hi",
                body="Body",
                trace_id="t1",
            ),
        ]
    )
    await db_session.flush()


async def test_second_refund_for_the_same_order_violates_uniqueness(
    db_session: AsyncSession,
) -> None:
    customer = await _customer(db_session)
    order = _order(customer)
    db_session.add(order)
    await db_session.flush()
    for _ in range(2):
        db_session.add(
            Refund(
                order_id=order.id,
                customer_id=customer.id,
                amount_cents=100,
                currency="USD",
                trace_id="t",
            )
        )
    with pytest.raises(IntegrityError, match="uq_refunds_order_id"):
        await db_session.flush()


async def test_currency_other_than_usd_is_rejected(db_session: AsyncSession) -> None:
    customer = await _customer(db_session)
    db_session.add(_order(customer, currency="BRL"))
    with pytest.raises(IntegrityError, match="ck_orders_currency"):
        await db_session.flush()


async def test_unknown_order_status_is_rejected(db_session: AsyncSession) -> None:
    customer = await _customer(db_session)
    db_session.add(_order(customer, status="lost"))
    with pytest.raises(IntegrityError, match="ck_orders_status"):
        await db_session.flush()


async def test_unknown_audit_outcome_is_rejected(db_session: AsyncSession) -> None:
    customer = await _customer(db_session)
    db_session.add(
        AuditLog(
            customer_id=customer.id,
            tool="x",
            arguments={},
            result={},
            outcome="maybe",
            trace_id="t",
        )
    )
    with pytest.raises(IntegrityError, match="ck_audit_log_outcome"):
        await db_session.flush()


def test_migration_0007_downgrade_and_upgrade(
    alembic_config: Config, migrated_database: str
) -> None:
    downgrade(alembic_config, "0006")
    upgrade(alembic_config, "head")
