"""Daily spend cap over the LLM call ledger, reset at 00:00 UTC (CT-68, DEMO-03, DEMO-08, DEMO-09).

Contract (orchestrator skeleton; TASK-143 implements it without changing signatures):

- `SpendCap(settings).check(session, now)`: sums `llm_calls.cost_usd` with
  `created_at >= today 00:00 UTC` (today = the UTC date of `now`; a naive `now` is UTC).
  `spent_usd >= settings.daily_spend_cap_usd` (default 2.00) -> `allowed=False`.
  `resets_at` is always the next 00:00 UTC after `now` (LAC-09). Read only: it never writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from api.limits.models import LlmCall

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from api.config import Settings


@dataclass(frozen=True)
class CapDecision:
    allowed: bool
    spent_usd: Decimal
    resets_at: datetime


class SpendCap:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def check(self, session: AsyncSession, now: datetime) -> CapDecision:
        start_of_day = _start_of_utc_day(now)
        spent = await session.scalar(
            select(func.coalesce(func.sum(LlmCall.cost_usd), 0)).where(
                LlmCall.created_at >= start_of_day
            )
        )
        spent_usd = Decimal(spent or 0)
        return CapDecision(
            allowed=spent_usd < self._settings.daily_spend_cap_usd,
            spent_usd=spent_usd,
            resets_at=start_of_day + timedelta(days=1),
        )


def _start_of_utc_day(now: datetime) -> datetime:
    """00:00 UTC of the UTC date of `now` (a naive `now` is UTC)."""
    utc = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    return utc.replace(hour=0, minute=0, second=0, microsecond=0)
