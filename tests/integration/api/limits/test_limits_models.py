"""Ledger, rate limit and maintenance tables and migration 0011 (TASK-141, CT-66)."""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from alembic.command import downgrade, upgrade
from alembic.config import Config
from sqlalchemy import create_engine, delete, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.models import Customer
from api.chat.models import Conversation
from api.limits.models import LlmCall, MaintenanceRun, RateLimitEvent
from api.tickets.models import Ticket

pytestmark = pytest.mark.integration


async def _customer(session: AsyncSession) -> Customer:
    suffix = uuid.uuid4().hex[:12]
    customer = Customer(
        username=f"limits-{suffix}",
        display_name=f"Limits {suffix}",
        email=f"limits-{suffix}@example.test",
        password_hash="not-a-real-hash",  # noqa: S106 - fixture value, never verified
        language="en",
    )
    session.add(customer)
    await session.flush()
    return customer


async def test_llm_call_keeps_six_decimal_places(db_session: AsyncSession) -> None:
    db_session.add(
        LlmCall(
            trace_id=uuid.uuid4().hex,
            provider="anthropic",
            model="claude-haiku-5-5",
            cost_usd=Decimal("0.001234"),
        )
    )
    await db_session.flush()
    stored = (await db_session.execute(select(LlmCall))).scalars().one()
    assert stored.cost_usd == Decimal("0.001234")
    assert stored.created_at is not None


async def test_rate_limit_event_cascades_with_customer(db_session: AsyncSession) -> None:
    customer = await _customer(db_session)
    db_session.add(RateLimitEvent(customer_id=customer.id, created_at=datetime.now(UTC)))
    await db_session.flush()
    await db_session.execute(delete(Customer).where(Customer.id == customer.id))
    remaining = (await db_session.execute(select(RateLimitEvent))).scalars().all()
    assert remaining == []


async def test_maintenance_run_is_unique_per_job_and_date(db_session: AsyncSession) -> None:
    db_session.add(MaintenanceRun(job="restore", run_date=date(2026, 11, 10)))
    db_session.add(MaintenanceRun(job="purge", run_date=date(2026, 11, 10)))
    db_session.add(MaintenanceRun(job="restore", run_date=date(2026, 11, 11)))
    await db_session.flush()
    db_session.add(MaintenanceRun(job="restore", run_date=date(2026, 11, 10)))
    with pytest.raises(IntegrityError, match="uq_maintenance_runs_job_run_date"):
        await db_session.flush()


async def test_ticket_survives_conversation_delete(db_session: AsyncSession) -> None:
    customer = await _customer(db_session)
    conversation = Conversation(customer_id=customer.id)
    db_session.add(conversation)
    await db_session.flush()
    ticket = Ticket(
        conversation_id=conversation.id,
        customer_id=customer.id,
        reason="all providers failed",
        trace_id=uuid.uuid4().hex,
    )
    db_session.add(ticket)
    await db_session.flush()
    await db_session.execute(delete(Conversation).where(Conversation.id == conversation.id))
    row = (
        await db_session.execute(
            select(Ticket.id, Ticket.conversation_id).where(Ticket.id == ticket.id)
        )
    ).one()
    assert row.conversation_id is None


def test_migration_0011_downgrade_and_upgrade(
    alembic_config: Config, migrated_database: str
) -> None:
    tables = ("llm_calls", "rate_limit_events", "maintenance_runs")
    downgrade(alembic_config, "0010")
    engine = create_engine(migrated_database)
    try:
        with engine.connect() as connection:
            inspector = inspect(connection)
            assert not set(tables) & set(inspector.get_table_names())
            column = {c["name"]: c for c in inspector.get_columns("tickets")}["conversation_id"]
            assert column["nullable"] is False
        upgrade(alembic_config, "head")
        with engine.connect() as connection:
            inspector = inspect(connection)
            assert set(tables) <= set(inspector.get_table_names())
            indexes = {index["name"] for index in inspector.get_indexes("llm_calls")}
            assert "ix_llm_calls_created_at" in indexes
            column = {c["name"]: c for c in inspector.get_columns("tickets")}["conversation_id"]
            assert column["nullable"] is True
            fks = [
                f
                for f in inspector.get_foreign_keys("tickets")
                if f["referred_table"] == "conversations"
            ]
            assert fks and fks[0]["options"].get("ondelete") == "SET NULL"
    finally:
        engine.dispose()
