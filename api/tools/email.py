"""`send_email` (CT-42, TOOL-08, TOOL-18, LAC-27): writes to `email_outbox` with
`recipient = ctx.customer_email` (the schema has no recipient field), audits it and sends
nothing."""

from pydantic import BaseModel, ConfigDict, Field

from api.tools.registry import ToolContext, ToolResult


class SendEmailArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=5000)


async def send_email(args: SendEmailArgs, ctx: ToolContext) -> ToolResult:
    raise NotImplementedError("TASK-072")
