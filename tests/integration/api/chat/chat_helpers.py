"""Helpers shared by the chat integration tests: customers created in the test database."""

import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.deps import CurrentCustomer
from api.auth.models import Customer

CustomerFactory = Callable[..., Awaitable[Customer]]


def principal_of(customer: Customer) -> CurrentCustomer:
    language = "pt" if customer.language == "pt" else "en"
    return CurrentCustomer(
        id=customer.id,
        email=customer.email,
        display_name=customer.display_name,
        language=language,
    )


async def create_customer(session: AsyncSession, language: str = "en") -> Customer:
    suffix = uuid.uuid4().hex[:12]
    customer = Customer(
        username=f"persona-{suffix}",
        display_name=f"Persona {suffix}",
        email=f"persona-{suffix}@example.test",
        password_hash="not-a-real-hash",  # noqa: S106 - fixture value, never verified
        language=language,
    )
    session.add(customer)
    await session.flush()
    return customer
