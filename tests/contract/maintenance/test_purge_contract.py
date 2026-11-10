"""Seven-day retention purge contract (CT-72, DEMO-10, DEMO-12, LAC-36, AS-5).

`purge_expired(session, now, retention_days, langfuse)` deletes what was created before
`now - retention_days` from conversations (and their messages, feedback and confirmations),
`audit_log`, `email_outbox`, `tickets` and `llm_calls`, and asks Langfuse to delete the traces
older than the same cutoff. Customers and the seed tables stay. A second run deletes nothing.
"""

from datetime import datetime, timedelta

import pytest
from maintenance_contract_helpers import ActivityFn, ExistingFn, FakeLangfuse, Persona, Rows
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.models import Customer
from api.config import get_settings
from api.maintenance.purge import PurgeReport, purge_expired
from api.tools.models import Order, Subscription

pytestmark = pytest.mark.integration

RETENTION_DAYS = 7


async def _count(session: AsyncSession, model: type[Order] | type[Subscription]) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def _old_and_recent(
    personas: dict[str, Persona], outsider: Persona, now: datetime, make_activity: ActivityFn
) -> tuple[list[Rows], list[Rows]]:
    old = [
        await make_activity(personas["alex"], now - timedelta(days=8)),
        await make_activity(outsider, now - timedelta(days=8)),
    ]
    recent = [
        await make_activity(personas["bia"], now - timedelta(days=6)),
        await make_activity(outsider, now - timedelta(days=6)),
    ]
    return old, recent


async def test_purge_deletes_rows_older_than_the_retention_and_keeps_recent_ones(
    db_session: AsyncSession,
    personas: dict[str, Persona],
    outsider: Persona,
    now: datetime,
    make_activity: ActivityFn,
    existing: ExistingFn,
) -> None:
    old, recent = await _old_and_recent(personas, outsider, now, make_activity)
    langfuse = FakeLangfuse(get_settings(), traces=4)

    report = await purge_expired(db_session, now, RETENTION_DAYS, langfuse)
    await db_session.flush()
    db_session.expire_all()

    assert isinstance(report, PurgeReport)
    for rows in old:
        assert await existing(rows) == Rows(), "rows older than 7 days must be gone"
    for rows in recent:
        assert await existing(rows) == rows, "rows younger than 7 days must stay"
    assert report.conversations == len(old)
    assert report.audit_rows == len(old)
    assert report.emails == len(old)
    assert report.tickets == len(old)
    assert report.llm_calls == len(old)
    assert report.langfuse_traces == 4


async def test_purge_calls_langfuse_with_the_same_cutoff(
    db_session: AsyncSession, now: datetime
) -> None:
    langfuse = FakeLangfuse(get_settings())

    await purge_expired(db_session, now, RETENTION_DAYS, langfuse)

    assert langfuse.cutoffs == [now - timedelta(days=RETENTION_DAYS)]


async def test_purge_keeps_customers_and_seed(
    db_session: AsyncSession,
    personas: dict[str, Persona],
    outsider: Persona,
    now: datetime,
    make_activity: ActivityFn,
) -> None:
    await _old_and_recent(personas, outsider, now, make_activity)
    orders = await _count(db_session, Order)
    subscriptions = await _count(db_session, Subscription)

    # Far in the future, everything the seed created is older than the cutoff.
    await purge_expired(db_session, now + timedelta(days=60), RETENTION_DAYS, None)
    await db_session.flush()
    db_session.expire_all()

    for customer in (*personas.values(), outsider):
        assert await db_session.get(Customer, customer.id) is not None, customer.username
    assert await _count(db_session, Order) == orders
    assert await _count(db_session, Subscription) == subscriptions


async def test_purge_is_idempotent(
    db_session: AsyncSession,
    personas: dict[str, Persona],
    outsider: Persona,
    now: datetime,
    make_activity: ActivityFn,
    existing: ExistingFn,
) -> None:
    _, recent = await _old_and_recent(personas, outsider, now, make_activity)
    langfuse = FakeLangfuse(get_settings())
    await purge_expired(db_session, now, RETENTION_DAYS, langfuse)
    await db_session.flush()

    second = await purge_expired(db_session, now, RETENTION_DAYS, langfuse)
    await db_session.flush()
    db_session.expire_all()

    assert (
        second.conversations,
        second.audit_rows,
        second.emails,
        second.tickets,
        second.llm_calls,
        second.langfuse_traces,
    ) == (0, 0, 0, 0, 0, 0)
    for rows in recent:
        assert await existing(rows) == rows


async def test_purge_without_langfuse_still_purges_the_database(
    db_session: AsyncSession,
    personas: dict[str, Persona],
    outsider: Persona,
    now: datetime,
    make_activity: ActivityFn,
    existing: ExistingFn,
) -> None:
    old, _ = await _old_and_recent(personas, outsider, now, make_activity)

    report = await purge_expired(db_session, now, RETENTION_DAYS, None)
    await db_session.flush()
    db_session.expire_all()

    assert report.langfuse_traces == 0
    for rows in old:
        assert await existing(rows) == Rows()
