import uuid

import pytest
from alembic.command import downgrade, upgrade
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.models import Customer
from api.chat.models import Conversation
from api.tickets.models import Ticket
from api.tickets.service import open_ticket

pytestmark = pytest.mark.integration


async def test_open_ticket_records_conversation_reason_and_trace(db_session: AsyncSession) -> None:
    name = f"ticket-{uuid.uuid4().hex[:8]}"
    customer = Customer(
        id=uuid.uuid4(),
        username=name,
        display_name="Ticket",
        email=f"{name}@example.test",
        password_hash="x",
        language="en",
    )
    db_session.add(customer)
    await db_session.flush()
    conversation = Conversation(customer_id=customer.id)
    db_session.add(conversation)
    await db_session.flush()

    ticket = await open_ticket(
        db_session,
        conversation_id=conversation.id,
        customer_id=customer.id,
        reason="all_providers_failed",
        trace_id="trace-x",
    )

    stored = (await db_session.execute(select(Ticket).where(Ticket.id == ticket.id))).scalar_one()
    assert stored.conversation_id == conversation.id
    assert stored.customer_id == customer.id
    assert stored.reason == "all_providers_failed"
    assert stored.trace_id == "trace-x"
    assert stored.created_at is not None


def test_migration_0009_downgrade_and_upgrade(
    alembic_config: Config, migrated_database: str
) -> None:
    downgrade(alembic_config, "0008")
    upgrade(alembic_config, "head")
