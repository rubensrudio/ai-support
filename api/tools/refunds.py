"""Refund tools (CT-42, TOOL-04, TOOL-07, TOOL-09, TOOL-15, TOOL-90, TOOL-93, LAC-05).

`check_refund_policy` returns `{order_id, eligible, reason_code, amount_cents, currency}` for an
order of the session customer (an order with a refund -> `already_refunded`). `issue_refund`
re-evaluates the policy on the server with the order locked (`SELECT ... FOR UPDATE`), whether or
not `check_refund_policy` ran before: not eligible -> `REFUND_NOT_ELIGIBLE`, no row; an existing
refund or a unique violation -> `ALREADY_REFUNDED` (TOOL-93); success inserts the refund and an
audit row in the same transaction. No payment gateway.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError

from api.tools.audit import record_audit
from api.tools.models import CURRENCY, Order, Refund
from api.tools.orders import order_not_found
from api.tools.refund_policy import RefundEligibility, evaluate_refund
from api.tools.registry import ToolContext, ToolErrorCode, ToolResult

REFUND_UNIQUE_CONSTRAINT = "uq_refunds_order_id"


class CheckRefundArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: int = Field(ge=1)


class IssueRefundArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: int = Field(ge=1)


def _policy_content(order_id: int, eligibility: RefundEligibility) -> dict[str, Any]:
    return {
        "order_id": order_id,
        "eligible": eligibility.eligible,
        "reason_code": eligibility.reason_code,
        "amount_cents": eligibility.amount_cents,
        "currency": CURRENCY,
    }


def _already_refunded(order_id: int) -> ToolResult:
    return ToolResult(
        ok=False, content={"order_id": order_id}, error_code=ToolErrorCode.ALREADY_REFUNDED
    )


async def _owned_order(ctx: ToolContext, order_id: int, *, lock: bool) -> Order | None:
    statement = (
        select(Order)
        .where(Order.id == order_id, Order.customer_id == ctx.customer_id)
        .execution_options(populate_existing=True)
    )
    if lock:
        statement = statement.with_for_update()
    return (await ctx.session.execute(statement)).scalar_one_or_none()


async def _has_refund(ctx: ToolContext, order_id: int) -> bool:
    statement = select(exists().where(Refund.order_id == order_id))
    return bool((await ctx.session.execute(statement)).scalar_one())


async def check_refund_policy(args: CheckRefundArgs, ctx: ToolContext) -> ToolResult:
    order = await _owned_order(ctx, args.order_id, lock=False)
    if order is None:
        return order_not_found(args.order_id)
    if await _has_refund(ctx, order.id):
        eligibility = RefundEligibility(False, "already_refunded", order.total_cents)
    else:
        eligibility = evaluate_refund(order, ctx.now)
    return ToolResult(ok=True, content=_policy_content(order.id, eligibility))


async def _refundable(
    ctx: ToolContext, order_id: int, *, lock: bool
) -> tuple[Order, RefundEligibility] | ToolResult:
    """The order and its eligibility if a refund may be issued, else the refusal result."""
    order = await _owned_order(ctx, order_id, lock=lock)
    if order is None:
        return order_not_found(order_id)
    if await _has_refund(ctx, order.id):
        return _already_refunded(order.id)
    eligibility = evaluate_refund(order, ctx.now)
    if not eligibility.eligible:
        return ToolResult(
            ok=False,
            content=_policy_content(order.id, eligibility),
            error_code=ToolErrorCode.REFUND_NOT_ELIGIBLE,
        )
    return order, eligibility


async def precheck_issue_refund(args: IssueRefundArgs, ctx: ToolContext) -> ToolResult:
    """Read-only check run by the agent loop before asking for confirmation.

    Same refusals as `issue_refund`, without a row lock, writes or audit; the locked
    re-evaluation in `issue_refund` stays the guarantee.
    """
    checked = await _refundable(ctx, args.order_id, lock=False)
    if isinstance(checked, ToolResult):
        return checked
    order, eligibility = checked
    return ToolResult(ok=True, content=_policy_content(order.id, eligibility))


async def issue_refund(args: IssueRefundArgs, ctx: ToolContext) -> ToolResult:
    # The row lock serialises concurrent refunds of the same order: the second one waits, then
    # sees the committed refund. UNIQUE(order_id) is the last line of defence (TOOL-93).
    checked = await _refundable(ctx, args.order_id, lock=True)
    if isinstance(checked, ToolResult):
        return checked
    order, eligibility = checked
    refund = Refund(
        order_id=order.id,
        customer_id=ctx.customer_id,
        amount_cents=eligibility.amount_cents,
        currency=CURRENCY,
        trace_id=ctx.trace_id,
    )
    try:
        async with ctx.session.begin_nested():
            ctx.session.add(refund)
    except IntegrityError as error:
        if REFUND_UNIQUE_CONSTRAINT not in str(error.orig):
            raise
        return _already_refunded(order.id)
    content = {
        "order_id": order.id,
        "refund_id": refund.id,
        "amount_cents": refund.amount_cents,
        "currency": CURRENCY,
    }
    await record_audit(
        ctx.session,
        customer_id=ctx.customer_id,
        tool="issue_refund",
        arguments={"order_id": args.order_id},
        result=content,
        outcome="success",
        trace_id=ctx.trace_id,
    )
    return ToolResult(ok=True, content=content)
