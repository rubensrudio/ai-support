"""Fixtures for the tools acceptance tests (Phase 6, AS-1).

Most tests run inside the rolled-back `db_session` of `tests/conftest.py`. The concurrency tests
need real parallel transactions, so `committed_customer` creates a throwaway customer with its own
committed rows and deletes it (cascading to orders, refunds and audit rows) at the end.
"""

import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tools_acceptance_helpers import DAY, build_order, new_trace

from api.auth.models import Customer
from api.db.engine import get_engine
from api.messages import Lang
from api.seed.personas import PERSONAS, seed_personas
from api.tools.catalog import build_default_registry
from api.tools.models import Order, OrderStatus, Subscription
from api.tools.registry import ToolContext, ToolRegistry

PERSONA_PASSWORD = "tools-acceptance-pass-9c1d"  # noqa: S105 - fictitious test credential


@dataclass(frozen=True)
class Persona:
    id: uuid.UUID
    username: str
    email: str
    language: str


@pytest.fixture
def now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


@pytest.fixture
async def personas(db_session: AsyncSession) -> dict[str, Persona]:
    """The seeded personas (alex/en, bia/pt) keyed by username, with no subscription yet."""
    await seed_personas(db_session, PERSONA_PASSWORD)
    usernames = [spec.username for spec in PERSONAS]
    customers = (
        await db_session.execute(select(Customer).where(Customer.username.in_(usernames)))
    ).scalars()
    result = {
        customer.username: Persona(
            id=customer.id,
            username=customer.username,
            email=customer.email,
            language=customer.language,
        )
        for customer in customers
    }
    ids = [persona.id for persona in result.values()]
    await db_session.execute(delete(Subscription).where(Subscription.customer_id.in_(ids)))
    await db_session.flush()
    return result


@pytest.fixture
def registry() -> ToolRegistry:
    return build_default_registry()


ContextFactory = Callable[..., ToolContext]


@pytest.fixture
def ctx_for(
    db_session: AsyncSession, personas: dict[str, Persona], now: datetime
) -> ContextFactory:
    """`ToolContext` of a persona, as the server builds it from the session."""

    def _ctx(username: str, trace_id: str | None = None) -> ToolContext:
        persona = personas[username]
        language: Lang = "pt" if persona.language == "pt" else "en"
        return ToolContext(
            customer_id=persona.id,
            customer_email=persona.email,
            trace_id=trace_id or new_trace(),
            session=db_session,
            language=language,
            now=now,
        )

    return _ctx


class OrderFactory(Protocol):
    async def __call__(
        self,
        username: str,
        status: OrderStatus,
        *,
        total_cents: int = ...,
        delivered_ago: timedelta = ...,
    ) -> Order: ...


@pytest.fixture
def make_order(
    db_session: AsyncSession, personas: dict[str, Persona], now: datetime
) -> OrderFactory:
    """Create (flush) an order for a persona: `await make_order('alex', OrderStatus.PAID)`."""

    async def _make(
        username: str,
        status: OrderStatus,
        *,
        total_cents: int = 4_250,
        delivered_ago: timedelta = 3 * DAY,
    ) -> Order:
        order = build_order(
            personas[username].id,
            status,
            now,
            total_cents=total_cents,
            delivered_ago=delivered_ago,
        )
        db_session.add(order)
        await db_session.flush()
        return order

    return _make


@dataclass(frozen=True)
class CommittedCustomer:
    id: uuid.UUID
    email: str
    sessions: async_sessionmaker[AsyncSession]


@pytest.fixture
async def committed_customer(migrated_database: str) -> AsyncIterator[CommittedCustomer]:
    """A throwaway committed customer for the concurrency tests; deleted afterwards."""
    sessions = async_sessionmaker(get_engine(), expire_on_commit=False)
    suffix = uuid.uuid4().hex[:12]
    email = f"tools-acc-{suffix}@example.com"
    async with sessions() as session:
        customer = Customer(
            username=f"tools-acc-{suffix}",
            display_name="Tools Acceptance",
            email=email,
            password_hash="not-a-real-hash",  # noqa: S106 - never used to log in
            language="en",
        )
        session.add(customer)
        await session.commit()
        customer_id = customer.id
    try:
        yield CommittedCustomer(id=customer_id, email=email, sessions=sessions)
    finally:
        async with sessions() as session:
            await session.execute(delete(Customer).where(Customer.id == customer_id))
            await session.commit()
