"""Seven-day retention purge, Langfuse traces included (TASK-147, CT-72, DEMO-10, DEMO-12)."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.models import Customer, CustomerSession
from api.chat.models import Conversation, Message
from api.config import Settings
from api.limits.models import LlmCall, MaintenanceRun, RateLimitEvent
from api.maintenance.purge import PurgeReport, purge_expired
from api.observability.langfuse_api import LangfuseApi
from api.rag.models import Chunk, Document
from api.seed.commerce import seed_commerce
from api.seed.personas import seed_personas
from api.tickets.models import Ticket
from api.tools.models import AuditLog, EmailOutbox, Order
from tests.integration.api.maintenance.conftest import make_conversation, make_customer

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
OLD = NOW - timedelta(days=8)
RECENT = NOW - timedelta(days=6)
PASSWORD = "persona-password-for-tests"


class FakeLangfuse:
    enabled = True

    def __init__(self, deleted: int = 3) -> None:
        self.cutoffs: list[datetime] = []
        self._deleted = deleted

    async def delete_traces_older_than(self, cutoff: datetime) -> int:
        self.cutoffs.append(cutoff)
        return self._deleted


async def _count(session: AsyncSession, model: type[object]) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def _add_rows(session: AsyncSession, customer: Customer, created_at: datetime) -> None:
    conversation = await make_conversation(session, customer.id, created_at)
    trace = uuid.uuid4().hex
    session.add_all(
        [
            Ticket(
                conversation_id=conversation.id,
                customer_id=customer.id,
                reason="all_providers_failed",
                trace_id=trace,
                created_at=created_at,
            ),
            AuditLog(
                customer_id=customer.id,
                tool="lookup_order",
                arguments={},
                result={},
                outcome="success",
                trace_id=trace,
                created_at=created_at,
            ),
            EmailOutbox(
                customer_id=customer.id,
                recipient=customer.email,
                subject="Subject",
                body="Body",
                trace_id=trace,
                created_at=created_at,
            ),
            LlmCall(
                trace_id=trace,
                provider="fake",
                model="fake-model",
                cost_usd=Decimal("0.001"),
                created_at=created_at,
            ),
            RateLimitEvent(customer_id=customer.id, created_at=created_at),
        ]
    )
    await session.flush()


async def test_purge_deletes_rows_older_than_retention_and_keeps_recent_ones(
    db_session: AsyncSession,
) -> None:
    persona_ids = await seed_personas(db_session, PASSWORD)
    await seed_commerce(db_session, NOW)
    customer = await make_customer(db_session)
    db_session.add(
        CustomerSession(
            customer_id=customer.id, token_hash="a" * 64, expires_at=NOW + timedelta(days=1)
        )
    )
    db_session.add(MaintenanceRun(job="restore", run_date=OLD.date(), finished_at=OLD))
    document = Document(
        doc_key="maintenance-old-doc",
        title="Old document",
        language="en",
        doc_type="faq",
        format="md",
        source_path="corpus/faq/old.md",
        content_hash="0" * 64,
        status="indexed",
        ingested_at=OLD,
    )
    db_session.add(document)
    await db_session.flush()
    db_session.add(
        Chunk(
            document_id=document.id,
            ordinal=0,
            content="Shipping takes three business days.",
            language="en",
            token_count=6,
            ts_config="english",
        )
    )
    await _add_rows(db_session, customer, OLD)
    await _add_rows(db_session, customer, RECENT)
    orders_before = await _count(db_session, Order)
    documents_before = await _count(db_session, Document)
    chunks_before = await _count(db_session, Chunk)
    langfuse = FakeLangfuse()

    report = await purge_expired(db_session, NOW, 7, cast(LangfuseApi, langfuse))
    db_session.expire_all()

    assert report == PurgeReport(
        conversations=1, audit_rows=1, emails=1, tickets=1, llm_calls=1, langfuse_traces=3
    )
    assert langfuse.cutoffs == [NOW - timedelta(days=7)]
    for model in (Conversation, Message, Ticket, AuditLog, EmailOutbox, LlmCall, RateLimitEvent):
        assert await _count(db_session, model) == 1, model.__name__
    for model in (Conversation, Ticket, AuditLog, EmailOutbox, LlmCall, RateLimitEvent):
        oldest = await db_session.scalar(select(func.min(model.created_at)))  # type: ignore[attr-defined]
        assert oldest == RECENT, model.__name__
    assert await _count(db_session, Order) == orders_before
    assert await _count(db_session, Document) == documents_before >= 1
    assert await _count(db_session, Chunk) == chunks_before >= 1
    assert await _count(db_session, CustomerSession) == 1
    assert await _count(db_session, MaintenanceRun) == 1
    customers = await db_session.scalar(select(func.count()).select_from(Customer))
    assert customers == len(persona_ids) + 1


async def test_purge_is_idempotent_and_skips_langfuse_when_absent(
    db_session: AsyncSession,
) -> None:
    customer = await make_customer(db_session)
    await _add_rows(db_session, customer, OLD)

    first = await purge_expired(db_session, NOW, 7, None)
    second = await purge_expired(db_session, NOW, 7, None)

    assert first.conversations == 1 and first.langfuse_traces == 0
    assert second == PurgeReport(
        conversations=0, audit_rows=0, emails=0, tickets=0, llm_calls=0, langfuse_traces=0
    )
    assert await _count(db_session, RateLimitEvent) == 0


async def test_purge_does_not_call_a_disabled_langfuse(db_session: AsyncSession) -> None:
    langfuse = FakeLangfuse()
    langfuse.enabled = False

    report = await purge_expired(db_session, NOW, 7, cast(LangfuseApi, langfuse))

    assert report.langfuse_traces == 0
    assert langfuse.cutoffs == []


async def test_purge_uses_the_retention_days_argument(db_session: AsyncSession) -> None:
    customer = await make_customer(db_session)
    await _add_rows(db_session, customer, RECENT)
    real = LangfuseApi(Settings())

    report = await purge_expired(db_session, NOW, 5, real)
    await real.aclose()

    assert report.conversations == 1 and report.tickets == 1
    assert report.langfuse_traces == 0
