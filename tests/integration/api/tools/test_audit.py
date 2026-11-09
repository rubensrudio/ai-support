import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.models import Customer
from api.tools.audit import list_successful_actions, record_audit
from api.tools.models import AuditLog

pytestmark = pytest.mark.integration


async def _customer(session: AsyncSession) -> Customer:
    name = f"audit-{uuid.uuid4().hex[:8]}"
    customer = Customer(
        id=uuid.uuid4(),
        username=name,
        display_name="Audit",
        email=f"{name}@example.test",
        password_hash="x",
        language="en",
    )
    session.add(customer)
    await session.flush()
    return customer


async def test_record_audit_persists_every_field(db_session: AsyncSession) -> None:
    customer = await _customer(db_session)

    row = await record_audit(
        db_session,
        customer_id=customer.id,
        tool="cancel_order",
        arguments={"order_id": 12},
        result={"order_id": 12, "status": "cancelled"},
        outcome="success",
        trace_id="trace-a",
    )

    stored = (await db_session.execute(select(AuditLog).where(AuditLog.id == row.id))).scalar_one()
    assert stored.customer_id == customer.id
    assert stored.tool == "cancel_order"
    assert stored.arguments == {"order_id": 12}
    assert stored.result == {"order_id": 12, "status": "cancelled"}
    assert stored.outcome == "success"
    assert stored.trace_id == "trace-a"
    assert stored.created_at is not None


async def test_list_successful_actions_returns_only_successes_of_the_trace(
    db_session: AsyncSession,
) -> None:
    customer = await _customer(db_session)
    for tool, outcome, trace in [
        ("cancel_order", "success", "trace-b"),
        ("issue_refund", "error", "trace-b"),
        ("send_email", "refused", "trace-b"),
        ("send_email", "success", "trace-other"),
        ("send_email", "success", "trace-b"),
    ]:
        await record_audit(
            db_session,
            customer_id=customer.id,
            tool=tool,
            arguments={},
            result={},
            outcome=outcome,  # type: ignore[arg-type]
            trace_id=trace,
        )

    actions = await list_successful_actions(db_session, "trace-b")

    assert [action.tool for action in actions] == ["cancel_order", "send_email"]


async def test_record_audit_rejects_an_unknown_outcome(db_session: AsyncSession) -> None:
    customer = await _customer(db_session)
    with pytest.raises(ValueError, match="outcome"):
        await record_audit(
            db_session,
            customer_id=customer.id,
            tool="x",
            arguments={},
            result={},
            outcome="maybe",  # type: ignore[arg-type]
            trace_id="t",
        )
