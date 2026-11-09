"""Seed command (CT-23): `python -m api.seed [--commerce] [--reset]`.

Creates or updates the fixed test personas with `ATLAS_PERSONA_PASSWORD`. `--reset` deletes
the personas (and, by cascade, their sessions and conversations) before recreating them.
`--commerce` then resets and recreates each persona's subscription, orders and refunds
(`seed_commerce`); conversations and the audit log are kept. Order numbers change on every
reset.
"""

import argparse
import asyncio
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import delete

from api.auth.models import Customer
from api.config import get_settings
from api.db.engine import dispose_engine, session_scope
from api.seed.commerce import CommerceSeedResult, seed_commerce
from api.seed.personas import PERSONAS, seed_personas


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m api.seed", description=__doc__)
    parser.add_argument(
        "--commerce", action="store_true", help="also reset the personas' orders and subscription"
    )
    parser.add_argument("--reset", action="store_true", help="delete and recreate the personas")
    return parser.parse_args(argv)


async def run(*, reset: bool = False, commerce: bool = False) -> list[UUID]:
    summary: CommerceSeedResult | None = None
    try:
        async with session_scope() as session:
            if reset:
                usernames = [persona.username for persona in PERSONAS]
                await session.execute(delete(Customer).where(Customer.username.in_(usernames)))
            ids = await seed_personas(session, get_settings().persona_password)
            if commerce:
                summary = await seed_commerce(session)
    finally:
        await dispose_engine()
    names = ", ".join(persona.username for persona in PERSONAS)
    print(f"Seeded {len(ids)} personas: {names}")
    if summary is not None:
        print(
            f"Commerce seed: {summary.customers} customers, {summary.orders} orders, "
            f"{summary.subscriptions} subscriptions"
        )
        if summary.skipped:
            print(f"Commerce seed skipped missing personas: {', '.join(summary.skipped)}")
    return ids


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    asyncio.run(run(reset=args.reset, commerce=args.commerce))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
