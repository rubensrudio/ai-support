"""Conversation repository (TASK-028, CHAT-03, CHAT-93, CHAT-94)."""

import asyncio
import uuid

import pytest
from chat_helpers import CustomerFactory, create_customer
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.models import Customer
from api.chat.repository import ConversationRepository
from api.db.engine import get_sessionmaker
from api.errors import AtlasError, ErrorCode

pytestmark = pytest.mark.integration


async def test_create_and_get_owned(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    customer = await make_customer()
    repo = ConversationRepository(db_session)

    created = await repo.create(customer.id)
    found = await repo.get_owned(created.id, customer.id)

    assert found.id == created.id
    assert found.customer_id == customer.id
    assert found.summary is None
    assert found.summary_upto_message_id is None


async def test_missing_and_foreign_conversation_raise_the_same_error(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    owner = await make_customer()
    intruder = await make_customer()
    repo = ConversationRepository(db_session)
    conversation = await repo.create(owner.id)

    with pytest.raises(AtlasError) as missing:
        await repo.get_owned(uuid.uuid4(), intruder.id)
    with pytest.raises(AtlasError) as foreign:
        await repo.get_owned(conversation.id, intruder.id)

    for info in (missing, foreign):
        assert info.value.code is ErrorCode.CONVERSATION_NOT_FOUND
        assert info.value.http_status == 404
        assert info.value.params == {}
    assert str(missing.value) == str(foreign.value)


async def test_list_messages_returns_insertion_order(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    customer = await make_customer()
    repo = ConversationRepository(db_session)
    conversation = await repo.create(customer.id)
    other = await repo.create(customer.id)

    texts = ["first", "second", "third", "fourth"]
    for index, text in enumerate(texts):
        role = "user" if index % 2 == 0 else "assistant"
        await repo.append_message(conversation.id, role, text, "complete", "en", "trace-1")
    await repo.append_message(other.id, "user", "elsewhere", "complete", "en", "trace-2")

    messages = await repo.list_messages(conversation.id)

    assert [m.content for m in messages] == texts
    assert [m.role for m in messages] == ["user", "assistant", "user", "assistant"]
    assert [m.id for m in messages] == sorted(m.id for m in messages)
    assert all(m.conversation_id == conversation.id for m in messages)
    assert messages[0].trace_id == "trace-1"
    assert messages[0].language == "en"
    assert messages[0].status == "complete"


async def test_save_summary_updates_conversation(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    customer = await make_customer()
    repo = ConversationRepository(db_session)
    conversation = await repo.create(customer.id)
    message = await repo.append_message(conversation.id, "user", "hi", "complete", "en", "t")

    await repo.save_summary(conversation.id, "The customer said hi.", message.id)
    found = await repo.get_owned(conversation.id, customer.id)
    await db_session.refresh(found)

    assert found.summary == "The customer said hi."
    assert found.summary_upto_message_id == message.id


async def test_delete_for_customers_returns_deleted_count(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    first = await make_customer()
    second = await make_customer()
    kept = await make_customer()
    repo = ConversationRepository(db_session)
    await repo.create(first.id)
    await repo.create(first.id)
    await repo.create(second.id)
    survivor = await repo.create(kept.id)

    deleted = await repo.delete_for_customers([first.id, second.id])

    assert deleted == 3
    assert (await repo.get_owned(survivor.id, kept.id)).id == survivor.id
    assert await repo.delete_for_customers([]) == 0


async def test_concurrent_appends_persist_both_messages(migrated_database: str) -> None:
    """Two sessions append at the same time with no lock; the bigserial orders them."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as setup:
        customer = await create_customer(setup)
        conversation = await ConversationRepository(setup).create(customer.id)
        await setup.commit()
        customer_id, conversation_id = customer.id, conversation.id

    async def append(text: str) -> None:
        async with sessionmaker() as session:
            repo = ConversationRepository(session)
            await repo.append_message(conversation_id, "user", text, "complete", "en", "t")
            await session.commit()

    try:
        await asyncio.gather(append("from session A"), append("from session B"))

        async with sessionmaker() as check:
            messages = await ConversationRepository(check).list_messages(conversation_id)
        assert sorted(m.content for m in messages) == ["from session A", "from session B"]
        assert messages[0].id < messages[1].id
    finally:
        async with sessionmaker() as cleanup:
            await cleanup.execute(delete(Customer).where(Customer.id == customer_id))
            await cleanup.commit()
