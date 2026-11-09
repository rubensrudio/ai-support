"""Audit log of side-effect tools (CT-39, TOOL-06, TOOL-07, TOOL-08).

Each handler decides when to audit (successful `cancel_order`, `issue_refund`, `send_email`;
reads are not audited). `list_successful_actions(trace_id)` lists what really happened in a
request, so the agent (AGT-08) and the output guardrail (GRD-08) never claim an action that has
no `success` row.
"""

import uuid
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.tools.models import AuditLog

Outcome = Literal["success", "error", "refused"]


async def record_audit(
    session: AsyncSession,
    *,
    customer_id: uuid.UUID,
    tool: str,
    arguments: dict[str, Any],
    result: dict[str, Any],
    outcome: Outcome,
    trace_id: str,
) -> AuditLog:
    """Add an audit row in the caller's transaction (flushed, not committed)."""
    if outcome not in ("success", "error", "refused"):
        raise ValueError(f"Invalid audit outcome: {outcome!r}")
    row = AuditLog(
        customer_id=customer_id,
        tool=tool,
        arguments=dict(arguments),
        result=dict(result),
        outcome=outcome,
        trace_id=trace_id,
    )
    session.add(row)
    await session.flush()
    return row


async def list_successful_actions(session: AsyncSession, trace_id: str) -> list[AuditLog]:
    """`success` rows of `trace_id`, oldest first."""
    statement = (
        select(AuditLog)
        .where(AuditLog.trace_id == trace_id, AuditLog.outcome == "success")
        .order_by(AuditLog.created_at, AuditLog.id)
    )
    return list((await session.execute(statement)).scalars())
