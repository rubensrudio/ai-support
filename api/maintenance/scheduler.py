"""In-process daily maintenance loop guarded by a Postgres advisory lock (CT-73, DA-26).

Contract (orchestrator skeleton; TASK-148 implements it without changing signatures):

- `tick(now) -> list[str]`: opens its own session, takes `pg_try_advisory_lock(<fixed key>)`
  on that connection; if it is held elsewhere, returns `[]` without running anything. With the
  lock, for each job in `JOBS` (`"restore"` then `"purge"`) with no `maintenance_runs` row for
  `(job, now's UTC date)`, runs it (`restore_demo`, `purge_expired` with
  `settings.retention_days` and a `LangfuseApi`), inserts the row and commits; returns the jobs
  that ran. A failing job is logged (`maintenance_failed`, job and error class) and rolled
  back without its row, so a later tick retries it. The lock is always released.
- `start_scheduler(app) -> asyncio.Task`: a loop that calls `tick(utc now)` every
  `settings.maintenance_interval_seconds` and never dies on an error; `api.main` starts it in
  the lifespan only when `ATLAS_MAINTENANCE_ENABLED=true` and cancels it on shutdown.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select, text

from api.config import get_settings
from api.db.engine import get_engine, get_sessionmaker
from api.limits.models import MaintenanceRun
from api.maintenance.purge import PurgeReport, purge_expired
from api.maintenance.restore import RestoreReport, restore_demo
from api.observability.langfuse_api import LangfuseApi
from api.observability.logging import log_event

if TYPE_CHECKING:
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession

JOBS: tuple[str, ...] = ("restore", "purge")
# Fixed int64 key of the maintenance advisory lock ("ATLASMNT" in ASCII).
LOCK_KEY = 0x41544C41534D4E54
_TRY_LOCK = text("SELECT pg_try_advisory_lock(:key)")
_UNLOCK = text("SELECT pg_advisory_unlock(:key)")


def _utc_date(now: datetime) -> date:
    aware = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    return aware.date()


async def _run(job: str, session: AsyncSession, now: datetime) -> RestoreReport | PurgeReport:
    if job == "restore":
        return await restore_demo(session, now)
    if job == "purge":
        settings = get_settings()
        langfuse = LangfuseApi(settings)
        try:
            return await purge_expired(session, now, settings.retention_days, langfuse)
        finally:
            await langfuse.aclose()
    raise ValueError(f"unknown maintenance job: {job}")


async def _run_once(job: str, now: datetime, run_date: date) -> bool:
    """Run `job` unless today's row exists; the job and its row commit in one transaction."""
    session = get_sessionmaker()()
    try:
        done = await session.scalar(
            select(MaintenanceRun.id).where(
                MaintenanceRun.job == job, MaintenanceRun.run_date == run_date
            )
        )
        if done is not None:
            return False
        report = await _run(job, session, now)
        session.add(MaintenanceRun(job=job, run_date=run_date))
        await session.commit()
    except Exception as exc:
        await session.rollback()
        log_event("maintenance_failed", job=job, exc_type=type(exc).__name__)
        return False
    finally:
        await session.close()
    log_event("maintenance_done", job=job, run_date=run_date.isoformat(), **asdict(report))
    return True


async def tick(now: datetime) -> list[str]:
    run_date = _utc_date(now)
    connection = await get_engine().connect()
    locked = False
    try:
        locked = bool(await connection.scalar(_TRY_LOCK, {"key": LOCK_KEY}))
        # Session-level lock: it outlives this transaction and stays until the unlock below.
        await connection.commit()
        if not locked:
            return []
        return [job for job in JOBS if await _run_once(job, now, run_date)]
    finally:
        try:
            if locked:
                await connection.execute(_UNLOCK, {"key": LOCK_KEY})
                await connection.commit()
        except Exception:
            # Never return a connection that may still hold the lock to the pool.
            await connection.invalidate()
            raise
        finally:
            await connection.close()


async def _loop(interval_seconds: int) -> None:
    while True:
        try:
            await tick(datetime.now(UTC))
        except Exception as exc:
            log_event("maintenance_tick_failed", exc_type=type(exc).__name__)
        await asyncio.sleep(interval_seconds)


def start_scheduler(app: FastAPI) -> asyncio.Task[None]:
    """Start the maintenance loop; the first tick runs right away, then every interval."""
    interval = get_settings().maintenance_interval_seconds
    return asyncio.create_task(_loop(interval), name="atlas-maintenance")
