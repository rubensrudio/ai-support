"""Seed command (CT-23): `python -m api.seed [--commerce] [--reset]`.

Creates or updates the fixed test personas with `ATLAS_PERSONA_PASSWORD`. `--reset` deletes
the personas (and, by cascade, their sessions and conversations) before recreating them.
`--commerce` is accepted now; orders and subscriptions arrive in phase 06.
"""

import argparse
import asyncio
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import delete

from api.auth.models import Customer
from api.config import get_settings
from api.db.engine import dispose_engine, session_scope
from api.seed.personas import PERSONAS, seed_personas


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m api.seed", description=__doc__)
    parser.add_argument("--commerce", action="store_true", help="also seed orders (phase 06)")
    parser.add_argument("--reset", action="store_true", help="delete and recreate the personas")
    return parser.parse_args(argv)


async def run(*, reset: bool = False, commerce: bool = False) -> list[UUID]:
    try:
        async with session_scope() as session:
            if reset:
                usernames = [persona.username for persona in PERSONAS]
                await session.execute(delete(Customer).where(Customer.username.in_(usernames)))
            ids = await seed_personas(session, get_settings().persona_password)
    finally:
        await dispose_engine()
    names = ", ".join(persona.username for persona in PERSONAS)
    print(f"Seeded {len(ids)} personas: {names}")
    if commerce:
        print("Commerce seed: not available until phase 06")
    return ids


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    asyncio.run(run(reset=args.reset, commerce=args.commerce))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
