"""Per-account sliding one-hour rate limit (CT-69, DEMO-02, DEMO-08, DEMO-09).

Contract (orchestrator skeleton; TASK-144 implements it without changing signatures):

- `RateLimiter(settings).check_and_record(session, customer_id, now)`: in the caller's
  transaction, takes `pg_advisory_xact_lock` keyed by the customer, counts
  `rate_limit_events` of that customer with `created_at > now - 1 hour`; below
  `settings.rate_limit_per_hour` (default 20) it inserts one event at `now` and returns
  `allowed=True, retry_at=None`; otherwise it inserts nothing and returns `allowed=False` with
  `retry_at` = the oldest event in the window + 1 hour. Count and insert are atomic per
  customer, so N concurrent calls allow exactly `limit` of them. The caller commits.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from api.config import Settings

WINDOW_SECONDS = 3600


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    retry_at: datetime | None


class RateLimiter:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def check_and_record(
        self, session: AsyncSession, customer_id: UUID, now: datetime
    ) -> RateDecision:
        raise NotImplementedError("TASK-144")
