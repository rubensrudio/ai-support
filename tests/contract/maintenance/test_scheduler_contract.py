"""Daily maintenance scheduler contract (CT-73, DA-26).

`tick(now)` runs each job (`restore`, then `purge`) at most once per UTC day, recorded in
`maintenance_runs`, and runs nothing while another connection holds the maintenance advisory
lock. `tick` opens its own sessions and commits, so the jobs themselves are replaced by spies
(`restore_demo`, `purge_expired` and `LangfuseApi` of `api.maintenance.scheduler`): the contract
is about scheduling, and no real restore, purge or Langfuse client ever touches the shared test
database. The only committed rows are the `maintenance_runs` of the day, deleted before and after
every test.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import Settings, get_settings
from api.db.engine import get_engine, get_sessionmaker
from api.limits.models import MaintenanceRun
from api.maintenance import scheduler
from api.maintenance.purge import PurgeReport
from api.maintenance.restore import RestoreReport
from api.maintenance.scheduler import JOBS, LOCK_KEY, tick

pytestmark = pytest.mark.integration


async def _clear_runs(run_date: date) -> None:
    async with get_sessionmaker()() as session:
        await session.execute(delete(MaintenanceRun).where(MaintenanceRun.run_date == run_date))
        await session.commit()


async def _runs(run_date: date) -> list[str]:
    session: AsyncSession
    async with get_sessionmaker()() as session:
        jobs = await session.scalars(
            select(MaintenanceRun.job)
            .where(MaintenanceRun.run_date == run_date)
            .order_by(MaintenanceRun.job)
        )
        return list(jobs.all())


class SpyLangfuse:
    """Stands in for `LangfuseApi` inside the scheduler: no HTTP client, no network."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.closed = False

    @property
    def enabled(self) -> bool:
        return False

    async def aclose(self) -> None:
        self.closed = True


@dataclass
class JobSpies:
    restores: list[datetime] = field(default_factory=list)
    purges: list[tuple[datetime, int, object]] = field(default_factory=list)


@pytest.fixture
def jobs(monkeypatch: pytest.MonkeyPatch) -> JobSpies:
    """Spies in place of the real jobs: they record the call and write nothing."""
    spies = JobSpies()

    async def _restore(session: AsyncSession, now: datetime) -> RestoreReport:
        spies.restores.append(now)
        return RestoreReport(conversations_deleted=0, orders_reset=0)

    async def _purge(
        session: AsyncSession, now: datetime, retention_days: int, langfuse: object
    ) -> PurgeReport:
        spies.purges.append((now, retention_days, langfuse))
        return PurgeReport(0, 0, 0, 0, 0, 0)

    monkeypatch.setattr(scheduler, "restore_demo", _restore)
    monkeypatch.setattr(scheduler, "purge_expired", _purge)
    monkeypatch.setattr(scheduler, "LangfuseApi", SpyLangfuse)
    return spies


@pytest.fixture
async def tick_now(migrated_database: str) -> AsyncIterator[datetime]:
    """Now (UTC) for `tick`, with no `maintenance_runs` row for today before or after the test."""
    now = datetime.now(UTC).replace(microsecond=0)
    await _clear_runs(now.date())
    yield now
    await _clear_runs(now.date())


async def test_jobs_run_once_per_utc_day(tick_now: datetime, jobs: JobSpies) -> None:
    first = await tick(tick_now)
    second = await tick(tick_now)

    assert first == list(JOBS) == ["restore", "purge"]
    assert second == []
    assert await _runs(tick_now.date()) == sorted(JOBS)
    assert jobs.restores == [tick_now]
    assert len(jobs.purges) == 1
    purge_now, retention_days, langfuse = jobs.purges[0]
    assert purge_now == tick_now
    assert retention_days == get_settings().retention_days
    assert isinstance(langfuse, SpyLangfuse)
    assert langfuse.closed, "the scheduler closes its Langfuse client"


async def test_nothing_runs_while_another_connection_holds_the_lock(
    tick_now: datetime, jobs: JobSpies
) -> None:
    async with get_engine().connect() as holder:
        locked = await holder.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK_KEY})
        assert locked is True
        try:
            ran = await tick(tick_now)
        finally:
            await holder.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK_KEY})
            await holder.commit()

    assert ran == []
    assert await _runs(tick_now.date()) == []
    assert jobs.restores == []
    assert jobs.purges == []


async def test_lock_is_released_after_a_tick(tick_now: datetime, jobs: JobSpies) -> None:
    assert await tick(tick_now) == list(JOBS)

    async with get_engine().connect() as other:
        locked = await other.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK_KEY})
        await other.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK_KEY})
        await other.commit()

    assert locked is True, "tick must release the advisory lock"
