"""`get_subscription` and `send_email` tools (TASK-072, TOOL-04, TOOL-08, TOOL-10, TOOL-18)."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.models import Customer
from api.tools.email import SendEmailArgs, send_email
from api.tools.models import AuditLog, EmailOutbox, Subscription
from api.tools.registry import IDENTITY_FIELDS, ToolContext, ToolErrorCode
from api.tools.subscriptions import EmptyArgs, get_subscription

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


async def _customer(session: AsyncSession, name: str) -> Customer:
    username = f"{name}-{uuid.uuid4().hex[:8]}"
    customer = Customer(
        id=uuid.uuid4(),
        username=username,
        display_name=name.title(),
        email=f"{username}@example.com",
        password_hash="x",
        language="en",
    )
    session.add(customer)
    await session.flush()
    return customer


def _ctx(session: AsyncSession, customer: Customer, trace_id: str = "trace-1") -> ToolContext:
    return ToolContext(
        customer_id=customer.id,
        customer_email=customer.email,
        trace_id=trace_id,
        session=session,
        language="en",
        now=NOW,
    )


async def _subscribe(session: AsyncSession, customer: Customer, plan: str, price: int) -> None:
    session.add(
        Subscription(
            customer_id=customer.id,
            plan_code=plan,
            status="active",
            price_cents=price,
            currency="USD",
            renews_at=NOW + timedelta(days=12),
        )
    )
    await session.flush()


async def _count(session: AsyncSession, model: type[AuditLog] | type[EmailOutbox]) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def test_get_subscription_returns_only_the_session_customer_subscription(
    db_session: AsyncSession,
) -> None:
    alex = await _customer(db_session, "alex")
    bia = await _customer(db_session, "bia")
    await _subscribe(db_session, alex, "plus", 3490)
    await _subscribe(db_session, bia, "premium", 5490)

    result = await get_subscription(EmptyArgs(), _ctx(db_session, alex))

    assert result.ok
    assert result.error_code is None
    assert result.content == {
        "plan_code": "plus",
        "status": "active",
        "amount_cents": 3490,
        "currency": "USD",
        "renews_at": (NOW + timedelta(days=12)).isoformat(),
    }


async def test_get_subscription_without_subscription_is_not_found_even_if_others_have_one(
    db_session: AsyncSession,
) -> None:
    alex = await _customer(db_session, "alex")
    bia = await _customer(db_session, "bia")
    await _subscribe(db_session, bia, "premium", 5490)

    result = await get_subscription(EmptyArgs(), _ctx(db_session, alex))

    assert not result.ok
    assert result.error_code == ToolErrorCode.SUBSCRIPTION_NOT_FOUND
    assert "premium" not in str(result.content)


async def test_get_subscription_writes_no_audit(db_session: AsyncSession) -> None:
    alex = await _customer(db_session, "alex")
    await _subscribe(db_session, alex, "plus", 3490)
    before = await _count(db_session, AuditLog)

    await get_subscription(EmptyArgs(), _ctx(db_session, alex))
    await get_subscription(EmptyArgs(), _ctx(db_session, await _customer(db_session, "x")))

    assert await _count(db_session, AuditLog) == before


async def test_send_email_records_outbox_to_the_session_email_and_one_audit(
    db_session: AsyncSession,
) -> None:
    alex = await _customer(db_session, "alex")
    await _customer(db_session, "bia")
    args = SendEmailArgs(subject="Your order summary", body="Order 12 was cancelled.")

    result = await send_email(args, _ctx(db_session, alex, trace_id="trace-mail"))

    assert result.ok
    outbox = (await db_session.scalars(select(EmailOutbox))).all()
    assert len(outbox) == 1
    email = outbox[0]
    assert email.recipient == alex.email
    assert email.customer_id == alex.id
    assert (email.subject, email.body) == ("Your order summary", "Order 12 was cancelled.")
    assert email.trace_id == "trace-mail"
    assert result.content == {
        "email_id": email.id,
        "recipient": alex.email,
        "subject": "Your order summary",
    }

    audits = (await db_session.scalars(select(AuditLog))).all()
    assert len(audits) == 1
    audit = audits[0]
    assert (audit.tool, audit.outcome, audit.trace_id) == ("send_email", "success", "trace-mail")
    assert audit.customer_id == alex.id
    assert audit.arguments["subject"] == "Your order summary"
    assert audit.arguments["body_length"] == len("Order 12 was cancelled.")
    assert "Order 12 was cancelled." not in str(audit.arguments)


def test_send_email_schema_has_no_recipient_field() -> None:
    schema = SendEmailArgs.model_json_schema()

    assert set(schema["properties"]) == {"subject", "body"}
    assert not set(schema["properties"]) & (IDENTITY_FIELDS | {"to", "recipient", "email"})
    assert schema.get("additionalProperties") is False


def test_send_email_rejects_a_recipient_argument() -> None:
    with pytest.raises(ValueError, match="to"):
        SendEmailArgs.model_validate({"subject": "s", "body": "b", "to": "evil@example.com"})


async def test_send_email_does_not_commit(db_session: AsyncSession) -> None:
    alex = await _customer(db_session, "alex")

    await send_email(SendEmailArgs(subject="s", body="b"), _ctx(db_session, alex))
    await db_session.rollback()

    assert await _count(db_session, EmailOutbox) == 0
    assert await _count(db_session, AuditLog) == 0
