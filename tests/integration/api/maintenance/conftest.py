"""Shared helpers for the maintenance job tests (TASK-146, TASK-147, TASK-148)."""

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import datetime

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.models import Customer
from api.chat.models import Conversation, Message
from api.config import get_settings
from api.db.engine import session_scope
from api.limits.models import MaintenanceRun


async def make_customer(session: AsyncSession, username: str | None = None) -> Customer:
    name = username or f"visitor-{uuid.uuid4().hex[:8]}"
    customer = Customer(
        username=name,
        display_name=name.title(),
        email=f"{name}@example.com",
        password_hash="not-a-real-hash",
        language="en",
    )
    session.add(customer)
    await session.flush()
    return customer


async def make_conversation(
    session: AsyncSession, customer_id: uuid.UUID, created_at: datetime | None = None
) -> Conversation:
    conversation = Conversation(customer_id=customer_id)
    if created_at is not None:
        conversation.created_at = created_at
    session.add(conversation)
    await session.flush()
    session.add(
        Message(
            conversation_id=conversation.id,
            role="user",
            content="Where is my order?",
            status="complete",
            language="en",
            trace_id=uuid.uuid4().hex,
        )
    )
    await session.flush()
    return conversation


@pytest.fixture
def settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    """`monkeypatch` for `ATLAS_*` variables that never leaks a cached `Settings` to later tests."""
    get_settings.cache_clear()
    try:
        yield monkeypatch
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


@pytest.fixture
async def clean_maintenance_runs(migrated_database: str) -> AsyncIterator[None]:
    """The scheduler commits on its own connections: start and end with no run rows."""
    async with session_scope() as session:
        await session.execute(delete(MaintenanceRun))
    yield
    async with session_scope() as session:
        await session.execute(delete(MaintenanceRun))
