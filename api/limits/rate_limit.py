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
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import func, select, text

from api.limits.models import RateLimitEvent

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from api.config import Settings

WINDOW_SECONDS = 3600
WINDOW = timedelta(seconds=WINDOW_SECONDS)
# Namespaced so the per-customer key never collides with other advisory locks in the app.
_LOCK_SQL = text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))")
_LOCK_NAMESPACE = "atlas:rate_limit:"


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
        now = now.replace(tzinfo=UTC) if now.tzinfo is None else now
        # Serialises count + insert per customer until the caller's transaction ends.
        await session.execute(_LOCK_SQL, {"key": f"{_LOCK_NAMESPACE}{customer_id}"})
        row = (
            await session.execute(
                select(func.count(), func.min(RateLimitEvent.created_at)).where(
                    RateLimitEvent.customer_id == customer_id,
                    RateLimitEvent.created_at > now - WINDOW,
                    RateLimitEvent.created_at <= now,
                )
            )
        ).one()
        count, oldest = int(row[0]), row[1]
        if count >= self._settings.rate_limit_per_hour:
            return RateDecision(allowed=False, retry_at=oldest + WINDOW)
        session.add(RateLimitEvent(customer_id=customer_id, created_at=now))
        await session.flush()
        return RateDecision(allowed=True, retry_at=None)
