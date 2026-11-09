import uuid

import pytest
from alembic.command import downgrade, upgrade
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.models import Customer
from api.chat.models import Conversation, Message

pytestmark = pytest.mark.integration


async def _conversation(session: AsyncSession) -> Conversation:
    customer = Customer(
        id=uuid.uuid4(),
        username=f"u-{uuid.uuid4().hex[:8]}",
        display_name="U",
        email="u@example.test",
        password_hash="x",
        language="en",
    )
    session.add(customer)
    await session.flush()
    conversation = Conversation(customer_id=customer.id)
    session.add(conversation)
    await session.flush()
    return conversation


def _message(conversation: Conversation, content: str, status: str = "complete") -> Message:
    return Message(
        conversation_id=conversation.id,
        role="user",
        content=content,
        status=status,
        language="en",
        trace_id="t",
    )


def test_migration_0003_downgrade_and_upgrade(
    alembic_config: Config, migrated_database: str
) -> None:
    downgrade(alembic_config, "0002")
    upgrade(alembic_config, "head")


async def test_messages_read_back_in_arrival_order(db_session: AsyncSession) -> None:
    conversation = await _conversation(db_session)
    db_session.add(_message(conversation, "first"))
    await db_session.flush()
    db_session.add(_message(conversation, "second"))
    await db_session.flush()
    rows = (
        await db_session.execute(
            select(Message).where(Message.conversation_id == conversation.id).order_by(Message.id)
        )
    ).scalars()
    assert [m.content for m in rows] == ["first", "second"]


async def test_status_outside_the_allowed_set_is_rejected(db_session: AsyncSession) -> None:
    conversation = await _conversation(db_session)
    db_session.add(_message(conversation, "x", status="weird"))
    with pytest.raises(IntegrityError):
        await db_session.flush()
