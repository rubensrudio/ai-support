"""Daily spend cap over the LLM call ledger, reset at 00:00 UTC (CT-68, DEMO-03, DEMO-08, DEMO-09).

Contract (orchestrator skeleton; TASK-143 implements it without changing signatures):

- `SpendCap(settings).check(session, now)`: sums `llm_calls.cost_usd` with
  `created_at >= today 00:00 UTC` (today = the UTC date of `now`; a naive `now` is UTC).
  `spent_usd >= settings.daily_spend_cap_usd` (default 2.00) -> `allowed=False`.
  `resets_at` is always the next 00:00 UTC after `now` (LAC-09). Read only: it never writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

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
        raise NotImplementedError("TASK-143")
