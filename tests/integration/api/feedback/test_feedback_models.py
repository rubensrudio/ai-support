"""Feedback table: one row per message, rating CHECK, cascade and migration 0010 (TASK-135)."""

from collections.abc import Awaitable, Callable

import pytest
from alembic.command import downgrade, upgrade
from alembic.config import Config
from sqlalchemy import create_engine, delete, func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.models import Customer
from api.chat.models import Message
from api.feedback.models import Feedback

CustomerFactory = Callable[[], Awaitable[Customer]]
MessageFactory = Callable[..., Awaitable[Message]]

pytestmark = pytest.mark.integration


async def test_feedback_row_round_trips(
    db_session: AsyncSession, make_customer: CustomerFactory, make_message: MessageFactory
) -> None:
    customer = await make_customer()
    message = await make_message(customer)
    db_session.add(
        Feedback(
            message_id=message.id,
            customer_id=customer.id,
            rating="up",
            trace_id=message.trace_id,
        )
    )
    await db_session.flush()

    stored = (
        await db_session.execute(select(Feedback).where(Feedback.message_id == message.id))
    ).scalar_one()
    assert stored.customer_id == customer.id
    assert stored.rating == "up"
    assert stored.trace_id == message.trace_id
    assert stored.created_at is not None


async def test_message_id_is_unique(
    db_session: AsyncSession, make_customer: CustomerFactory, make_message: MessageFactory
) -> None:
    customer = await make_customer()
    message = await make_message(customer)
    for rating in ("up", "down"):
        db_session.add(
            Feedback(
                message_id=message.id,
                customer_id=customer.id,
                rating=rating,
                trace_id=message.trace_id,
            )
        )
    with pytest.raises(IntegrityError, match="uq_feedback_message_id"):
        await db_session.flush()


async def test_rating_check_rejects_other_values(
    db_session: AsyncSession, make_customer: CustomerFactory, make_message: MessageFactory
) -> None:
    customer = await make_customer()
    message = await make_message(customer)
    db_session.add(
        Feedback(
            message_id=message.id,
            customer_id=customer.id,
            rating="meh",
            trace_id=message.trace_id,
        )
    )
    with pytest.raises(IntegrityError, match="ck_feedback_rating"):
        await db_session.flush()


async def test_deleting_the_message_cascades_to_feedback(
    db_session: AsyncSession, make_customer: CustomerFactory, make_message: MessageFactory
) -> None:
    customer = await make_customer()
    message = await make_message(customer)
    db_session.add(
        Feedback(
            message_id=message.id,
            customer_id=customer.id,
            rating="down",
            trace_id=message.trace_id,
        )
    )
    await db_session.flush()

    await db_session.execute(delete(Message).where(Message.id == message.id))

    count = await db_session.scalar(
        select(func.count()).select_from(Feedback).where(Feedback.message_id == message.id)
    )
    assert count == 0


def test_migration_0010_downgrade_and_upgrade(
    alembic_config: Config, migrated_database: str
) -> None:
    downgrade(alembic_config, "0009")
    engine = create_engine(migrated_database)
    try:
        with engine.connect() as connection:
            assert "feedback" not in inspect(connection).get_table_names()
        upgrade(alembic_config, "head")
        with engine.connect() as connection:
            inspector = inspect(connection)
            assert "feedback" in inspector.get_table_names()
            indexes = {index["name"] for index in inspector.get_indexes("feedback")}
            assert "ix_feedback_customer_id" in indexes
    finally:
        engine.dispose()
