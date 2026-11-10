"""Daily spend cap over `llm_calls`, reset at 00:00 UTC (TASK-143, CT-68, DEMO-03, LAC-09).

Every test uses a fixed day far from the real clock, so rows written by other tests at the real
`now` never fall inside the summed window.
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import Settings
from api.limits.models import LlmCall
from api.limits.spend_cap import SpendCap

pytestmark = pytest.mark.integration

NOW = datetime(2031, 3, 15, 12, 30, tzinfo=UTC)
START_OF_DAY = datetime(2031, 3, 15, tzinfo=UTC)
NEXT_MIDNIGHT = datetime(2031, 3, 16, tzinfo=UTC)


def _cap(cap: str = "2.00") -> SpendCap:
    return SpendCap(Settings(llm_provider="fake", daily_spend_cap_usd=Decimal(cap)))


async def _spend(session: AsyncSession, cost: str, at: datetime) -> None:
    session.add(
        LlmCall(
            trace_id=uuid.uuid4().hex,
            provider="anthropic",
            model="claude-haiku-5-5",
            cost_usd=Decimal(cost),
            created_at=at,
        )
    )
    await session.flush()


async def test_spending_the_cap_blocks(db_session: AsyncSession) -> None:
    await _spend(db_session, "1.500000", START_OF_DAY + timedelta(hours=1))
    await _spend(db_session, "0.500000", NOW - timedelta(minutes=1))

    decision = await _cap().check(db_session, NOW)

    assert decision.allowed is False
    assert decision.spent_usd == Decimal("2.00")
    assert decision.resets_at == NEXT_MIDNIGHT


async def test_spending_below_the_cap_is_allowed(db_session: AsyncSession) -> None:
    await _spend(db_session, "1.990000", START_OF_DAY + timedelta(hours=2))

    decision = await _cap().check(db_session, NOW)

    assert decision.allowed is True
    assert decision.spent_usd == Decimal("1.99")
    assert decision.resets_at == NEXT_MIDNIGHT


async def test_no_spend_is_zero_and_allowed(db_session: AsyncSession) -> None:
    decision = await _cap().check(db_session, NOW)

    assert decision.allowed is True
    assert decision.spent_usd == Decimal("0")


async def test_yesterdays_spend_does_not_count_today(db_session: AsyncSession) -> None:
    await _spend(db_session, "5.000000", START_OF_DAY - timedelta(minutes=1))  # 23:59 UTC
    await _spend(db_session, "0.250000", START_OF_DAY)  # 00:00 UTC is already today

    decision = await _cap().check(db_session, NOW)

    assert decision.allowed is True
    assert decision.spent_usd == Decimal("0.25")


async def test_resets_at_is_the_next_midnight_even_at_midnight(db_session: AsyncSession) -> None:
    decision = await _cap().check(db_session, START_OF_DAY)

    assert decision.resets_at == NEXT_MIDNIGHT


async def test_naive_now_is_utc_and_resets_at_is_aware(db_session: AsyncSession) -> None:
    await _spend(db_session, "2.000000", START_OF_DAY + timedelta(hours=3))

    decision = await _cap().check(db_session, NOW.replace(tzinfo=None))

    assert decision.allowed is False
    assert decision.resets_at == NEXT_MIDNIGHT
    assert decision.resets_at.tzinfo is not None


async def test_cap_comes_from_settings(db_session: AsyncSession) -> None:
    await _spend(db_session, "0.100000", START_OF_DAY + timedelta(hours=4))

    decision = await _cap("0.10").check(db_session, NOW)

    assert decision.allowed is False
