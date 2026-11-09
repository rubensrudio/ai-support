"""Async SQLAlchemy engine and sessions (CT-5, DA-3).

SQLAlchemy 2 async over psycopg 3. The engine is cached per process; tests call
`reset_engine()` after pointing `ATLAS_DATABASE_URL` at their own database.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from api.config import get_settings


class Base(DeclarativeBase):
    """Declarative base for every ORM model; Alembic reads `Base.metadata`."""


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        _engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    get_engine()
    if _sessionmaker is None:  # pragma: no cover - get_engine() always sets it
        raise RuntimeError("Session factory not initialised")
    return _sessionmaker


async def dispose_engine() -> None:
    """Close pooled connections (app shutdown, tests)."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


def reset_engine() -> None:
    """Forget the cached engine without awaiting (sync test fixtures)."""
    global _engine, _sessionmaker
    if _engine is not None:
        _engine.sync_engine.dispose(close=False)
    _engine = None
    _sessionmaker = None


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Unit of work: commits when the block succeeds, rolls back when it raises."""
    session = get_sessionmaker()()
    try:
        yield session
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    finally:
        await session.close()


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request with commit/rollback semantics."""
    async with session_scope() as session:
        yield session
