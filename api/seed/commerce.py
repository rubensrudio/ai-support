"""Commerce seed per persona (TASK-066, TOOL-01, DEMO-06, TOOL-19, TOOL-98).

For each persona: one subscription and orders in every status, with dates relative to `now`:
a cancellable paid order (and a pending one), a shipped one, delivered 3, 7 (exactly 7×24h) and
10 days ago, and a cancelled one; USD cents. Idempotent: running it again resets the persona's
orders, refunds and subscription to the same initial state (base of the daily demo reset) and
never deletes conversations. Returns a summary of what was seeded.

Only the personas' commerce rows are touched: conversations, messages, the audit log and the
email outbox are kept, and other customers are never read or changed. Order numbers
(`orders.id`) come from the table sequence, so they change on every reset; nothing (tests,
evals, docs) should hard-code them. A persona missing from `customers` is skipped and listed in
`CommerceSeedResult.skipped`.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.models import Customer
from api.seed.personas import PERSONAS
from api.tools.models import CURRENCY, Order, OrderStatus, Refund, Subscription


@dataclass(frozen=True)
class CommerceSeedResult:
    customers: int
    orders: int
    subscriptions: int
    skipped: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Item:
    sku: str
    name: str
    unit_cents: int


_COFFEE = _Item("COF-ETH-250", "Ethiopia Yirgacheffe coffee beans 250 g", 1490)
_COFFEE_BR = _Item("COF-BRA-250", "Brazil Cerrado coffee beans 250 g", 1290)
_TEA = _Item("TEA-SEN-100", "Japanese sencha green tea 100 g", 990)
_CHAI = _Item("TEA-CHA-100", "Masala chai loose-leaf tea 100 g", 890)
_SNACK = _Item("SNK-ALM-150", "Roasted almond and honey bites 150 g", 590)
_COOKIE = _Item("SNK-COO-120", "Oat and dark chocolate cookies 120 g", 490)


@dataclass(frozen=True)
class _PlanSpec:
    code: str
    price_cents: int
    shipping_cents: int


_ESSENTIAL = _PlanSpec("essential", 1990, 690)  # corpus: plans-and-pricing.md
_PLUS = _PlanSpec("plus", 3490, 0)

# Persona username -> subscribed plan. Unknown personas get Essential.
_PERSONA_PLANS: dict[str, _PlanSpec] = {"alex": _PLUS, "bia": _ESSENTIAL}

# (status, created ago, shipped ago, delivered ago, cancelled ago, items with quantity)
_OrderSpec = tuple[
    OrderStatus,
    timedelta,
    timedelta | None,
    timedelta | None,
    timedelta | None,
    tuple[tuple[_Item, int], ...],
]
_D = timedelta(days=1)
_ORDERS: tuple[_OrderSpec, ...] = (
    (OrderStatus.DELIVERED, 18 * _D, 16 * _D, 10 * _D, None, ((_COFFEE, 1), (_SNACK, 2))),
    (OrderStatus.DELIVERED, 14 * _D, 11 * _D, 7 * _D, None, ((_TEA, 2), (_COOKIE, 1))),
    (OrderStatus.DELIVERED, 8 * _D, 6 * _D, 3 * _D, None, ((_COFFEE_BR, 1), (_CHAI, 1))),
    (OrderStatus.CANCELLED, 6 * _D, None, None, 5 * _D, ((_CHAI, 1), (_SNACK, 1))),
    (OrderStatus.SHIPPED, 4 * _D, 2 * _D, None, None, ((_COFFEE, 1), (_TEA, 1))),
    (OrderStatus.PAID, 1 * _D, None, None, None, ((_COFFEE_BR, 2), (_COOKIE, 1))),
    (OrderStatus.PENDING, timedelta(hours=2), None, None, None, ((_TEA, 1), (_SNACK, 1))),
)
_RENEWS_IN = 12 * _D


def _items(lines: tuple[tuple[_Item, int], ...]) -> list[dict[str, Any]]:
    return [
        {"sku": item.sku, "name": item.name, "quantity": qty, "unit_cents": item.unit_cents}
        for item, qty in lines
    ]


def _total(lines: tuple[tuple[_Item, int], ...], plan: _PlanSpec) -> int:
    return sum(item.unit_cents * qty for item, qty in lines) + plan.shipping_cents


def _ago(now: datetime, delta: timedelta | None) -> datetime | None:
    return None if delta is None else now - delta


async def seed_commerce(session: AsyncSession, now: datetime | None = None) -> CommerceSeedResult:
    """Reset and recreate the personas' subscription and orders (flushed, not committed)."""
    now = now or datetime.now(UTC)
    usernames = [persona.username for persona in PERSONAS]
    customers = {
        customer.username: customer
        for customer in await session.scalars(
            select(Customer).where(Customer.username.in_(usernames))
        )
    }
    ids = [customer.id for customer in customers.values()]
    if ids:
        await session.execute(delete(Refund).where(Refund.customer_id.in_(ids)))
        await session.execute(delete(Order).where(Order.customer_id.in_(ids)))
        await session.execute(delete(Subscription).where(Subscription.customer_id.in_(ids)))

    skipped: list[str] = []
    orders = 0
    for persona in PERSONAS:
        customer = customers.get(persona.username)
        if customer is None:
            skipped.append(persona.username)
            continue
        plan = _PERSONA_PLANS.get(persona.username, _ESSENTIAL)
        session.add(
            Subscription(
                customer_id=customer.id,
                plan_code=plan.code,
                status="active",
                price_cents=plan.price_cents,
                currency=CURRENCY,
                renews_at=now + _RENEWS_IN,
            )
        )
        for status, created, shipped, delivered, cancelled, lines in _ORDERS:
            session.add(
                Order(
                    customer_id=customer.id,
                    status=status.value,
                    total_cents=_total(lines, plan),
                    currency=CURRENCY,
                    items=_items(lines),
                    created_at=now - created,
                    shipped_at=_ago(now, shipped),
                    delivered_at=_ago(now, delivered),
                    cancelled_at=_ago(now, cancelled),
                )
            )
            orders += 1
    await session.flush()
    seeded = len(PERSONAS) - len(skipped)
    return CommerceSeedResult(
        customers=seeded, orders=orders, subscriptions=seeded, skipped=tuple(skipped)
    )
