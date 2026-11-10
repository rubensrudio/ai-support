"""Fixtures for the maintenance contract tests (CT-71..CT-73, DEMO-07, DEMO-10, DEMO-12, AS-5).

The restore and purge tests run inside the rolled-back `db_session` of `tests/conftest.py`: the
personas get their seed (`seed_personas` + `seed_commerce`), a non-persona customer gets data of
its own, and `make_activity` creates one row in every table the jobs touch (conversation with a
message, a feedback and a pending confirmation, audit row, email, ticket, LLM call) at a given
time. The scheduler tests need committed rows (`tick` opens its own sessions), so they clean up
what they create themselves.

This folder has no `__init__.py`; shared types live in `maintenance_contract_helpers.py`, a
unique module name, so nothing collides with `tests/integration/api/maintenance` (a package named
`maintenance`) in the same pytest run.
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from maintenance_contract_helpers import (
    PERSONA_PASSWORD,
    ActivityFn,
    ExistingFn,
    Persona,
    Rows,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from api.agent.models import PendingConfirmation
from api.auth.models import Customer
from api.chat.models import Conversation, Message
from api.config import get_settings
from api.feedback.models import Feedback
from api.limits.models import LlmCall
from api.seed.commerce import seed_commerce
from api.seed.personas import PERSONAS, seed_personas
from api.tickets.models import Ticket
from api.tools.models import AuditLog, EmailOutbox, Order, OrderStatus


@pytest.fixture
def maintenance_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("ATLAS_PERSONA_PASSWORD", PERSONA_PASSWORD)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


@pytest.fixture
async def personas(
    maintenance_env: None, db_session: AsyncSession, now: datetime
) -> dict[str, Persona]:
    """The seeded personas (alex, bia) with their commerce seed at `now`."""
    ids = await seed_personas(db_session, PERSONA_PASSWORD)
    await seed_commerce(db_session, now)
    await db_session.flush()
    return {
        spec.username: Persona(id=customer_id, username=spec.username, email=spec.email)
        for spec, customer_id in zip(PERSONAS, ids, strict=True)
    }


@pytest.fixture
async def outsider(db_session: AsyncSession) -> Persona:
    """A customer that is not a persona, with one paid order."""
    suffix = uuid.uuid4().hex[:10]
    customer = Customer(
        username=f"maint-outsider-{suffix}",
        display_name="Outsider",
        email=f"maint-outsider-{suffix}@example.com",
        password_hash="not-a-real-hash",  # noqa: S106 - fictitious value, never verified
        language="en",
    )
    db_session.add(customer)
    await db_session.flush()
    db_session.add(
        Order(
            customer_id=customer.id,
            status=OrderStatus.PAID.value,
            total_cents=1_234,
            currency="USD",
            items=[{"sku": "outsider-sku", "name": "Outsider item", "qty": 1}],
        )
    )
    await db_session.flush()
    return Persona(id=customer.id, username=customer.username, email=customer.email)


@pytest.fixture
def make_activity(db_session: AsyncSession) -> ActivityFn:
    """One row of `owner` in every table the jobs touch, all created at `at`."""

    async def _make(owner: Persona, at: datetime) -> Rows:
        trace_id = uuid.uuid4().hex
        conversation = Conversation(customer_id=owner.id, created_at=at)
        db_session.add(conversation)
        await db_session.flush()
        message = Message(
            conversation_id=conversation.id,
            role="assistant",
            content=f"answer to {owner.username}",
            status="complete",
            language="en",
            trace_id=trace_id,
            created_at=at,
        )
        db_session.add(message)
        await db_session.flush()
        feedback = Feedback(
            message_id=message.id,
            customer_id=owner.id,
            rating="up",
            trace_id=trace_id,
            created_at=at,
        )
        confirmation = PendingConfirmation(
            conversation_id=conversation.id,
            customer_id=owner.id,
            tool="cancel_order",
            arguments={"order_id": 1},
            agent_state={},
            trace_id=trace_id,
            created_at=at,
        )
        audit = AuditLog(
            customer_id=owner.id,
            tool="cancel_order",
            arguments={"order_id": 1},
            result={"ok": True},
            outcome="success",
            trace_id=trace_id,
            created_at=at,
        )
        email = EmailOutbox(
            customer_id=owner.id,
            recipient=owner.email,
            subject="Your order",
            body="Your order was cancelled.",
            trace_id=trace_id,
            created_at=at,
        )
        ticket = Ticket(
            conversation_id=conversation.id,
            customer_id=owner.id,
            reason="customer asked for a human",
            trace_id=trace_id,
            created_at=at,
        )
        call = LlmCall(
            trace_id=trace_id,
            provider="anthropic",
            model="claude-sonnet-5-5",
            cost_usd=Decimal("0.010000"),
            created_at=at,
        )
        db_session.add_all([feedback, confirmation, audit, email, ticket, call])
        await db_session.flush()
        return Rows(
            conversations=[conversation.id],
            messages=[message.id],
            feedback=[feedback.id],
            confirmations=[confirmation.id],
            audit_rows=[audit.id],
            emails=[email.id],
            tickets=[ticket.id],
            llm_calls=[call.id],
        )

    return _make


async def _still_there[K: (int, uuid.UUID)](
    session: AsyncSession, column: InstrumentedAttribute[K], wanted: list[K]
) -> list[K]:
    if not wanted:
        return []
    found = set((await session.scalars(select(column).where(column.in_(wanted)))).all())
    return [key for key in wanted if key in found]


@pytest.fixture
def existing(db_session: AsyncSession) -> ExistingFn:
    """The subset of `rows` that still exists in the database (same order)."""

    async def _existing(rows: Rows) -> Rows:
        session = db_session
        return Rows(
            conversations=await _still_there(session, Conversation.id, rows.conversations),
            messages=await _still_there(session, Message.id, rows.messages),
            feedback=await _still_there(session, Feedback.id, rows.feedback),
            confirmations=await _still_there(session, PendingConfirmation.id, rows.confirmations),
            audit_rows=await _still_there(session, AuditLog.id, rows.audit_rows),
            emails=await _still_there(session, EmailOutbox.id, rows.emails),
            tickets=await _still_there(session, Ticket.id, rows.tickets),
            llm_calls=await _still_there(session, LlmCall.id, rows.llm_calls),
        )

    return _existing
