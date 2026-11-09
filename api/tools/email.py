"""`send_email` (CT-42, TOOL-08, TOOL-18, LAC-27): writes to `email_outbox` with
`recipient = ctx.customer_email` (the schema has no recipient field), audits it and sends
nothing."""

from pydantic import BaseModel, ConfigDict, Field

from api.tools.audit import record_audit
from api.tools.models import EmailOutbox
from api.tools.registry import ToolContext, ToolResult


class SendEmailArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=5000)


async def send_email(args: SendEmailArgs, ctx: ToolContext) -> ToolResult:
    """Record the email in the outbox and audit it, in the caller's transaction (no commit).

    No email ever leaves the system (TOOL-08). The audit keeps the subject and the body length,
    not the body itself; the full body stays only in `email_outbox`.
    """
    email = EmailOutbox(
        customer_id=ctx.customer_id,
        recipient=ctx.customer_email,
        subject=args.subject,
        body=args.body,
        trace_id=ctx.trace_id,
    )
    ctx.session.add(email)
    await ctx.session.flush()
    content = {"email_id": email.id, "recipient": email.recipient, "subject": email.subject}
    await record_audit(
        ctx.session,
        customer_id=ctx.customer_id,
        tool="send_email",
        arguments={"subject": args.subject, "body_length": len(args.body)},
        result=content,
        outcome="success",
        trace_id=ctx.trace_id,
    )
    return ToolResult(ok=True, content=content)
