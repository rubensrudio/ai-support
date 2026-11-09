"""Order tools (CT-42, TOOL-04, TOOL-06, TOOL-10, TOOL-17, TOOL-90..92, TOOL-99, DA-21).

Every query filters `customer_id = ctx.customer_id`. A missing order and another customer's
order return the same `ORDER_NOT_FOUND` result (TOOL-90). `cancel_order` is a single
`UPDATE ... WHERE id AND customer_id AND status IN ('pending','paid') RETURNING`: zero rows ->
`ORDER_NOT_CANCELLABLE` (or `ORDER_NOT_FOUND` when the order is not the customer's), so two
concurrent cancellations have exactly one effect (TOOL-92). A successful cancellation writes an
audit row in the same transaction. Money is returned as `{amount_cents, currency: 'USD'}`.
Confirmation is the agent loop's job, not this module's.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update

from api.tools.audit import record_audit
from api.tools.models import CANCELLABLE_STATUSES, CURRENCY, Order, OrderStatus
from api.tools.registry import ToolContext, ToolErrorCode, ToolResult


class ListOrdersArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(default=10, ge=1, le=50)


class GetOrderArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: int = Field(ge=1)


class CancelOrderArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: int = Field(ge=1)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def order_content(order: Order) -> dict[str, Any]:
    """Customer-facing view of an order (money as integer cents in USD, dates ISO 8601)."""
    return {
        "order_id": order.id,
        "status": str(order.status),
        "amount_cents": order.total_cents,
        "currency": CURRENCY,
        "items": list(order.items),
        "created_at": _iso(order.created_at),
        "shipped_at": _iso(order.shipped_at),
        "delivered_at": _iso(order.delivered_at),
        "cancelled_at": _iso(order.cancelled_at),
    }


def order_not_found(order_id: int) -> ToolResult:
    """Same result for a missing order and another customer's order (TOOL-90)."""
    return ToolResult(
        ok=False, content={"order_id": order_id}, error_code=ToolErrorCode.ORDER_NOT_FOUND
    )


async def list_orders(args: ListOrdersArgs, ctx: ToolContext) -> ToolResult:
    statement = (
        select(Order)
        .where(Order.customer_id == ctx.customer_id)
        .order_by(Order.created_at.desc(), Order.id.desc())
        .limit(args.limit)
    )
    orders = (await ctx.session.execute(statement)).scalars()
    return ToolResult(ok=True, content={"orders": [order_content(order) for order in orders]})


def _not_cancellable(order_id: int, status: str) -> ToolResult:
    return ToolResult(
        ok=False,
        content={"order_id": order_id, "status": str(status)},
        error_code=ToolErrorCode.ORDER_NOT_CANCELLABLE,
    )


async def _owned_order(ctx: ToolContext, order_id: int) -> Order | None:
    statement = select(Order).where(Order.id == order_id, Order.customer_id == ctx.customer_id)
    return (await ctx.session.execute(statement)).scalar_one_or_none()


async def get_order(args: GetOrderArgs, ctx: ToolContext) -> ToolResult:
    order = await _owned_order(ctx, args.order_id)
    if order is None:
        return order_not_found(args.order_id)
    return ToolResult(ok=True, content=order_content(order))


async def precheck_cancel_order(args: CancelOrderArgs, ctx: ToolContext) -> ToolResult:
    """Read-only check run by the agent loop before asking for confirmation.

    Same outcomes as `cancel_order` without writing or auditing; the conditional UPDATE in
    `cancel_order` stays the guarantee if the order changes in between.
    """
    order = await _owned_order(ctx, args.order_id)
    if order is None:
        return order_not_found(args.order_id)
    if order.status not in CANCELLABLE_STATUSES:
        return _not_cancellable(order.id, order.status)
    return ToolResult(ok=True, content=order_content(order))


async def cancel_order(args: CancelOrderArgs, ctx: ToolContext) -> ToolResult:
    # One conditional UPDATE (DA-21): the row lock makes a concurrent second cancellation
    # re-check the status after the first commits and match zero rows (TOOL-92).
    statement = (
        update(Order)
        .where(
            Order.id == args.order_id,
            Order.customer_id == ctx.customer_id,
            Order.status.in_([status.value for status in CANCELLABLE_STATUSES]),
        )
        .values(status=OrderStatus.CANCELLED.value, cancelled_at=ctx.now)
        .returning(Order)
        .execution_options(synchronize_session="fetch", populate_existing=True)
    )
    cancelled = (await ctx.session.execute(statement)).scalar_one_or_none()
    if cancelled is None:
        current = (
            await ctx.session.execute(
                select(Order.status).where(
                    Order.id == args.order_id, Order.customer_id == ctx.customer_id
                )
            )
        ).scalar_one_or_none()
        if current is None:
            return order_not_found(args.order_id)
        return _not_cancellable(args.order_id, current)
    content = order_content(cancelled)
    await record_audit(
        ctx.session,
        customer_id=ctx.customer_id,
        tool="cancel_order",
        arguments={"order_id": args.order_id},
        result=content,
        outcome="success",
        trace_id=ctx.trace_id,
    )
    return ToolResult(ok=True, content=content)
