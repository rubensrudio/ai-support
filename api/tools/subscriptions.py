"""`get_subscription` (CT-42, TOOL-04, TOOL-10): the session customer's subscription only,
no side effects, no audit. No subscription -> `SUBSCRIPTION_NOT_FOUND`."""

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from api.tools.models import Subscription
from api.tools.registry import ToolContext, ToolErrorCode, ToolResult


class EmptyArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


async def get_subscription(args: EmptyArgs, ctx: ToolContext) -> ToolResult:
    """Read-only lookup filtered by `ctx.customer_id` (never by a model-supplied value)."""
    subscription = await ctx.session.scalar(
        select(Subscription).where(Subscription.customer_id == ctx.customer_id)
    )
    if subscription is None:
        return ToolResult(ok=False, error_code=ToolErrorCode.SUBSCRIPTION_NOT_FOUND)
    renews_at = subscription.renews_at
    return ToolResult(
        ok=True,
        content={
            "plan_code": subscription.plan_code,
            "status": subscription.status,
            "amount_cents": subscription.price_cents,
            "currency": subscription.currency,
            "renews_at": renews_at.isoformat() if renews_at is not None else None,
        },
    )
