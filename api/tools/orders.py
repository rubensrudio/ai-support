"""Order tools (CT-42, TOOL-04, TOOL-06, TOOL-10, TOOL-17, TOOL-90..92, TOOL-99, DA-21).

Every query filters `customer_id = ctx.customer_id`. A missing order and another customer's
order return the same `ORDER_NOT_FOUND` result (TOOL-90). `cancel_order` is a single
`UPDATE ... WHERE id AND customer_id AND status IN ('pending','paid') RETURNING`: zero rows ->
`ORDER_NOT_CANCELLABLE` (or `ORDER_NOT_FOUND` when the order is not the customer's), so two
concurrent cancellations have exactly one effect (TOOL-92). A successful cancellation writes an
audit row in the same transaction. Money is returned as `{amount_cents, currency: 'USD'}`.
Confirmation is the agent loop's job, not this module's.
"""

from pydantic import BaseModel, ConfigDict, Field

from api.tools.registry import ToolContext, ToolResult


class ListOrdersArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(default=10, ge=1, le=50)


class GetOrderArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: int = Field(ge=1)


class CancelOrderArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: int = Field(ge=1)


async def list_orders(args: ListOrdersArgs, ctx: ToolContext) -> ToolResult:
    raise NotImplementedError("TASK-069")


async def get_order(args: GetOrderArgs, ctx: ToolContext) -> ToolResult:
    raise NotImplementedError("TASK-069")


async def cancel_order(args: CancelOrderArgs, ctx: ToolContext) -> ToolResult:
    raise NotImplementedError("TASK-069")
