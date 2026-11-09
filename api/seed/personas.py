"""Fixed test personas (CT-23). Skeleton: `PERSONAS` is final; `seed_personas` is TASK-027.

All persona data is fictitious (`@example.com`). The password comes from
`ATLAS_PERSONA_PASSWORD`.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

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
    raise NotImplementedError("TASK-027")
