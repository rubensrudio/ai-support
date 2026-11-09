"""Commerce seed per persona (TASK-066, TOOL-01, DEMO-06, TOOL-19, TOOL-98)."""

import os
import subprocess
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.models import Customer
from api.chat.models import Conversation
from api.db.engine import session_scope
from api.seed.commerce import CommerceSeedResult, seed_commerce
from api.seed.personas import PERSONAS, seed_personas
from api.tools.models import AuditLog, Order, OrderStatus, Refund, Subscription

pytestmark = pytest.mark.integration

ROOT_DIR = Path(__file__).resolve().parents[4]
PASSWORD = "persona-password-for-tests"
NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


async def _count(session: AsyncSession, model: type[object]) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def _orders(session: AsyncSession, customer_id: object) -> list[Order]:
    statement = select(Order).where(Order.customer_id == customer_id).order_by(Order.id)
    return list((await session.scalars(statement)).all())


async def test_each_persona_gets_one_subscription_and_orders_in_every_status(
    db_session: AsyncSession,
) -> None:
    ids = await seed_personas(db_session, PASSWORD)

    result = await seed_commerce(db_session, NOW)

    assert isinstance(result, CommerceSeedResult)
    assert result.customers == len(PERSONAS)
    assert result.subscriptions == len(PERSONAS)
    assert result.orders == await _count(db_session, Order)
    assert result.skipped == ()
    for customer_id in ids:
        subs = (
            await db_session.scalars(
                select(Subscription).where(Subscription.customer_id == customer_id)
            )
        ).all()
        assert len(subs) == 1
        assert subs[0].currency == "USD"
        assert subs[0].price_cents > 0
        assert subs[0].renews_at is not None and subs[0].renews_at > NOW

        orders = await _orders(db_session, customer_id)
        assert {order.status for order in orders} == {status.value for status in OrderStatus}
        for order in orders:
            assert order.currency == "USD"
            assert order.total_cents > 0
            assert order.items, "every order lists its items"
            assert order.created_at <= NOW
            assert all({"name", "quantity"} <= set(item) for item in order.items)


async def test_delivered_orders_cover_3_exactly_7_and_10_days_ago(
    db_session: AsyncSession,
) -> None:
    ids = await seed_personas(db_session, PASSWORD)

    await seed_commerce(db_session, NOW)

    for customer_id in ids:
        orders = await _orders(db_session, customer_id)
        delivered = [o for o in orders if o.status == OrderStatus.DELIVERED]
        ages = sorted(NOW - o.delivered_at for o in delivered if o.delivered_at is not None)
        assert ages == [timedelta(days=3), timedelta(days=7), timedelta(days=10)]
        for order in delivered:
            assert order.shipped_at is not None and order.shipped_at < order.delivered_at  # type: ignore[operator]

        by_status = {o.status: o for o in orders if o.status != OrderStatus.DELIVERED}
        cancelled = by_status[OrderStatus.CANCELLED]
        assert cancelled.cancelled_at is not None and cancelled.shipped_at is None
        shipped = by_status[OrderStatus.SHIPPED]
        assert shipped.shipped_at is not None and shipped.delivered_at is None
        for status in (OrderStatus.PENDING, OrderStatus.PAID):
            assert by_status[status].shipped_at is None
            assert by_status[status].cancelled_at is None


async def test_seed_twice_gives_the_same_counts_and_resets_refunds(
    db_session: AsyncSession,
) -> None:
    ids = await seed_personas(db_session, PASSWORD)
    first = await seed_commerce(db_session, NOW)
    order = (await _orders(db_session, ids[0]))[0]
    db_session.add(
        Refund(
            order_id=order.id,
            customer_id=ids[0],
            amount_cents=order.total_cents,
            trace_id="t",
        )
    )
    sub = await db_session.scalar(select(Subscription).where(Subscription.customer_id == ids[0]))
    assert sub is not None
    sub.status = "cancelled"
    await db_session.flush()
    counts_before = (await _count(db_session, Order), await _count(db_session, Subscription))

    second = await seed_commerce(db_session, NOW)

    assert second == first
    assert (await _count(db_session, Order), await _count(db_session, Subscription)) == (
        counts_before
    )
    assert await _count(db_session, Refund) == 0
    restored = await db_session.scalar(
        select(Subscription.status).where(Subscription.customer_id == ids[0])
    )
    assert restored == "active"


async def test_seed_preserves_conversations_audit_and_other_customers(
    db_session: AsyncSession,
) -> None:
    ids = await seed_personas(db_session, PASSWORD)
    other = Customer(
        username="someone-else",
        display_name="Other",
        email="other@example.com",
        password_hash="x",
        language="en",
    )
    db_session.add(other)
    await db_session.flush()
    db_session.add(Conversation(customer_id=ids[0]))
    db_session.add(
        Order(customer_id=other.id, status="paid", total_cents=1990, currency="USD", items=[])
    )
    db_session.add(
        AuditLog(
            customer_id=ids[0],
            tool="send_email",
            arguments={},
            result={},
            outcome="success",
            trace_id="keep",
        )
    )
    await db_session.flush()

    await seed_commerce(db_session, NOW)
    await seed_commerce(db_session, NOW)

    assert await _count(db_session, Conversation) == 1
    assert await _count(db_session, AuditLog) == 1
    assert len(await _orders(db_session, other.id)) == 1


async def test_missing_persona_is_skipped_and_reported(db_session: AsyncSession) -> None:
    await seed_personas(db_session, PASSWORD)
    await db_session.execute(delete(Customer).where(Customer.username == PERSONAS[1].username))
    await db_session.flush()

    result = await seed_commerce(db_session, NOW)

    assert result.customers == 1
    assert result.subscriptions == 1
    assert result.skipped == (PERSONAS[1].username,)


@pytest.fixture
async def committed_personas(migrated_database: str) -> AsyncIterator[str]:
    """For CLI tests that commit to the test database: remove the personas afterwards."""
    yield migrated_database
    usernames = [p.username for p in PERSONAS]
    async with session_scope() as session:
        await session.execute(delete(Customer).where(Customer.username.in_(usernames)))


def _run_cli(database_url: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "ATLAS_DATABASE_URL": database_url,
        "ATLAS_PERSONA_PASSWORD": PASSWORD,
        "ATLAS_LLM_PROVIDER": "fake",
    }
    return subprocess.run(  # noqa: S603 - fixed interpreter and module, test-only arguments
        [sys.executable, "-m", "api.seed", *args],
        cwd=ROOT_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


async def test_cli_commerce_flag_seeds_orders_and_prints_a_summary(
    committed_personas: str,
) -> None:
    result = _run_cli(committed_personas, "--commerce")

    assert result.returncode == 0, result.stderr
    assert "not available" not in result.stdout
    assert "Commerce seed:" in result.stdout
    async with session_scope() as session:
        assert await _count(session, Subscription) == len(PERSONAS)
        orders = await _count(session, Order)
    assert orders > 0
    assert f"{orders} orders" in result.stdout

    again = _run_cli(committed_personas, "--commerce")
    assert again.returncode == 0, again.stderr
    async with session_scope() as session:
        assert await _count(session, Order) == orders
        assert await _count(session, Subscription) == len(PERSONAS)


async def test_cli_without_commerce_flag_does_not_seed_orders(committed_personas: str) -> None:
    result = _run_cli(committed_personas)

    assert result.returncode == 0, result.stderr
    assert "Commerce seed" not in result.stdout
    async with session_scope() as session:
        assert await _count(session, Order) == 0
