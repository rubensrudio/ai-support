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
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

JOBS: tuple[str, ...] = ("restore", "purge")


async def tick(now: datetime) -> list[str]:
    raise NotImplementedError("TASK-148")


def start_scheduler(app: FastAPI) -> asyncio.Task[None]:
    raise NotImplementedError("TASK-148")
