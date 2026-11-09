"""Commerce seed per persona (TASK-066, TOOL-01, DEMO-06, TOOL-19, TOOL-98).

For each persona: one subscription and orders in every status, with dates relative to `now`:
a cancellable paid order (and a pending one), a shipped one, delivered 3, 7 (exactly 7×24h) and
10 days ago, and a cancelled one; USD cents. Idempotent: running it again resets the persona's
orders, refunds and subscription to the same initial state (base of the daily demo reset) and
never deletes conversations. Returns a summary of what was seeded.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class CommerceSeedResult:
    customers: int
    orders: int
    subscriptions: int


async def seed_commerce(session: AsyncSession, now: datetime | None = None) -> CommerceSeedResult:
    raise NotImplementedError("TASK-066")
