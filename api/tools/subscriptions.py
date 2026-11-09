"""`get_subscription` (CT-42, TOOL-04, TOOL-10): the session customer's subscription only,
no side effects, no audit. No subscription -> `SUBSCRIPTION_NOT_FOUND`."""

from pydantic import BaseModel, ConfigDict

from api.tools.registry import ToolContext, ToolResult


class EmptyArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


async def get_subscription(args: EmptyArgs, ctx: ToolContext) -> ToolResult:
    raise NotImplementedError("TASK-072")
