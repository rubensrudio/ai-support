import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.models import Customer
from api.db.engine import get_sessionmaker
from api.tools.models import AuditLog, Order, OrderStatus
from api.tools.orders import (
    CancelOrderArgs,
    GetOrderArgs,
    ListOrdersArgs,
    cancel_order,
    get_order,
    list_orders,
    order_content,
    precheck_cancel_order,
)
from api.tools.registry import ToolContext, ToolErrorCode

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


def _new_customer(prefix: str) -> Customer:
    name = f"{prefix}-{uuid.uuid4().hex[:8]}"
    return Customer(
        id=uuid.uuid4(),
        username=name,
        display_name=prefix.title(),
        email=f"{name}@example.test",
        password_hash="x",
        language="en",
    )


async def _customer(session: AsyncSession, prefix: str) -> Customer:
    customer = _new_customer(prefix)
    session.add(customer)
    await session.flush()
    return customer


async def _order(
    session: AsyncSession, customer: Customer, status: OrderStatus, **overrides: object
) -> Order:
    values: dict[str, object] = {
        "customer_id": customer.id,
        "status": status,
        "total_cents": 4990,
        "currency": "USD",
        "items": [{"sku": "box-coffee", "name": "Coffee box", "qty": 1}],
    }
    values.update(overrides)
    order = Order(**values)
    session.add(order)
    await session.flush()
    return order


def _ctx(session: AsyncSession, customer: Customer, trace_id: str = "trace-orders") -> ToolContext:
    return ToolContext(
        customer_id=customer.id,
        customer_email=customer.email,
        trace_id=trace_id,
        session=session,
        language="en",
        now=NOW,
    )


async def _audit_count(session: AsyncSession, customer: Customer) -> int:
    statement = (
        select(func.count()).select_from(AuditLog).where(AuditLog.customer_id == customer.id)
    )
    return int((await session.execute(statement)).scalar_one())


async def _missing_order_id(session: AsyncSession) -> int:
    highest = (await session.execute(select(func.max(Order.id)))).scalar_one()
    return int(highest or 0) + 1000


async def test_get_order_returns_the_customer_order(db_session: AsyncSession) -> None:
    alex = await _customer(db_session, "alex")
    shipped_at = NOW - timedelta(days=1)
    order = await _order(db_session, alex, OrderStatus.SHIPPED, shipped_at=shipped_at)

    result = await get_order(GetOrderArgs(order_id=order.id), _ctx(db_session, alex))

    assert result.ok is True
    assert result.error_code is None
    assert result.content == {
        "order_id": order.id,
        "status": "shipped",
        "amount_cents": 4990,
        "currency": "USD",
        "items": [{"sku": "box-coffee", "name": "Coffee box", "qty": 1}],
        "created_at": order.created_at.isoformat(),
        "shipped_at": shipped_at.isoformat(),
        "delivered_at": None,
        "cancelled_at": None,
    }


async def test_another_customer_order_looks_exactly_like_a_missing_one(
    db_session: AsyncSession,
) -> None:
    alex = await _customer(db_session, "alex")
    bia = await _customer(db_session, "bia")
    bia_order = await _order(db_session, bia, OrderStatus.PAID)
    missing_id = await _missing_order_id(db_session)
    ctx = _ctx(db_session, alex)

    foreign_get = await get_order(GetOrderArgs(order_id=bia_order.id), ctx)
    missing_get = await get_order(GetOrderArgs(order_id=missing_id), ctx)
    foreign_cancel = await cancel_order(CancelOrderArgs(order_id=bia_order.id), ctx)
    missing_cancel = await cancel_order(CancelOrderArgs(order_id=missing_id), ctx)

    for result, order_id in [
        (foreign_get, bia_order.id),
        (missing_get, missing_id),
        (foreign_cancel, bia_order.id),
        (missing_cancel, missing_id),
    ]:
        assert result.ok is False
        assert result.error_code == ToolErrorCode.ORDER_NOT_FOUND
        assert result.content == {"order_id": order_id}
    listed = await list_orders(ListOrdersArgs(), ctx)
    assert bia_order.id not in [item["order_id"] for item in listed.content["orders"]]

    await db_session.refresh(bia_order)
    assert bia_order.status == OrderStatus.PAID
    assert bia_order.cancelled_at is None
    assert await _audit_count(db_session, alex) == 0
    assert await _audit_count(db_session, bia) == 0


@pytest.mark.parametrize("status", [OrderStatus.PENDING, OrderStatus.PAID])
async def test_cancel_order_cancels_pending_or_paid_and_audits(
    db_session: AsyncSession, status: OrderStatus
) -> None:
    alex = await _customer(db_session, "alex")
    order = await _order(db_session, alex, status)

    result = await cancel_order(
        CancelOrderArgs(order_id=order.id), _ctx(db_session, alex, "trace-cancel")
    )

    assert result.ok is True
    assert result.content["order_id"] == order.id
    assert result.content["status"] == "cancelled"
    assert result.content["cancelled_at"] == NOW.isoformat()
    assert result.content["amount_cents"] == 4990
    assert result.content["currency"] == "USD"
    await db_session.refresh(order)
    assert order.status == OrderStatus.CANCELLED
    assert order.cancelled_at == NOW
    audits = list(
        (
            await db_session.execute(select(AuditLog).where(AuditLog.customer_id == alex.id))
        ).scalars()
    )
    assert len(audits) == 1
    assert audits[0].tool == "cancel_order"
    assert audits[0].outcome == "success"
    assert audits[0].trace_id == "trace-cancel"
    assert audits[0].arguments == {"order_id": order.id}
    assert audits[0].result == result.content


@pytest.mark.parametrize(
    ("status", "extra"),
    [
        (OrderStatus.SHIPPED, {"shipped_at": NOW - timedelta(days=1)}),
        (
            OrderStatus.DELIVERED,
            {"shipped_at": NOW - timedelta(days=3), "delivered_at": NOW - timedelta(days=1)},
        ),
        (OrderStatus.CANCELLED, {"cancelled_at": NOW - timedelta(days=2)}),
    ],
)
async def test_cancel_order_refuses_orders_past_payment_without_changing_them(
    db_session: AsyncSession, status: OrderStatus, extra: dict[str, object]
) -> None:
    alex = await _customer(db_session, "alex")
    order = await _order(db_session, alex, status, **extra)
    cancelled_before = order.cancelled_at

    result = await cancel_order(CancelOrderArgs(order_id=order.id), _ctx(db_session, alex))

    assert result.ok is False
    assert result.error_code == ToolErrorCode.ORDER_NOT_CANCELLABLE
    assert result.content == {"order_id": order.id, "status": status.value}
    await db_session.refresh(order)
    assert order.status == status
    assert order.cancelled_at == cancelled_before
    assert await _audit_count(db_session, alex) == 0


async def test_list_orders_returns_newest_first_without_writing(
    db_session: AsyncSession,
) -> None:
    alex = await _customer(db_session, "alex")
    bia = await _customer(db_session, "bia")
    oldest = await _order(db_session, alex, OrderStatus.DELIVERED, created_at=NOW - timedelta(3))
    newest = await _order(db_session, alex, OrderStatus.PENDING, created_at=NOW - timedelta(1))
    middle = await _order(db_session, alex, OrderStatus.PAID, created_at=NOW - timedelta(2))
    await _order(db_session, bia, OrderStatus.PAID, created_at=NOW)
    statuses_before = [
        tuple(row)
        for row in await db_session.execute(select(Order.id, Order.status).order_by(Order.id))
    ]

    result = await list_orders(ListOrdersArgs(), _ctx(db_session, alex))
    limited = await list_orders(ListOrdersArgs(limit=2), _ctx(db_session, alex))

    assert result.ok is True
    assert [item["order_id"] for item in result.content["orders"]] == [
        newest.id,
        middle.id,
        oldest.id,
    ]
    assert result.content["orders"][0]["amount_cents"] == 4990
    assert result.content["orders"][0]["currency"] == "USD"
    assert [item["order_id"] for item in limited.content["orders"]] == [newest.id, middle.id]
    statuses_after = [
        tuple(row)
        for row in await db_session.execute(select(Order.id, Order.status).order_by(Order.id))
    ]
    assert statuses_after == statuses_before
    assert await _audit_count(db_session, alex) == 0
    assert not db_session.new and not db_session.dirty and not db_session.deleted


async def test_list_orders_of_a_customer_without_orders_is_empty(
    db_session: AsyncSession,
) -> None:
    alex = await _customer(db_session, "alex")

    result = await list_orders(ListOrdersArgs(), _ctx(db_session, alex))

    assert result.ok is True
    assert result.content == {"orders": []}


@pytest.fixture
async def committed_paid_order(
    migrated_database: str,
) -> AsyncIterator[tuple[Customer, int]]:
    """A paid order committed for real, so separate sessions can race on it."""
    factory = get_sessionmaker()
    customer = _new_customer("race")
    async with factory() as session:
        session.add(customer)
        await session.flush()
        order = await _order(session, customer, OrderStatus.PAID)
        order_id = order.id
        await session.commit()
    try:
        yield customer, order_id
    finally:
        async with factory() as session:
            await session.execute(delete(Customer).where(Customer.id == customer.id))
            await session.commit()


async def test_concurrent_cancellations_have_exactly_one_effect(
    committed_paid_order: tuple[Customer, int],
) -> None:
    customer, order_id = committed_paid_order
    factory = get_sessionmaker()

    async def attempt(trace_id: str) -> tuple[bool, str | None]:
        async with factory() as session:
            result = await cancel_order(
                CancelOrderArgs(order_id=order_id), _ctx(session, customer, trace_id)
            )
            await session.commit()
            return result.ok, result.error_code

    outcomes = await asyncio.gather(attempt("trace-race-1"), attempt("trace-race-2"))

    assert sorted(outcomes, key=lambda item: item[0]) == [
        (False, ToolErrorCode.ORDER_NOT_CANCELLABLE),
        (True, None),
    ]
    async with factory() as session:
        order = (await session.execute(select(Order).where(Order.id == order_id))).scalar_one()
        assert order.status == OrderStatus.CANCELLED
        audits = list(
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.customer_id == customer.id, AuditLog.tool == "cancel_order"
                    )
                )
            ).scalars()
        )
    assert [audit.outcome for audit in audits] == ["success"]


@pytest.mark.parametrize("status", [OrderStatus.PENDING, OrderStatus.PAID])
async def test_precheck_cancel_order_accepts_a_cancellable_order_without_writing(
    db_session: AsyncSession, status: OrderStatus
) -> None:
    alex = await _customer(db_session, "alex")
    order = await _order(db_session, alex, status)

    result = await precheck_cancel_order(CancelOrderArgs(order_id=order.id), _ctx(db_session, alex))

    assert result.ok is True
    assert result.error_code is None
    assert result.content == order_content(order)
    assert not db_session.new and not db_session.dirty and not db_session.deleted
    await db_session.refresh(order)
    assert order.status == status
    assert order.cancelled_at is None
    assert await _audit_count(db_session, alex) == 0


@pytest.mark.parametrize(
    ("status", "extra"),
    [
        (OrderStatus.SHIPPED, {"shipped_at": NOW - timedelta(days=1)}),
        (
            OrderStatus.DELIVERED,
            {"shipped_at": NOW - timedelta(days=3), "delivered_at": NOW - timedelta(days=1)},
        ),
        (OrderStatus.CANCELLED, {"cancelled_at": NOW - timedelta(days=2)}),
    ],
)
async def test_precheck_cancel_order_refuses_like_cancel_order(
    db_session: AsyncSession, status: OrderStatus, extra: dict[str, object]
) -> None:
    alex = await _customer(db_session, "alex")
    order = await _order(db_session, alex, status, **extra)
    ctx = _ctx(db_session, alex)

    prechecked = await precheck_cancel_order(CancelOrderArgs(order_id=order.id), ctx)
    cancelled = await cancel_order(CancelOrderArgs(order_id=order.id), ctx)

    assert prechecked.ok is False
    assert prechecked.error_code == ToolErrorCode.ORDER_NOT_CANCELLABLE
    assert prechecked.content == {"order_id": order.id, "status": status.value}
    assert prechecked == cancelled
    await db_session.refresh(order)
    assert order.status == status
    assert await _audit_count(db_session, alex) == 0


async def test_precheck_cancel_order_treats_another_customer_order_as_missing(
    db_session: AsyncSession,
) -> None:
    alex = await _customer(db_session, "alex")
    bia = await _customer(db_session, "bia")
    bia_order = await _order(db_session, bia, OrderStatus.PAID)
    missing_id = await _missing_order_id(db_session)
    ctx = _ctx(db_session, alex)

    foreign = await precheck_cancel_order(CancelOrderArgs(order_id=bia_order.id), ctx)
    missing = await precheck_cancel_order(CancelOrderArgs(order_id=missing_id), ctx)

    assert foreign.ok is False and missing.ok is False
    assert foreign.error_code == missing.error_code == ToolErrorCode.ORDER_NOT_FOUND
    assert foreign.content == {"order_id": bia_order.id}
    assert missing.content == {"order_id": missing_id}
    await db_session.refresh(bia_order)
    assert bia_order.status == OrderStatus.PAID
    assert await _audit_count(db_session, alex) == 0
    assert await _audit_count(db_session, bia) == 0
