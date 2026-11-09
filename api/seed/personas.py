"""Fixed test personas (CT-23), upserted by username with `seed_personas` (TASK-027).

All persona data is fictitious (`@example.com`). The password comes from
`ATLAS_PERSONA_PASSWORD`.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.models import Customer
from api.auth.service import hash_password
from api.messages import Lang


@dataclass(frozen=True)
class PersonaSpec:
    username: str
    display_name: str
    email: str
    language: Lang


PERSONAS: tuple[PersonaSpec, ...] = (
    PersonaSpec("alex", "Alex Morgan", "alex@example.com", "en"),
    PersonaSpec("bia", "Bia Souza", "bia@example.com", "pt"),
)


async def seed_personas(session: AsyncSession, password: str) -> list[UUID]:
    """Idempotent upsert by username; returns the customer ids in `PERSONAS` order."""
    usernames = [persona.username for persona in PERSONAS]
    existing = {
        customer.username: customer
        for customer in await session.scalars(
            select(Customer).where(Customer.username.in_(usernames))
        )
    }
    ids: list[UUID] = []
    for persona in PERSONAS:
        customer = existing.get(persona.username)
        if customer is None:
            customer = Customer(username=persona.username)
            session.add(customer)
        customer.display_name = persona.display_name
        customer.email = persona.email
        customer.language = persona.language
        customer.password_hash = hash_password(password)
        await session.flush()
        ids.append(customer.id)
    return ids
