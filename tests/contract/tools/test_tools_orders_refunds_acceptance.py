"""Order states, refund policy, email and concurrency (TOOL-06..09, TOOL-15..18, TOOL-91..93,
TOOL-98, TOOL-99, DA-21, section 7 of the spec)."""

import asyncio
import smtplib
from datetime import datetime, timedelta
from typing import Any, NoReturn

import httpx
import pytest
from conftest import CommittedCustomer, ContextFactory, OrderFactory, Persona
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tools_acceptance_helpers import (
    DAY,
    audit_count,
    build_order,
    new_trace,
    order_state,
    outbox_rows,
    refund_count,
)

from api.llm.client import ToolCall
from api.tools.models import AuditLog, OrderStatus, Refund
from api.tools.registry import ToolContext, ToolErrorCode, ToolRegistry, ToolResult

pytestmark = pytest.mark.integration


async def _call(
    registry: ToolRegistry, ctx: ToolContext, tool: str, **arguments: Any
) -> ToolResult:
    return await registry.execute(ToolCall(id="c1", name=tool, arguments=arguments), ctx)


# --- TOOL-17 / TOOL-91 / TOOL-99: only pending and paid are cancellable ---------------------


@pytest.mark.parametrize("status", [OrderStatus.PENDING, OrderStatus.PAID])
async def test_pending_and_paid_orders_are_cancelled_and_audited(
    status: OrderStatus,
    registry: ToolRegistry,
    ctx_for: ContextFactory,
    make_order: OrderFactory,
    personas: dict[str, Persona],
    db_session: AsyncSession,
) -> None:
    order = await make_order("alex", status)
    ctx = ctx_for("alex")

    result = await _call(registry, ctx, "cancel_order", order_id=order.id)

    assert result.ok is True
    new_status, cancelled_at = await order_state(db_session, order.id)
    assert new_status == OrderStatus.CANCELLED
    assert cancelled_at is not None
    rows = list(
        (
            await db_session.execute(
                select(AuditLog).where(
                    AuditLog.trace_id == ctx.trace_id, AuditLog.outcome == "success"
                )
            )
        ).scalars()
    )
    assert [(row.tool, row.customer_id) for row in rows] == [("cancel_order", personas["alex"].id)]
    assert rows[0].arguments.get("order_id") == order.id


@pytest.mark.parametrize(
    "status", [OrderStatus.SHIPPED, OrderStatus.DELIVERED, OrderStatus.CANCELLED]
)
async def test_non_cancellable_orders_return_an_error_and_keep_their_status(
    status: OrderStatus,
    registry: ToolRegistry,
    ctx_for: ContextFactory,
    make_order: OrderFactory,
    db_session: AsyncSession,
) -> None:
    order = await make_order("alex", status)
    before = await order_state(db_session, order.id)
    ctx = ctx_for("alex")

    result = await _call(registry, ctx, "cancel_order", order_id=order.id)

    assert result.ok is False
    assert result.error_code == ToolErrorCode.ORDER_NOT_CANCELLABLE
    assert await order_state(db_session, order.id) == before
    assert await audit_count(db_session, ctx.trace_id, "success") == 0


async def test_second_cancellation_of_the_same_order_is_an_error(
    registry: ToolRegistry,
    ctx_for: ContextFactory,
    make_order: OrderFactory,
    db_session: AsyncSession,
) -> None:
    order = await make_order("alex", OrderStatus.PAID)
    assert (await _call(registry, ctx_for("alex"), "cancel_order", order_id=order.id)).ok
    after_first = await order_state(db_session, order.id)
    ctx = ctx_for("alex")

    second = await _call(registry, ctx, "cancel_order", order_id=order.id)

    assert second.ok is False
    assert second.error_code == ToolErrorCode.ORDER_NOT_CANCELLABLE
    assert await order_state(db_session, order.id) == after_first
    assert await audit_count(db_session, ctx.trace_id, "success") == 0


async def test_get_order_has_no_side_effect(
    registry: ToolRegistry,
    ctx_for: ContextFactory,
    make_order: OrderFactory,
    db_session: AsyncSession,
) -> None:
    order = await make_order("alex", OrderStatus.PAID)
    before = await order_state(db_session, order.id)
    ctx = ctx_for("alex")

    result = await _call(registry, ctx, "get_order", order_id=order.id)

    assert result.ok is True
    assert await order_state(db_session, order.id) == before
    assert await audit_count(db_session, ctx.trace_id) == 0


# --- TOOL-15 / TOOL-16 / TOOL-98: refund policy enforced on the server ----------------------


@pytest.mark.parametrize(
    ("status", "delivered_ago"),
    [
        (OrderStatus.PENDING, None),
        (OrderStatus.PAID, None),
        (OrderStatus.SHIPPED, None),
        (OrderStatus.DELIVERED, 10 * DAY),
        (OrderStatus.DELIVERED, 7 * DAY + timedelta(seconds=1)),
    ],
    ids=["pending", "paid", "shipped", "delivered-10d", "delivered-7d+1s"],
)
async def test_ineligible_refund_never_creates_a_row_even_without_a_prior_check(
    status: OrderStatus,
    delivered_ago: timedelta | None,
    registry: ToolRegistry,
    ctx_for: ContextFactory,
    make_order: OrderFactory,
    db_session: AsyncSession,
) -> None:
    if delivered_ago is None:
        order = await make_order("alex", status)
    else:
        order = await make_order("alex", status, delivered_ago=delivered_ago)
    ctx = ctx_for("alex")

    result = await _call(registry, ctx, "issue_refund", order_id=order.id)

    assert result.ok is False
    assert result.error_code == ToolErrorCode.REFUND_NOT_ELIGIBLE
    assert await refund_count(db_session, order.id) == 0
    assert await audit_count(db_session, ctx.trace_id, "success") == 0


async def test_delivered_exactly_seven_days_ago_is_eligible_and_one_second_more_is_not(
    registry: ToolRegistry,
    ctx_for: ContextFactory,
    make_order: OrderFactory,
) -> None:
    edge = await make_order("alex", OrderStatus.DELIVERED, delivered_ago=7 * DAY)
    late = await make_order(
        "alex", OrderStatus.DELIVERED, delivered_ago=7 * DAY + timedelta(seconds=1)
    )

    edge_check = await _call(registry, ctx_for("alex"), "check_refund_policy", order_id=edge.id)
    late_check = await _call(registry, ctx_for("alex"), "check_refund_policy", order_id=late.id)

    assert edge_check.ok is True
    assert edge_check.content["eligible"] is True
    assert edge_check.content["reason_code"] == "delivered_within_7_days"
    assert edge_check.content["amount_cents"] == edge.total_cents
    assert edge_check.content["currency"] == "USD"
    assert late_check.ok is True
    assert late_check.content["eligible"] is False
    assert late_check.content["reason_code"] == "delivered_over_7_days"


async def test_eligible_refund_is_full_amount_in_usd_and_audited(
    registry: ToolRegistry,
    ctx_for: ContextFactory,
    make_order: OrderFactory,
    personas: dict[str, Persona],
    db_session: AsyncSession,
) -> None:
    order = await make_order(
        "alex", OrderStatus.DELIVERED, delivered_ago=7 * DAY, total_cents=6_180
    )
    ctx = ctx_for("alex")

    result = await _call(registry, ctx, "issue_refund", order_id=order.id)

    assert result.ok is True
    refunds = list(
        (await db_session.execute(select(Refund).where(Refund.order_id == order.id))).scalars()
    )
    assert [(r.amount_cents, r.currency, r.customer_id) for r in refunds] == [
        (6_180, "USD", personas["alex"].id)
    ]
    assert refunds[0].trace_id == ctx.trace_id
    success = list(
        (
            await db_session.execute(
                select(AuditLog.tool).where(
                    AuditLog.trace_id == ctx.trace_id, AuditLog.outcome == "success"
                )
            )
        ).scalars()
    )
    assert success == ["issue_refund"]


async def test_cancelled_before_shipment_is_refund_eligible(
    registry: ToolRegistry,
    ctx_for: ContextFactory,
    make_order: OrderFactory,
) -> None:
    order = await make_order("alex", OrderStatus.CANCELLED)

    check = await _call(registry, ctx_for("alex"), "check_refund_policy", order_id=order.id)

    assert check.ok is True
    assert check.content["eligible"] is True
    assert check.content["reason_code"] == "cancelled_before_shipment"


async def test_second_refund_of_the_same_order_is_an_error(
    registry: ToolRegistry,
    ctx_for: ContextFactory,
    make_order: OrderFactory,
    db_session: AsyncSession,
) -> None:
    order = await make_order("alex", OrderStatus.DELIVERED, delivered_ago=2 * DAY)
    assert (await _call(registry, ctx_for("alex"), "issue_refund", order_id=order.id)).ok
    ctx = ctx_for("alex")

    second = await _call(registry, ctx, "issue_refund", order_id=order.id)
    check = await _call(registry, ctx_for("alex"), "check_refund_policy", order_id=order.id)

    assert second.ok is False
    assert second.error_code == ToolErrorCode.ALREADY_REFUNDED
    assert await refund_count(db_session, order.id) == 1
    assert await audit_count(db_session, ctx.trace_id, "success") == 0
    assert check.content["eligible"] is False
    assert check.content["reason_code"] == "already_refunded"


# --- TOOL-92 / TOOL-93 / DA-21: single effect under real concurrency ------------------------

PARALLEL_CALLS = 4


async def _parallel(
    customer: CommittedCustomer, registry: ToolRegistry, tool: str, order_id: int, now: datetime
) -> list[ToolResult]:
    async def _one() -> ToolResult:
        async with customer.sessions() as session:
            ctx = ToolContext(
                customer_id=customer.id,
                customer_email=customer.email,
                trace_id=new_trace(),
                session=session,
                language="en",
                now=now,
            )
            result = await registry.execute(
                ToolCall(id="c1", name=tool, arguments={"order_id": order_id}), ctx
            )
            await session.commit()
            return result

    return list(await asyncio.gather(*(_one() for _ in range(PARALLEL_CALLS))))


async def _committed_order(
    customer: CommittedCustomer,
    status: OrderStatus,
    now: datetime,
    delivered_ago: timedelta = 3 * DAY,
) -> int:
    async with customer.sessions() as session:
        order = build_order(customer.id, status, now, delivered_ago=delivered_ago)
        session.add(order)
        await session.commit()
        return order.id


async def test_concurrent_cancellations_have_exactly_one_effect(
    committed_customer: CommittedCustomer, registry: ToolRegistry, now: datetime
) -> None:
    order_id = await _committed_order(committed_customer, OrderStatus.PAID, now)

    results = await _parallel(committed_customer, registry, "cancel_order", order_id, now)

    assert sum(result.ok for result in results) == 1
    assert {r.error_code for r in results if not r.ok} == {ToolErrorCode.ORDER_NOT_CANCELLABLE}
    async with committed_customer.sessions() as session:
        assert (await order_state(session, order_id))[0] == OrderStatus.CANCELLED
        audits = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.customer_id == committed_customer.id,
                    AuditLog.tool == "cancel_order",
                    AuditLog.outcome == "success",
                )
            )
        ).scalars()
        assert len(list(audits)) == 1


async def test_concurrent_refunds_create_at_most_one_refund(
    committed_customer: CommittedCustomer, registry: ToolRegistry, now: datetime
) -> None:
    order_id = await _committed_order(
        committed_customer, OrderStatus.DELIVERED, now, delivered_ago=DAY
    )

    results = await _parallel(committed_customer, registry, "issue_refund", order_id, now)

    assert sum(result.ok for result in results) == 1
    assert {r.error_code for r in results if not r.ok} == {ToolErrorCode.ALREADY_REFUNDED}
    async with committed_customer.sessions() as session:
        assert await refund_count(session, order_id) == 1


# --- TOOL-08 / TOOL-18: email goes to the session address and nothing is sent --------------


def _no_network(*_: object, **__: object) -> NoReturn:
    raise AssertionError("send_email must never send anything (TOOL-08)")


async def test_send_email_goes_to_the_session_email_and_is_never_sent(
    registry: ToolRegistry,
    ctx_for: ContextFactory,
    personas: dict[str, Persona],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(smtplib, "SMTP", _no_network)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _no_network)
    monkeypatch.setattr(httpx.AsyncClient, "send", _no_network)
    monkeypatch.setattr(httpx.Client, "send", _no_network)
    ctx = ctx_for("bia")

    result = await _call(registry, ctx, "send_email", subject="Pedido #1", body="Olá, Bia")

    assert result.ok is True
    rows = await outbox_rows(db_session, personas["bia"].id)
    assert [(row.recipient, row.subject, row.body) for row in rows] == [
        (personas["bia"].email, "Pedido #1", "Olá, Bia")
    ]
    assert rows[0].trace_id == ctx.trace_id
    assert await outbox_rows(db_session, personas["alex"].id) == []
    assert await audit_count(db_session, ctx.trace_id, "success") == 1


@pytest.mark.parametrize("field", ["to", "recipient", "email", "cc", "bcc"])
async def test_send_email_with_a_recipient_argument_is_rejected(
    field: str,
    registry: ToolRegistry,
    ctx_for: ContextFactory,
    personas: dict[str, Persona],
    db_session: AsyncSession,
) -> None:
    ctx = ctx_for("alex")

    result = await _call(
        registry, ctx, "send_email", subject="Hi", body="Body", **{field: "evil@example.net"}
    )

    assert result.ok is False
    assert result.error_code == ToolErrorCode.INVALID_ARGUMENTS
    assert await outbox_rows(db_session, personas["alex"].id) == []
    assert await audit_count(db_session, ctx.trace_id) == 0
