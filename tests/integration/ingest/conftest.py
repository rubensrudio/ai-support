"""Fixtures for the ingestion pipeline tests.

The pipeline commits one transaction per document, so these tests use the real session factory
on the per-session test database: each test starts with an empty `documents` table and deletes
its own documents (`ingest-test-*`) afterwards.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.db.engine import get_sessionmaker
from api.rag.models import Document

KEY_PREFIX = "ingest-test-"


@pytest.fixture
async def session_factory(
    migrated_database: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    factory = get_sessionmaker()
    # A run retires every stored document that is not in its manifest, so start from an empty
    # table: leftovers from another test would show up in the report as "removed from manifest".
    async with factory() as session, session.begin():
        await session.execute(delete(Document))
    yield factory
    async with factory() as session, session.begin():
        await session.execute(delete(Document).where(Document.doc_key.startswith(KEY_PREFIX)))
