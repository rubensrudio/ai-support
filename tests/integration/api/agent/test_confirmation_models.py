import uuid

import pytest
from alembic.command import downgrade, upgrade
from alembic.config import Config
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.agent.models import PendingConfirmation
from api.auth.models import Customer
from api.chat.models import Conversation

pytestmark = pytest.mark.integration


async def _conversation(session: AsyncSession) -> Conversation:
    name = f"confirm-{uuid.uuid4().hex[:8]}"
    customer = Customer(
        id=uuid.uuid4(),
        username=name,
        display_name="Confirm",
        email=f"{name}@example.test",
        password_hash="x",
        language="pt",
    )
    session.add(customer)
    await session.flush()
    conversation = Conversation(customer_id=customer.id)
    session.add(conversation)
    await session.flush()
    return conversation


def _pending(conversation: Conversation, status: str = "pending") -> PendingConfirmation:
    return PendingConfirmation(
        conversation_id=conversation.id,
        customer_id=conversation.customer_id,
        tool="cancel_order",
        arguments={"order_id": 7},
        agent_state={"messages": []},
        status=status,
        trace_id="t",
    )


async def test_pending_confirmation_round_trips(db_session: AsyncSession) -> None:
    conversation = await _conversation(db_session)
    row = _pending(conversation)
    db_session.add(row)
    await db_session.flush()
    await db_session.refresh(row)
    assert row.status == "pending"
    assert row.arguments == {"order_id": 7}
    assert row.created_at is not None and row.resolved_at is None


async def test_status_outside_the_four_values_is_rejected(db_session: AsyncSession) -> None:
    conversation = await _conversation(db_session)
    db_session.add(_pending(conversation, status="maybe"))
    with pytest.raises(IntegrityError, match="ck_pending_confirmations_status"):
        await db_session.flush()


def test_migration_0008_downgrade_and_upgrade(
    alembic_config: Config, migrated_database: str
) -> None:
    downgrade(alembic_config, "0007")
    upgrade(alembic_config, "head")
