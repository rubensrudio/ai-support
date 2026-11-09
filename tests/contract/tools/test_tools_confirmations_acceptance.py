"""Pending confirmations are owned, single-use and expire (TOOL-05, TOOL-96, TOOL-97, CT-44)."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from conftest import Persona
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tools_acceptance_helpers import new_trace

from api.agent.confirmations import ConfirmationStore
from api.agent.models import PendingConfirmation
from api.chat.models import Conversation
from api.config import Settings, get_settings
from api.errors import AtlasError, ErrorCode

pytestmark = pytest.mark.integration


@pytest.fixture
def settings() -> Settings:
    return get_settings()


@pytest.fixture
def store(db_session: AsyncSession, settings: Settings) -> ConfirmationStore:
    return ConfirmationStore(db_session, settings)


async def _pending(
    store: ConfirmationStore, db_session: AsyncSession, owner: Persona, order_id: int = 42
) -> PendingConfirmation:
    conversation = Conversation(customer_id=owner.id)
    db_session.add(conversation)
    await db_session.flush()
    return await store.create(
        conversation_id=conversation.id,
        customer_id=owner.id,
        tool="cancel_order",
        arguments={"order_id": order_id},
        agent_state={"messages": []},
        trace_id=new_trace(),
    )


async def _status(db_session: AsyncSession, confirmation_id: uuid.UUID) -> str:
    statement = select(PendingConfirmation.status).where(PendingConfirmation.id == confirmation_id)
    return str((await db_session.execute(statement)).scalar_one())


async def test_create_stores_a_pending_confirmation(
    store: ConfirmationStore, db_session: AsyncSession, personas: dict[str, Persona]
) -> None:
    pending = await _pending(store, db_session, personas["alex"])

    assert pending.status == "pending"
    assert pending.resolved_at is None
    assert await _status(db_session, pending.id) == "pending"


async def test_other_customers_confirmation_is_the_same_404_as_a_missing_one(
    store: ConfirmationStore, db_session: AsyncSession, personas: dict[str, Persona]
) -> None:
    pending = await _pending(store, db_session, personas["alex"])

    with pytest.raises(AtlasError) as foreign:
        await store.resolve(pending.id, personas["bia"].id, "confirm")
    with pytest.raises(AtlasError) as missing:
        await store.resolve(uuid.uuid4(), personas["bia"].id, "confirm")

    assert foreign.value.code == ErrorCode.CONFIRMATION_NOT_FOUND
    assert foreign.value.http_status == 404
    assert (foreign.value.code, foreign.value.http_status, foreign.value.params) == (
        missing.value.code,
        missing.value.http_status,
        missing.value.params,
    )
    assert await _status(db_session, pending.id) == "pending"


@pytest.mark.parametrize("decision", ["confirm", "refuse"])
async def test_owner_resolves_once_and_a_second_resolution_is_409(
    decision: str,
    store: ConfirmationStore,
    db_session: AsyncSession,
    personas: dict[str, Persona],
) -> None:
    pending = await _pending(store, db_session, personas["alex"])
    expected = "confirmed" if decision == "confirm" else "refused"

    resolved = await store.resolve(pending.id, personas["alex"].id, decision)  # type: ignore[arg-type]

    assert resolved.status == expected
    assert resolved.resolved_at is not None
    for again in ("confirm", "refuse"):
        with pytest.raises(AtlasError) as second:
            await store.resolve(pending.id, personas["alex"].id, again)
        assert second.value.code == ErrorCode.CONFIRMATION_NOT_PENDING
        assert second.value.http_status == 409
    assert await _status(db_session, pending.id) == expected


async def test_expired_confirmation_never_becomes_confirmed(
    store: ConfirmationStore,
    db_session: AsyncSession,
    personas: dict[str, Persona],
    settings: Settings,
) -> None:
    pending = await _pending(store, db_session, personas["alex"])
    after_ttl = datetime.now(UTC) + timedelta(minutes=settings.confirmation_ttl_minutes + 1)

    with pytest.raises(AtlasError) as expired:
        await store.resolve(pending.id, personas["alex"].id, "confirm", now=after_ttl)

    assert expired.value.code == ErrorCode.CONFIRMATION_NOT_PENDING
    assert expired.value.http_status == 409
    assert await _status(db_session, pending.id) == "expired"
    with pytest.raises(AtlasError):
        await store.resolve(pending.id, personas["alex"].id, "confirm")
    assert await _status(db_session, pending.id) == "expired"


async def test_expire_stale_marks_old_pending_rows_and_they_cannot_be_confirmed(
    store: ConfirmationStore,
    db_session: AsyncSession,
    personas: dict[str, Persona],
    settings: Settings,
) -> None:
    pending = await _pending(store, db_session, personas["alex"])
    after_ttl = datetime.now(UTC) + timedelta(minutes=settings.confirmation_ttl_minutes + 1)

    assert await store.expire_stale(after_ttl) >= 1

    assert await _status(db_session, pending.id) == "expired"
    with pytest.raises(AtlasError) as late:
        await store.resolve(pending.id, personas["alex"].id, "confirm")
    assert late.value.code == ErrorCode.CONFIRMATION_NOT_PENDING
    assert await _status(db_session, pending.id) == "expired"


async def test_new_message_refuses_the_pending_confirmation(
    store: ConfirmationStore, db_session: AsyncSession, personas: dict[str, Persona]
) -> None:
    pending = await _pending(store, db_session, personas["alex"])

    refused = await store.refuse_pending(pending.conversation_id)

    assert [row.id for row in refused] == [pending.id]
    assert await _status(db_session, pending.id) == "refused"
    with pytest.raises(AtlasError) as late:
        await store.resolve(pending.id, personas["alex"].id, "confirm")
    assert late.value.code == ErrorCode.CONFIRMATION_NOT_PENDING
