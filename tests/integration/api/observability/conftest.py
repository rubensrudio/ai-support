"""Fixtures of the request span tests: in-memory span exporter, customer, order and a chunk."""

import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.auth.models import Customer
from api.db.engine import get_sessionmaker
from api.observability.tracing import add_span_exporter
from api.rag.models import TS_CONFIGS, Chunk, Document
from api.tools.models import Order, OrderStatus

DOC_PREFIX = "request-spans-test-"


@pytest.fixture
def exporter() -> Iterator[InMemorySpanExporter]:
    memory = InMemorySpanExporter()
    detach = add_span_exporter(memory)
    try:
        yield memory
    finally:
        detach()


@pytest.fixture
async def customer(db_session: AsyncSession) -> Customer:
    suffix = uuid.uuid4().hex[:12]
    row = Customer(
        username=f"spans-{suffix}",
        display_name=f"Spans {suffix}",
        email=f"spans-{suffix}@example.test",
        password_hash="not-a-real-hash",  # noqa: S106 - fixture value, never verified
        language="en",
    )
    db_session.add(row)
    await db_session.flush()
    return row


@pytest.fixture
async def order(db_session: AsyncSession, customer: Customer) -> Order:
    row = Order(
        customer_id=customer.id,
        status=OrderStatus.SHIPPED.value,
        total_cents=4990,
        currency="USD",
        items=[{"sku": "box-coffee", "name": "Coffee box", "qty": 1}],
    )
    db_session.add(row)
    await db_session.flush()
    return row


@pytest.fixture
async def session_factory(
    migrated_database: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    factory = get_sessionmaker()
    yield factory
    async with factory() as session:
        await session.execute(delete(Document).where(Document.doc_key.startswith(DOC_PREFIX)))
        await session.commit()


@pytest.fixture
async def chunk_id(session_factory: async_sessionmaker[AsyncSession]) -> int:
    """One indexed chunk (committed: the retriever opens its own sessions)."""
    async with session_factory() as session:
        document = Document(
            doc_key=f"{DOC_PREFIX}{uuid.uuid4().hex[:8]}",
            title="Shipping guide",
            language="en",
            doc_type="manual",
            format="pdf",
            source_path="corpus/pdf/shipping.pdf",
            content_hash="h",
            status="indexed",
        )
        session.add(document)
        await session.flush()
        content = "Shipped orders arrive in three to five business days."
        row = Chunk(
            document_id=document.id,
            ordinal=0,
            content=content,
            language="en",
            section="Delivery",
            page=1,
            token_count=len(content.split()),
            ts_config=TS_CONFIGS["en"],
        )
        session.add(row)
        await session.commit()
        return row.id
