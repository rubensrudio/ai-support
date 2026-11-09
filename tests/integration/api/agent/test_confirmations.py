import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.agent.confirmations import ConfirmationStore
from api.agent.models import PendingConfirmation
from api.auth.models import Customer
from api.chat.models import Conversation
from api.config import Settings, get_settings
from api.db.engine import get_sessionmaker
from api.errors import AtlasError, ErrorCode

pytestmark = pytest.mark.integration

TTL_MINUTES = 15


@pytest.fixture
def settings() -> Settings:
    return get_settings().model_copy(update={"confirmation_ttl_minutes": TTL_MINUTES})


@pytest.fixture
def store(db_session: AsyncSession, settings: Settings) -> ConfirmationStore:
    return ConfirmationStore(db_session, settings)


async def _customer(session: AsyncSession) -> Customer:
    name = f"store-{uuid.uuid4().hex[:8]}"
    customer = Customer(
        id=uuid.uuid4(),
        username=name,
        display_name="Store",
        email=f"{name}@example.test",
        password_hash="x",
        language="en",
    )
    session.add(customer)
    await session.flush()
    return customer


async def _conversation(session: AsyncSession, customer: Customer | None = None) -> Conversation:
    owner = customer or await _customer(session)
    conversation = Conversation(customer_id=owner.id)
    session.add(conversation)
    await session.flush()
    return conversation


async def _create(store: ConfirmationStore, conversation: Conversation) -> PendingConfirmation:
    return await store.create(
        conversation_id=conversation.id,
        customer_id=conversation.customer_id,
        tool="cancel_order",
        arguments={"order_id": 1042},
        agent_state={"messages": [], "steps": 1},
        trace_id="trace-confirm",
    )


async def _status(session: AsyncSession, confirmation_id: uuid.UUID) -> str:
    result = await session.execute(
        select(PendingConfirmation.status).where(PendingConfirmation.id == confirmation_id)
    )
    return result.scalar_one()


async def test_create_inserts_a_pending_row(
    db_session: AsyncSession, store: ConfirmationStore
) -> None:
    conversation = await _conversation(db_session)
    row = await _create(store, conversation)

    assert isinstance(row.id, uuid.UUID)
    assert row.status == "pending"
    assert row.created_at is not None and row.resolved_at is None
    assert row.arguments == {"order_id": 1042}
    assert row.agent_state == {"messages": [], "steps": 1}
    assert await _status(db_session, row.id) == "pending"


async def test_confirm_marks_confirmed_with_resolved_at(
    db_session: AsyncSession, store: ConfirmationStore
) -> None:
    conversation = await _conversation(db_session)
    row = await _create(store, conversation)

    resolved = await store.resolve(row.id, conversation.customer_id, "confirm")

    assert resolved.id == row.id
    assert resolved.status == "confirmed"
    assert resolved.resolved_at is not None
    assert resolved.agent_state == {"messages": [], "steps": 1}
    assert await _status(db_session, row.id) == "confirmed"


async def test_refuse_marks_refused(db_session: AsyncSession, store: ConfirmationStore) -> None:
    conversation = await _conversation(db_session)
    row = await _create(store, conversation)

    resolved = await store.resolve(row.id, conversation.customer_id, "refuse")

    assert resolved.status == "refused"
    assert resolved.resolved_at is not None
    assert await _status(db_session, row.id) == "refused"


async def test_confirming_twice_returns_409_on_the_second(
    db_session: AsyncSession, store: ConfirmationStore
) -> None:
    conversation = await _conversation(db_session)
    row = await _create(store, conversation)
    await store.resolve(row.id, conversation.customer_id, "confirm")

    with pytest.raises(AtlasError) as excinfo:
        await store.resolve(row.id, conversation.customer_id, "confirm")

    assert excinfo.value.code is ErrorCode.CONFIRMATION_NOT_PENDING
    assert excinfo.value.http_status == 409
    assert await _status(db_session, row.id) == "confirmed"


async def test_refusing_after_confirming_returns_409(
    db_session: AsyncSession, store: ConfirmationStore
) -> None:
    conversation = await _conversation(db_session)
    row = await _create(store, conversation)
    await store.resolve(row.id, conversation.customer_id, "confirm")

    with pytest.raises(AtlasError) as excinfo:
        await store.resolve(row.id, conversation.customer_id, "refuse")

    assert excinfo.value.code is ErrorCode.CONFIRMATION_NOT_PENDING
    assert await _status(db_session, row.id) == "confirmed"


async def test_another_customer_gets_the_same_404_as_a_missing_id(
    db_session: AsyncSession, store: ConfirmationStore
) -> None:
    conversation = await _conversation(db_session)
    row = await _create(store, conversation)
    intruder = await _customer(db_session)

    with pytest.raises(AtlasError) as foreign:
        await store.resolve(row.id, intruder.id, "confirm")
    with pytest.raises(AtlasError) as missing:
        await store.resolve(uuid.uuid4(), intruder.id, "confirm")

    assert foreign.value.code is ErrorCode.CONFIRMATION_NOT_FOUND
    assert foreign.value.http_status == 404
    assert (foreign.value.code, foreign.value.http_status, foreign.value.params) == (
        missing.value.code,
        missing.value.http_status,
        missing.value.params,
    )
    assert str(foreign.value) == str(missing.value)
    assert await _status(db_session, row.id) == "pending"


async def test_another_customer_cannot_learn_a_resolved_confirmation_exists(
    db_session: AsyncSession, store: ConfirmationStore
) -> None:
    conversation = await _conversation(db_session)
    row = await _create(store, conversation)
    await store.resolve(row.id, conversation.customer_id, "confirm")
    intruder = await _customer(db_session)

    with pytest.raises(AtlasError) as excinfo:
        await store.resolve(row.id, intruder.id, "refuse")

    assert excinfo.value.code is ErrorCode.CONFIRMATION_NOT_FOUND
    assert excinfo.value.http_status == 404


async def test_confirmation_older_than_ttl_is_not_confirmed_and_becomes_expired(
    db_session: AsyncSession, store: ConfirmationStore
) -> None:
    conversation = await _conversation(db_session)
    row = await _create(store, conversation)
    later = row.created_at + timedelta(minutes=TTL_MINUTES, seconds=1)

    with pytest.raises(AtlasError) as excinfo:
        await store.resolve(row.id, conversation.customer_id, "confirm", now=later)

    assert excinfo.value.code is ErrorCode.CONFIRMATION_NOT_PENDING
    assert excinfo.value.http_status == 409
    assert await _status(db_session, row.id) == "expired"


async def test_confirmation_created_past_ttl_in_the_database_does_not_resolve(
    db_session: AsyncSession, store: ConfirmationStore
) -> None:
    conversation = await _conversation(db_session)
    row = await _create(store, conversation)
    await db_session.execute(
        update(PendingConfirmation)
        .where(PendingConfirmation.id == row.id)
        .values(created_at=datetime.now(UTC) - timedelta(minutes=TTL_MINUTES + 1))
    )

    with pytest.raises(AtlasError) as excinfo:
        await store.resolve(row.id, conversation.customer_id, "confirm")

    assert excinfo.value.code is ErrorCode.CONFIRMATION_NOT_PENDING
    assert await _status(db_session, row.id) == "expired"


async def test_confirmation_just_inside_ttl_still_resolves(
    db_session: AsyncSession, store: ConfirmationStore
) -> None:
    conversation = await _conversation(db_session)
    row = await _create(store, conversation)
    almost = row.created_at + timedelta(minutes=TTL_MINUTES, seconds=-1)

    resolved = await store.resolve(row.id, conversation.customer_id, "confirm", now=almost)

    assert resolved.status == "confirmed"
    assert resolved.resolved_at == almost


async def test_refuse_pending_marks_every_pending_of_the_conversation_only(
    db_session: AsyncSession, store: ConfirmationStore
) -> None:
    conversation = await _conversation(db_session)
    other = await _conversation(db_session)
    first = await _create(store, conversation)
    second = await _create(store, conversation)
    already_confirmed = await _create(store, conversation)
    await store.resolve(already_confirmed.id, conversation.customer_id, "confirm")
    untouched = await _create(store, other)

    refused = await store.refuse_pending(conversation.id)

    assert {row.id for row in refused} == {first.id, second.id}
    assert all(row.status == "refused" and row.resolved_at is not None for row in refused)
    assert await _status(db_session, first.id) == "refused"
    assert await _status(db_session, second.id) == "refused"
    assert await _status(db_session, already_confirmed.id) == "confirmed"
    assert await _status(db_session, untouched.id) == "pending"


async def test_refuse_pending_without_pending_returns_empty(
    db_session: AsyncSession, store: ConfirmationStore
) -> None:
    conversation = await _conversation(db_session)

    assert await store.refuse_pending(conversation.id) == []


async def test_expire_stale_marks_only_rows_past_ttl(
    db_session: AsyncSession, store: ConfirmationStore
) -> None:
    conversation = await _conversation(db_session)
    stale = await _create(store, conversation)
    fresh = await _create(store, conversation)
    confirmed = await _create(store, conversation)
    await store.resolve(confirmed.id, conversation.customer_id, "confirm")
    old = datetime.now(UTC) - timedelta(minutes=TTL_MINUTES + 5)
    await db_session.execute(
        update(PendingConfirmation)
        .where(PendingConfirmation.id.in_([stale.id, confirmed.id]))
        .values(created_at=old)
    )

    count = await store.expire_stale(datetime.now(UTC))

    assert count == 1
    assert await _status(db_session, stale.id) == "expired"
    assert await _status(db_session, fresh.id) == "pending"
    assert await _status(db_session, confirmed.id) == "confirmed"


@pytest.fixture
async def committed_pending(
    migrated_database: str, settings: Settings
) -> AsyncIterator[tuple[uuid.UUID, uuid.UUID]]:
    """A pending confirmation committed for real, so separate sessions can race on it."""
    factory = get_sessionmaker()
    async with factory() as session:
        customer = await _customer(session)
        conversation = await _conversation(session, customer)
        row = await _create(ConfirmationStore(session, settings), conversation)
        await session.commit()
        ids = (row.id, customer.id)
    try:
        yield ids
    finally:
        async with factory() as session:
            await session.execute(delete(Customer).where(Customer.id == ids[1]))
            await session.commit()


async def test_concurrent_resolves_have_exactly_one_success(
    committed_pending: tuple[uuid.UUID, uuid.UUID], settings: Settings
) -> None:
    confirmation_id, customer_id = committed_pending
    factory = get_sessionmaker()

    async def attempt() -> PendingConfirmation | AtlasError:
        async with factory() as session:
            try:
                row = await ConfirmationStore(session, settings).resolve(
                    confirmation_id, customer_id, "confirm"
                )
            except AtlasError as error:
                await session.rollback()
                return error
            await session.commit()
            return row

    outcomes = await asyncio.gather(attempt(), attempt())

    successes = [o for o in outcomes if isinstance(o, PendingConfirmation)]
    failures = [o for o in outcomes if isinstance(o, AtlasError)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0].code is ErrorCode.CONFIRMATION_NOT_PENDING
    async with factory() as session:
        assert await _status(session, confirmation_id) == "confirmed"
