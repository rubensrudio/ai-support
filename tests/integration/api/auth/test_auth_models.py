import uuid
from datetime import UTC, datetime, timedelta

import pytest
from alembic.command import downgrade, upgrade
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.models import Customer, CustomerSession

pytestmark = pytest.mark.integration


def _customer(username: str, language: str = "en") -> Customer:
    return Customer(
        id=uuid.uuid4(),
        username=username,
        display_name=username.title(),
        email=f"{username}@example.test",
        password_hash="x",
        language=language,
    )


def test_migration_0002_downgrade_and_upgrade(
    alembic_config: Config, migrated_database: str
) -> None:
    downgrade(alembic_config, "0001")
    upgrade(alembic_config, "head")


async def test_username_is_unique(db_session: AsyncSession) -> None:
    db_session.add(_customer("dup"))
    await db_session.flush()
    db_session.add(_customer("dup"))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_language_must_be_pt_or_en(db_session: AsyncSession) -> None:
    db_session.add(_customer("fr-user", language="fr"))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_session_is_deleted_with_its_customer(db_session: AsyncSession) -> None:
    customer = _customer("cascade")
    db_session.add(customer)
    await db_session.flush()
    db_session.add(
        CustomerSession(
            customer_id=customer.id,
            token_hash="a" * 64,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    await db_session.flush()
    await db_session.delete(customer)
    await db_session.flush()
    remaining = (await db_session.execute(select(CustomerSession))).scalars().all()
    assert [s for s in remaining if s.customer_id == customer.id] == []
