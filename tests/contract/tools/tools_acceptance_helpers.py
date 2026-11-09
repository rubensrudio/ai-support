"""Shared helpers for the tools acceptance tests (Phase 6, AS-1).

Written from the contracts (spec TOOL-*, plan CT-38..CT-45 and the stub docstrings) only; the
handler implementations are deliberately not used as a source.
"""

import re
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.agent.events import ConfirmationPrompt
from api.agent.loop import AgentState
from api.llm.client import LLMResponse, ToolCall, Usage
from api.tools.models import AuditLog, EmailOutbox, Order, OrderStatus, Refund
from api.tools.registry import IDENTITY_FIELDS

DAY = timedelta(days=1)

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
ORDER_ID_KEYS = frozenset({"id", "order_id", "order_number", "number"})


def normalise_field(name: str) -> str:
    """`customerId`, `Customer-ID` and `customer_id` all compare as `customer_id`."""
    return _CAMEL_BOUNDARY.sub("_", name).replace("-", "_").lower()


def schema_property_names(node: Any) -> list[str]:
    """Every property name at any depth of a JSON schema (`$defs`, items, anyOf...)."""
    names: list[str] = []
    if isinstance(node, dict):
        properties = node.get("properties")
        if isinstance(properties, dict):
            names.extend(str(key) for key in properties)
        for value in node.values():
            names.extend(schema_property_names(value))
    elif isinstance(node, list):
        for item in node:
            names.extend(schema_property_names(item))
    return names


def identity_fields_in(schema: dict[str, Any]) -> list[str]:
    return [
        name for name in schema_property_names(schema) if normalise_field(name) in IDENTITY_FIELDS
    ]


def order_ids_in(content: Any) -> set[int]:
    """Values of order-number keys anywhere in a tool result `content`."""
    found: set[int] = set()
    if isinstance(content, dict):
        for key, value in content.items():
            if key in ORDER_ID_KEYS and isinstance(value, int) and not isinstance(value, bool):
                found.add(value)
            else:
                found |= order_ids_in(value)
    elif isinstance(content, list):
        for item in content:
            found |= order_ids_in(item)
    return found


def build_order(
    customer_id: uuid.UUID,
    status: OrderStatus,
    now: datetime,
    *,
    total_cents: int = 4_250,
    delivered_ago: timedelta = 3 * DAY,
) -> Order:
    """An order whose timestamps are consistent with `status`, relative to `now`."""
    shipped_at: datetime | None = None
    delivered_at: datetime | None = None
    cancelled_at: datetime | None = None
    if status is OrderStatus.SHIPPED:
        shipped_at = now - 2 * DAY
    elif status is OrderStatus.DELIVERED:
        delivered_at = now - delivered_ago
        shipped_at = delivered_at - 2 * DAY
    elif status is OrderStatus.CANCELLED:
        cancelled_at = now - DAY  # cancelled before shipment (shipped_at stays null)
    return Order(
        customer_id=customer_id,
        status=status.value,
        total_cents=total_cents,
        currency="USD",
        items=[
            {"sku": "ACC-TEST-1", "name": "Acceptance test item", "quantity": 2, "unit_cents": 0}
        ],
        created_at=now - 30 * DAY,
        shipped_at=shipped_at,
        delivered_at=delivered_at,
        cancelled_at=cancelled_at,
    )


async def order_state(session: AsyncSession, order_id: int) -> tuple[str, datetime | None]:
    """Fresh `(status, cancelled_at)` from the database (bypasses the identity map)."""
    row = (
        await session.execute(select(Order.status, Order.cancelled_at).where(Order.id == order_id))
    ).one()
    return row[0], row[1]


async def refund_count(session: AsyncSession, order_id: int) -> int:
    statement = select(func.count()).select_from(Refund).where(Refund.order_id == order_id)
    return int((await session.execute(statement)).scalar_one())


async def audit_count(session: AsyncSession, trace_id: str, outcome: str | None = None) -> int:
    statement = select(func.count()).select_from(AuditLog).where(AuditLog.trace_id == trace_id)
    if outcome is not None:
        statement = statement.where(AuditLog.outcome == outcome)
    return int((await session.execute(statement)).scalar_one())


async def outbox_rows(session: AsyncSession, customer_id: uuid.UUID) -> list[EmailOutbox]:
    statement = select(EmailOutbox).where(EmailOutbox.customer_id == customer_id)
    return list((await session.execute(statement)).scalars())


async def missing_order_id(session: AsyncSession) -> int:
    """An order number that does not exist for anyone."""
    highest = (await session.execute(select(func.max(Order.id)))).scalar_one()
    return int(highest or 0) + 100_000


def new_trace() -> str:
    return f"acc-{uuid.uuid4().hex}"


def tool_call_response(*calls: ToolCall) -> LLMResponse:
    return LLMResponse(
        text="",
        tool_calls=list(calls),
        usage=Usage(input_tokens=10, output_tokens=5),
        model="fake-model",
        provider="fake",
        stop_reason="tool_use",
    )


def text_response(text: str) -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=[],
        usage=Usage(input_tokens=10, output_tokens=5),
        model="fake-model",
        provider="fake",
        stop_reason="end_turn",
    )


class RecordingSuspend:
    """`SuspendFn` that records every suspension and returns a confirmation prompt."""

    def __init__(self) -> None:
        self.calls: list[tuple[ToolCall, AgentState]] = []
        self.prompts: list[ConfirmationPrompt] = []

    async def __call__(self, call: ToolCall, state: AgentState) -> ConfirmationPrompt:
        self.calls.append((call, state))
        prompt = ConfirmationPrompt(
            id=uuid.uuid4(), tool=call.name, target=f"#{call.arguments.get('order_id', '')}"
        )
        self.prompts.append(prompt)
        return prompt
