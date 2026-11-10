"""Daily maintenance scheduler with an advisory lock (TASK-148, CT-73, DA-26)."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import api.main as main
from api.auth.models import Customer
from api.chat.models import Conversation
from api.db.engine import get_engine, session_scope
from api.limits.models import MaintenanceRun
from api.maintenance import scheduler
from api.maintenance.purge import PurgeReport
from api.maintenance.restore import RestoreReport
from api.seed.personas import PERSONAS, seed_personas
from api.tickets.models import Ticket
from tests.integration.api.maintenance.conftest import make_conversation

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("clean_maintenance_runs")]

NOW = datetime(2026, 11, 9, 3, 0, tzinfo=UTC)


class JobSpy:
    def __init__(self) -> None:
        self.restore: list[datetime] = []
        self.purge: list[tuple[datetime, int, Any]] = []
        self.fail_restore = False

    async def restore_demo(self, session: AsyncSession, now: datetime) -> RestoreReport:
        self.restore.append(now)
        if self.fail_restore:
            raise RuntimeError("restore exploded")
        return RestoreReport(conversations_deleted=0, orders_reset=0)

    async def purge_expired(
        self, session: AsyncSession, now: datetime, retention_days: int, langfuse: Any
    ) -> PurgeReport:
        self.purge.append((now, retention_days, langfuse))
        return PurgeReport(0, 0, 0, 0, 0, 0)


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> JobSpy:
    jobs = JobSpy()
    monkeypatch.setattr(scheduler, "restore_demo", jobs.restore_demo)
    monkeypatch.setattr(scheduler, "purge_expired", jobs.purge_expired)
    return jobs


async def _runs() -> list[tuple[str, object]]:
    async with session_scope() as session:
        rows = await session.execute(
            select(MaintenanceRun.job, MaintenanceRun.run_date).order_by(MaintenanceRun.id)
        )
        return [(job, run_date) for job, run_date in rows.all()]


async def test_two_ticks_on_the_same_day_run_each_job_once(spy: JobSpy) -> None:
    first = await scheduler.tick(NOW)
    second = await scheduler.tick(NOW + timedelta(hours=5))

    assert first == ["restore", "purge"]
    assert second == []
    assert spy.restore == [NOW]
    assert len(spy.purge) == 1
    assert spy.purge[0][1] == 7
    assert await _runs() == [("restore", NOW.date()), ("purge", NOW.date())]

    next_day = await scheduler.tick(NOW + timedelta(days=1))
    assert next_day == ["restore", "purge"]
    assert len(spy.restore) == 2


async def test_tick_runs_nothing_while_another_connection_holds_the_lock(spy: JobSpy) -> None:
    holder = await get_engine().connect()
    try:
        await holder.execute(text("SELECT pg_advisory_lock(:key)"), {"key": scheduler.LOCK_KEY})

        ran = await scheduler.tick(NOW)

        assert ran == []
        assert spy.restore == [] and spy.purge == []
        assert await _runs() == []
    finally:
        await holder.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": scheduler.LOCK_KEY})
        await holder.close()


async def test_tick_releases_the_lock(spy: JobSpy) -> None:
    await scheduler.tick(NOW)

    other = await get_engine().connect()
    try:
        taken = await other.scalar(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": scheduler.LOCK_KEY}
        )
        assert taken is True
        await other.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": scheduler.LOCK_KEY})
    finally:
        await other.close()


async def test_failing_job_is_logged_without_its_row_and_retried(
    spy: JobSpy, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        scheduler, "log_event", lambda event, **fields: events.append((event, fields))
    )
    spy.fail_restore = True

    first = await scheduler.tick(NOW)

    assert first == ["purge"]
    assert await _runs() == [("purge", NOW.date())]
    assert ("maintenance_failed", {"job": "restore", "exc_type": "RuntimeError"}) in events

    spy.fail_restore = False
    second = await scheduler.tick(NOW + timedelta(minutes=5))
    assert second == ["restore"]
    assert len(spy.restore) == 2


async def _committed_state() -> tuple[int, int, int]:
    async with session_scope() as session:
        personas = await session.scalar(
            select(func.count())
            .select_from(Customer)
            .where(Customer.username.in_([persona.username for persona in PERSONAS]))
        )
        conversations = await session.scalar(select(func.count()).select_from(Conversation))
        tickets = await session.scalar(select(func.count()).select_from(Ticket))
        return int(personas or 0), int(conversations or 0), int(tickets or 0)


async def test_tick_with_the_real_jobs_closes_its_langfuse_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real restore and purge, inside an outer transaction rolled back at the end.

    The jobs' `commit()` only releases a savepoint of that transaction, so nothing they write
    (persona data, deletions, `maintenance_runs` rows) reaches the shared test database.
    """
    closed: list[bool] = []

    class FakeLangfuseApi:
        enabled = False

        def __init__(self, settings: object) -> None:
            self.settings = settings

        async def delete_traces_older_than(self, cutoff: datetime) -> int:
            raise AssertionError("disabled client must not be called")

        async def aclose(self) -> None:
            closed.append(True)

    before = await _committed_state()
    connection = await get_engine().connect()
    transaction = await connection.begin()
    try:
        factory = async_sessionmaker(
            bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
        )
        async with factory() as setup:
            persona_ids = await seed_personas(setup, "persona-password-for-tests")
            await make_conversation(setup, persona_ids[0])
            await make_conversation(setup, persona_ids[1], NOW - timedelta(days=10))
            await setup.commit()
        monkeypatch.setattr(scheduler, "LangfuseApi", FakeLangfuseApi)
        monkeypatch.setattr(scheduler, "get_sessionmaker", lambda: factory)

        ran = await scheduler.tick(NOW)

        assert ran == ["restore", "purge"]
        assert closed == [True]
        async with factory() as check:
            conversations = await check.scalar(
                select(func.count())
                .select_from(Conversation)
                .where(Conversation.customer_id.in_(persona_ids))
            )
            runs = await check.scalar(select(func.count()).select_from(MaintenanceRun))
        assert conversations == 0
        assert runs == 2
    finally:
        await transaction.rollback()
        await connection.close()

    assert await _committed_state() == before
    assert await _runs() == []


async def test_scheduler_loop_survives_a_failing_tick(
    settings_env: pytest.MonkeyPatch,
) -> None:
    settings_env.setenv("ATLAS_MAINTENANCE_INTERVAL_SECONDS", "1")
    calls: list[datetime] = []
    second_call = asyncio.Event()

    async def flaky_tick(now: datetime) -> list[str]:
        calls.append(now)
        if len(calls) == 1:
            raise RuntimeError("database down")
        second_call.set()
        return []

    settings_env.setattr(scheduler, "tick", flaky_tick)

    task = scheduler.start_scheduler(main.app)
    try:
        await asyncio.wait_for(second_call.wait(), timeout=5)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(calls) >= 2
    assert all(call.tzinfo is not None for call in calls)


def _quiet_lifespan(monkeypatch: pytest.MonkeyPatch) -> None:
    async def noop() -> None:
        return None

    monkeypatch.setattr(main, "shutdown_tracing", lambda: None)
    monkeypatch.setattr(main, "close_embedder", noop)
    monkeypatch.setattr(main, "close_langfuse_api", noop)
    monkeypatch.setattr(main, "dispose_engine", noop)


async def test_lifespan_starts_and_cancels_the_scheduler_when_enabled(
    settings_env: pytest.MonkeyPatch,
) -> None:
    settings_env.setenv("ATLAS_MAINTENANCE_ENABLED", "true")
    _quiet_lifespan(settings_env)
    ticked = asyncio.Event()
    tasks: list[asyncio.Task[None]] = []

    async def fake_tick(now: datetime) -> list[str]:
        ticked.set()
        return []

    def capture(app: object) -> asyncio.Task[None]:
        task = scheduler.start_scheduler(main.app)
        tasks.append(task)
        return task

    settings_env.setattr(scheduler, "tick", fake_tick)
    settings_env.setattr(main, "start_scheduler", capture)

    async with main.lifespan(main.app):
        await asyncio.wait_for(ticked.wait(), timeout=5)
        assert len(tasks) == 1 and not tasks[0].done()

    assert tasks[0].cancelled()


async def test_lifespan_does_not_start_the_scheduler_when_disabled(
    settings_env: pytest.MonkeyPatch,
) -> None:
    settings_env.setenv("ATLAS_MAINTENANCE_ENABLED", "false")
    _quiet_lifespan(settings_env)
    started: list[object] = []
    settings_env.setattr(main, "start_scheduler", lambda app: started.append(app))

    async with main.lifespan(main.app):
        pass

    assert started == []
