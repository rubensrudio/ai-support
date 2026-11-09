import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.models import Customer
from api.db.engine import get_sessionmaker
from api.tools import refunds as refunds_module
from api.tools.models import AuditLog, Order, OrderStatus, Refund
from api.tools.refunds import (
    CheckRefundArgs,
    IssueRefundArgs,
    check_refund_policy,
    issue_refund,
    precheck_issue_refund,
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
        "total_cents": 7450,
        "currency": "USD",
        "items": [{"sku": "kettle", "qty": 1}],
    }
    values.update(overrides)
    order = Order(**values)
    session.add(order)
    await session.flush()
    return order


async def _delivered(session: AsyncSession, customer: Customer, days_ago: float = 2) -> Order:
    return await _order(
        session,
        customer,
        OrderStatus.DELIVERED,
        shipped_at=NOW - timedelta(days=days_ago + 2),
        delivered_at=NOW - timedelta(days=days_ago),
    )


def _ctx(session: AsyncSession, customer: Customer, trace_id: str = "trace-refund") -> ToolContext:
    return ToolContext(
        customer_id=customer.id,
        customer_email=customer.email,
        trace_id=trace_id,
        session=session,
        language="en",
        now=NOW,
    )


async def _refund_count(session: AsyncSession, order_id: int) -> int:
    statement = select(func.count()).select_from(Refund).where(Refund.order_id == order_id)
    return int((await session.execute(statement)).scalar_one())


async def _audits(session: AsyncSession, customer: Customer) -> list[AuditLog]:
    statement = select(AuditLog).where(AuditLog.customer_id == customer.id)
    return list((await session.execute(statement)).scalars())


async def test_check_refund_policy_reports_eligibility_without_writing(
    db_session: AsyncSession,
) -> None:
    alex = await _customer(db_session, "alex")
    eligible = await _delivered(db_session, alex, days_ago=2)
    late = await _delivered(db_session, alex, days_ago=10)
    cancelled = await _order(db_session, alex, OrderStatus.CANCELLED, cancelled_at=NOW)
    ctx = _ctx(db_session, alex)

    ok = await check_refund_policy(CheckRefundArgs(order_id=eligible.id), ctx)
    too_late = await check_refund_policy(CheckRefundArgs(order_id=late.id), ctx)
    before_shipment = await check_refund_policy(CheckRefundArgs(order_id=cancelled.id), ctx)

    assert ok.ok is True
    assert ok.content == {
        "order_id": eligible.id,
        "eligible": True,
        "reason_code": "delivered_within_7_days",
        "amount_cents": 7450,
        "currency": "USD",
    }
    assert too_late.ok is True
    assert too_late.content["eligible"] is False
    assert too_late.content["reason_code"] == "delivered_over_7_days"
    assert before_shipment.content["eligible"] is True
    assert before_shipment.content["reason_code"] == "cancelled_before_shipment"
    assert await _audits(db_session, alex) == []
    assert await _refund_count(db_session, eligible.id) == 0


async def test_check_refund_policy_reports_an_existing_refund(db_session: AsyncSession) -> None:
    alex = await _customer(db_session, "alex")
    order = await _delivered(db_session, alex)
    db_session.add(
        Refund(
            order_id=order.id,
            customer_id=alex.id,
            amount_cents=order.total_cents,
            currency="USD",
            trace_id="earlier",
        )
    )
    await db_session.flush()

    result = await check_refund_policy(CheckRefundArgs(order_id=order.id), _ctx(db_session, alex))

    assert result.ok is True
    assert result.content["eligible"] is False
    assert result.content["reason_code"] == "already_refunded"


async def test_issue_refund_inserts_the_full_amount_and_audits_with_trace(
    db_session: AsyncSession,
) -> None:
    alex = await _customer(db_session, "alex")
    order = await _delivered(db_session, alex)

    result = await issue_refund(
        IssueRefundArgs(order_id=order.id), _ctx(db_session, alex, "trace-issue")
    )

    assert result.ok is True
    assert result.content["order_id"] == order.id
    assert result.content["amount_cents"] == 7450
    assert result.content["currency"] == "USD"
    refund = (
        await db_session.execute(select(Refund).where(Refund.order_id == order.id))
    ).scalar_one()
    assert refund.customer_id == alex.id
    assert refund.amount_cents == order.total_cents
    assert refund.currency == "USD"
    assert refund.trace_id == "trace-issue"
    assert result.content["refund_id"] == refund.id
    audits = await _audits(db_session, alex)
    assert len(audits) == 1
    assert audits[0].tool == "issue_refund"
    assert audits[0].outcome == "success"
    assert audits[0].trace_id == "trace-issue"
    assert audits[0].arguments == {"order_id": order.id}
    assert audits[0].result == result.content


@pytest.mark.parametrize(
    ("status", "extra", "reason"),
    [
        (OrderStatus.PAID, {}, "not_delivered_yet"),
        (OrderStatus.SHIPPED, {"shipped_at": NOW - timedelta(days=1)}, "not_delivered_yet"),
        (
            OrderStatus.DELIVERED,
            {"shipped_at": NOW - timedelta(days=12), "delivered_at": NOW - timedelta(days=10)},
            "delivered_over_7_days",
        ),
    ],
)
async def test_issue_refund_of_an_ineligible_order_is_refused_without_a_row(
    db_session: AsyncSession, status: OrderStatus, extra: dict[str, object], reason: str
) -> None:
    alex = await _customer(db_session, "alex")
    order = await _order(db_session, alex, status, **extra)

    result = await issue_refund(IssueRefundArgs(order_id=order.id), _ctx(db_session, alex))

    assert result.ok is False
    assert result.error_code == ToolErrorCode.REFUND_NOT_ELIGIBLE
    assert result.content["order_id"] == order.id
    assert result.content["reason_code"] == reason
    assert await _refund_count(db_session, order.id) == 0
    assert await _audits(db_session, alex) == []


async def test_issue_refund_twice_returns_already_refunded(db_session: AsyncSession) -> None:
    alex = await _customer(db_session, "alex")
    order = await _delivered(db_session, alex)
    ctx = _ctx(db_session, alex)

    first = await issue_refund(IssueRefundArgs(order_id=order.id), ctx)
    second = await issue_refund(IssueRefundArgs(order_id=order.id), ctx)

    assert first.ok is True
    assert second.ok is False
    assert second.error_code == ToolErrorCode.ALREADY_REFUNDED
    assert second.content == {"order_id": order.id}
    assert await _refund_count(db_session, order.id) == 1
    assert len(await _audits(db_session, alex)) == 1


async def test_unique_violation_is_reported_as_already_refunded(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the existence check is bypassed, UNIQUE(order_id) still holds and the caller's
    transaction stays usable (the insert runs in a savepoint)."""
    alex = await _customer(db_session, "alex")
    order = await _delivered(db_session, alex)
    ctx = _ctx(db_session, alex)
    first = await issue_refund(IssueRefundArgs(order_id=order.id), ctx)

    async def never_refunded(_ctx: ToolContext, _order_id: int) -> bool:
        return False

    monkeypatch.setattr(refunds_module, "_has_refund", never_refunded)
    second = await issue_refund(IssueRefundArgs(order_id=order.id), ctx)

    assert first.ok is True
    assert second.ok is False
    assert second.error_code == ToolErrorCode.ALREADY_REFUNDED
    assert second.content == {"order_id": order.id}
    assert await _refund_count(db_session, order.id) == 1
    assert len(await _audits(db_session, alex)) == 1


async def test_refund_tools_treat_another_customer_order_as_missing(
    db_session: AsyncSession,
) -> None:
    alex = await _customer(db_session, "alex")
    bia = await _customer(db_session, "bia")
    bia_order = await _delivered(db_session, bia)
    highest = (await db_session.execute(select(func.max(Order.id)))).scalar_one()
    missing_id = int(highest or 0) + 1000
    ctx = _ctx(db_session, alex)

    for order_id in (bia_order.id, missing_id):
        checked = await check_refund_policy(CheckRefundArgs(order_id=order_id), ctx)
        issued = await issue_refund(IssueRefundArgs(order_id=order_id), ctx)
        for result in (checked, issued):
            assert result.ok is False
            assert result.error_code == ToolErrorCode.ORDER_NOT_FOUND
            assert result.content == {"order_id": order_id}

    assert await _refund_count(db_session, bia_order.id) == 0
    assert await _audits(db_session, alex) == []
    assert await _audits(db_session, bia) == []


@pytest.fixture
async def committed_delivered_order(
    migrated_database: str,
) -> AsyncIterator[tuple[Customer, int]]:
    """A delivered order committed for real, so separate sessions can race on it."""
    factory = get_sessionmaker()
    customer = _new_customer("race")
    async with factory() as session:
        session.add(customer)
        await session.flush()
        order = await _delivered(session, customer)
        order_id = order.id
        await session.commit()
    try:
        yield customer, order_id
    finally:
        async with factory() as session:
            await session.execute(delete(Customer).where(Customer.id == customer.id))
            await session.commit()


async def test_concurrent_refunds_create_exactly_one_refund(
    committed_delivered_order: tuple[Customer, int],
) -> None:
    customer, order_id = committed_delivered_order
    factory = get_sessionmaker()

    async def attempt(trace_id: str) -> tuple[bool, str | None]:
        async with factory() as session:
            result = await issue_refund(
                IssueRefundArgs(order_id=order_id), _ctx(session, customer, trace_id)
            )
            await session.commit()
            return result.ok, result.error_code

    outcomes = await asyncio.gather(attempt("trace-race-1"), attempt("trace-race-2"))

    assert sorted(outcomes, key=lambda item: item[0]) == [
        (False, ToolErrorCode.ALREADY_REFUNDED),
        (True, None),
    ]
    async with factory() as session:
        assert await _refund_count(session, order_id) == 1
        audits = await _audits(session, customer)
    assert [(audit.tool, audit.outcome) for audit in audits] == [("issue_refund", "success")]


async def test_precheck_issue_refund_accepts_an_eligible_order_without_writing(
    db_session: AsyncSession,
) -> None:
    alex = await _customer(db_session, "alex")
    order = await _delivered(db_session, alex)

    result = await precheck_issue_refund(IssueRefundArgs(order_id=order.id), _ctx(db_session, alex))

    assert result.ok is True
    assert result.error_code is None
    assert result.content == {
        "order_id": order.id,
        "eligible": True,
        "reason_code": "delivered_within_7_days",
        "amount_cents": 7450,
        "currency": "USD",
    }
    assert not db_session.new and not db_session.dirty and not db_session.deleted
    assert await _refund_count(db_session, order.id) == 0
    assert await _audits(db_session, alex) == []


@pytest.mark.parametrize(
    ("status", "extra", "reason"),
    [
        (OrderStatus.PAID, {}, "not_delivered_yet"),
        (OrderStatus.SHIPPED, {"shipped_at": NOW - timedelta(days=1)}, "not_delivered_yet"),
        (
            OrderStatus.DELIVERED,
            {"shipped_at": NOW - timedelta(days=12), "delivered_at": NOW - timedelta(days=10)},
            "delivered_over_7_days",
        ),
    ],
)
async def test_precheck_issue_refund_refuses_an_ineligible_order_like_issue_refund(
    db_session: AsyncSession, status: OrderStatus, extra: dict[str, object], reason: str
) -> None:
    alex = await _customer(db_session, "alex")
    order = await _order(db_session, alex, status, **extra)
    ctx = _ctx(db_session, alex)

    prechecked = await precheck_issue_refund(IssueRefundArgs(order_id=order.id), ctx)
    issued = await issue_refund(IssueRefundArgs(order_id=order.id), ctx)

    assert prechecked.ok is False
    assert prechecked.error_code == ToolErrorCode.REFUND_NOT_ELIGIBLE
    assert prechecked.content == {
        "order_id": order.id,
        "eligible": False,
        "reason_code": reason,
        "amount_cents": 7450,
        "currency": "USD",
    }
    assert prechecked == issued
    assert await _refund_count(db_session, order.id) == 0
    assert await _audits(db_session, alex) == []


async def test_precheck_issue_refund_reports_an_existing_refund(db_session: AsyncSession) -> None:
    alex = await _customer(db_session, "alex")
    order = await _delivered(db_session, alex)
    ctx = _ctx(db_session, alex)
    await issue_refund(IssueRefundArgs(order_id=order.id), ctx)

    result = await precheck_issue_refund(IssueRefundArgs(order_id=order.id), ctx)

    assert result.ok is False
    assert result.error_code == ToolErrorCode.ALREADY_REFUNDED
    assert result.content == {"order_id": order.id}
    assert await _refund_count(db_session, order.id) == 1
    assert len(await _audits(db_session, alex)) == 1


async def test_precheck_issue_refund_treats_another_customer_order_as_missing(
    db_session: AsyncSession,
) -> None:
    alex = await _customer(db_session, "alex")
    bia = await _customer(db_session, "bia")
    bia_order = await _delivered(db_session, bia)
    highest = (await db_session.execute(select(func.max(Order.id)))).scalar_one()
    missing_id = int(highest or 0) + 1000
    ctx = _ctx(db_session, alex)

    for order_id in (bia_order.id, missing_id):
        result = await precheck_issue_refund(IssueRefundArgs(order_id=order_id), ctx)
        assert result.ok is False
        assert result.error_code == ToolErrorCode.ORDER_NOT_FOUND
        assert result.content == {"order_id": order_id}

    assert await _refund_count(db_session, bia_order.id) == 0
    assert await _audits(db_session, bia) == []


async def test_precheck_issue_refund_does_not_wait_for_a_row_lock(
    committed_delivered_order: tuple[Customer, int],
) -> None:
    """No `FOR UPDATE`: the precheck completes while another transaction holds the row lock."""
    customer, order_id = committed_delivered_order
    factory = get_sessionmaker()
    async with factory() as holder, factory() as reader:
        await holder.execute(select(Order.id).where(Order.id == order_id).with_for_update())

        result = await asyncio.wait_for(
            precheck_issue_refund(IssueRefundArgs(order_id=order_id), _ctx(reader, customer)),
            timeout=5,
        )

        assert result.ok is True
        assert not reader.new and not reader.dirty and not reader.deleted
        await holder.rollback()
        await reader.rollback()
