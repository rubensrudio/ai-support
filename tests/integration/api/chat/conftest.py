"""Fixtures shared by the chat integration tests."""

import pytest
from chat_helpers import CustomerFactory, create_customer
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.models import Customer


@pytest.fixture
def make_customer(db_session: AsyncSession) -> CustomerFactory:
    async def _make(language: str = "en") -> Customer:
        return await create_customer(db_session, language)

    return _make
