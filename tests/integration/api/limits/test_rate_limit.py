"""Per-account sliding one-hour rate limit (TASK-144, CT-69, DEMO-02).

The sequential tests run inside the rolled-back `db_session`. The concurrency test needs real
commits from separate connections, so it commits its own customer and deletes it at the end
(the events go with it, `ON DELETE CASCADE`).
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.models import Customer
from api.config import Settings
from api.db.engine import get_sessionmaker
from api.limits.models import RateLimitEvent
from api.limits.rate_limit import RateLimiter

pytestmark = pytest.mark.integration

NOW = datetime(2031, 3, 15, 12, 0, tzinfo=UTC)
LIMIT = 20


def _limiter(limit: int = LIMIT) -> RateLimiter:
    return RateLimiter(Settings(llm_provider="fake", rate_limit_per_hour=limit))


def _new_customer() -> Customer:
    suffix = uuid.uuid4().hex[:12]
    return Customer(
        username=f"rate-{suffix}",
        display_name=f"Rate {suffix}",
        email=f"rate-{suffix}@example.test",
        password_hash="not-a-real-hash",  # noqa: S106 - fixture value, never verified
        language="en",
    )


@pytest.fixture
async def customer(db_session: AsyncSession) -> Customer:
    customer = _new_customer()
    db_session.add(customer)
    await db_session.flush()
    return customer


async def _events(session: AsyncSession, customer_id: uuid.UUID) -> int:
    count = await session.scalar(
        select(func.count()).where(RateLimitEvent.customer_id == customer_id)
    )
    return int(count or 0)


async def test_twenty_allowed_and_the_21st_refused_with_retry_at(
    db_session: AsyncSession, customer: Customer
) -> None:
    limiter = _limiter()
    first = NOW - timedelta(minutes=30)

    decisions = [
        await limiter.check_and_record(db_session, customer.id, first + timedelta(minutes=i))
        for i in range(LIMIT)
    ]
    refused = await limiter.check_and_record(db_session, customer.id, NOW)

    assert all(d.allowed and d.retry_at is None for d in decisions)
    assert refused.allowed is False
    assert refused.retry_at == first + timedelta(hours=1)
    assert await _events(db_session, customer.id) == LIMIT  # the refusal is not recorded


async def test_window_slides_events_older_than_one_hour_do_not_count(
    db_session: AsyncSession, customer: Customer
) -> None:
    old = NOW - timedelta(minutes=61)
    db_session.add_all(
        RateLimitEvent(customer_id=customer.id, created_at=old) for _ in range(LIMIT)
    )
    # Exactly one hour ago is already outside the `(now - 1h, now]` window.
    db_session.add(RateLimitEvent(customer_id=customer.id, created_at=NOW - timedelta(hours=1)))
    await db_session.flush()

    decision = await _limiter().check_and_record(db_session, customer.id, NOW)

    assert decision.allowed is True
    assert decision.retry_at is None


async def test_limit_is_per_customer(db_session: AsyncSession, customer: Customer) -> None:
    other = _new_customer()
    db_session.add(other)
    await db_session.flush()
    limiter = _limiter(limit=1)

    assert (await limiter.check_and_record(db_session, customer.id, NOW)).allowed is True
    assert (await limiter.check_and_record(db_session, customer.id, NOW)).allowed is False
    assert (await limiter.check_and_record(db_session, other.id, NOW)).allowed is True


async def test_the_limit_comes_from_settings(db_session: AsyncSession, customer: Customer) -> None:
    limiter = _limiter(limit=2)

    results = [
        (await limiter.check_and_record(db_session, customer.id, NOW)).allowed for _ in range(3)
    ]

    assert results == [True, True, False]


@pytest.fixture
async def committed_customer(migrated_database: str) -> AsyncIterator[uuid.UUID]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        customer = _new_customer()
        session.add(customer)
        await session.commit()
        customer_id = customer.id
    try:
        yield customer_id
    finally:
        async with sessionmaker() as session:
            await session.execute(delete(Customer).where(Customer.id == customer_id))
            await session.commit()


async def test_concurrent_calls_allow_exactly_the_limit(committed_customer: uuid.UUID) -> None:
    limiter = _limiter()
    sessionmaker = get_sessionmaker()

    async def _call() -> bool:
        async with sessionmaker() as session:
            decision = await limiter.check_and_record(session, committed_customer, NOW)
            await session.commit()
            return decision.allowed

    results = await asyncio.gather(*(_call() for _ in range(25)))

    assert results.count(True) == LIMIT
    async with sessionmaker() as session:
        assert await _events(session, committed_customer) == LIMIT
