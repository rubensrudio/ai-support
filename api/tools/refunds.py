"""Refund tools (CT-42, TOOL-04, TOOL-07, TOOL-09, TOOL-15, TOOL-90, TOOL-93, LAC-05).

`check_refund_policy` returns `{order_id, eligible, reason_code, amount_cents, currency}` for an
order of the session customer (an order with a refund -> `already_refunded`). `issue_refund`
re-evaluates the policy on the server with the order locked (`SELECT ... FOR UPDATE`), whether or
not `check_refund_policy` ran before: not eligible -> `REFUND_NOT_ELIGIBLE`, no row; an existing
refund or a unique violation -> `ALREADY_REFUNDED` (TOOL-93); success inserts the refund and an
audit row in the same transaction. No payment gateway.
"""

from pydantic import BaseModel, ConfigDict, Field

from api.tools.registry import ToolContext, ToolResult


class CheckRefundArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: int = Field(ge=1)


class IssueRefundArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: int = Field(ge=1)


async def check_refund_policy(args: CheckRefundArgs, ctx: ToolContext) -> ToolResult:
    raise NotImplementedError("TASK-071")


async def issue_refund(args: IssueRefundArgs, ctx: ToolContext) -> ToolResult:
    raise NotImplementedError("TASK-071")
