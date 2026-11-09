"""Identity comes only from the session and every tool is scoped to it (TOOL-03, TOOL-04, TOOL-10,
TOOL-18, TOOL-90, AS-1).

All calls go through the real default registry (`build_default_registry().execute`) with the
`ToolContext` the server builds for a persona, exactly as the agent loop does.
"""

from typing import Any

import pytest
from conftest import ContextFactory, OrderFactory, Persona
from sqlalchemy.ext.asyncio import AsyncSession
from tools_acceptance_helpers import (
    audit_count,
    identity_fields_in,
    missing_order_id,
    order_ids_in,
    order_state,
    outbox_rows,
    refund_count,
)

from api.llm.client import ToolCall
from api.tools.catalog import TOOL_NAMES
from api.tools.models import OrderStatus, Subscription
from api.tools.registry import ToolErrorCode, ToolRegistry

pytestmark = pytest.mark.integration

IDENTITY_KEYS = ("customer_id", "user_id", "email", "to", "recipient", "customerId")
ORDER_ID_TOOLS = ("get_order", "cancel_order", "check_refund_policy", "issue_refund")


# --- TOOL-03 / TOOL-18: no identity field in any schema -------------------------------------


def test_default_registry_exposes_exactly_the_seven_tools(registry: ToolRegistry) -> None:
    assert sorted(spec.name for spec in registry.specs()) == sorted(TOOL_NAMES)
    assert len(registry.specs()) == 7


@pytest.mark.parametrize("tool", TOOL_NAMES)
def test_no_tool_schema_has_an_identity_field_at_any_level(
    registry: ToolRegistry, tool: str
) -> None:
    spec = next(spec for spec in registry.specs() if spec.name == tool)
    assert identity_fields_in(spec.input_schema) == []


def test_send_email_schema_has_no_recipient_field(registry: ToolRegistry) -> None:
    spec = next(spec for spec in registry.specs() if spec.name == "send_email")
    properties = spec.input_schema.get("properties", {})
    assert set(properties) == {"subject", "body"}


@pytest.mark.parametrize("tool", TOOL_NAMES)
async def test_identity_argument_is_rejected_without_any_effect(
    tool: str,
    registry: ToolRegistry,
    ctx_for: ContextFactory,
    make_order: OrderFactory,
    personas: dict[str, Persona],
    db_session: AsyncSession,
) -> None:
    paid = await make_order("alex", OrderStatus.PAID)
    delivered = await make_order("alex", OrderStatus.DELIVERED)  # refund-eligible
    before_paid = await order_state(db_session, paid.id)
    valid_args: dict[str, dict[str, Any]] = {
        "list_orders": {},
        "get_order": {"order_id": paid.id},
        "cancel_order": {"order_id": paid.id},
        "get_subscription": {},
        "check_refund_policy": {"order_id": delivered.id},
        "issue_refund": {"order_id": delivered.id},
        "send_email": {"subject": "Your order", "body": "Hello"},
    }
    other = personas["bia"]
    for key in IDENTITY_KEYS:
        ctx = ctx_for("alex")
        injected = str(other.id) if "id" in key.lower() else other.email
        arguments = {**valid_args[tool], key: injected}

        result = await registry.execute(ToolCall(id="c1", name=tool, arguments=arguments), ctx)

        assert result.ok is False, key
        assert result.error_code == ToolErrorCode.INVALID_ARGUMENTS, key
        assert await audit_count(db_session, ctx.trace_id) == 0, key

    assert await order_state(db_session, paid.id) == before_paid
    assert await refund_count(db_session, delivered.id) == 0
    assert await outbox_rows(db_session, personas["alex"].id) == []
    assert await outbox_rows(db_session, other.id) == []


# --- TOOL-04 / TOOL-90: another customer's order == a missing order -------------------------


def _target_status(tool: str) -> OrderStatus:
    # The other persona's order is chosen so that only the ownership check can stop the tool.
    if tool in ("issue_refund", "check_refund_policy"):
        return OrderStatus.DELIVERED
    return OrderStatus.PAID


@pytest.mark.parametrize("tool", ORDER_ID_TOOLS)
async def test_other_customers_order_is_indistinguishable_from_a_missing_one(
    tool: str,
    registry: ToolRegistry,
    ctx_for: ContextFactory,
    make_order: OrderFactory,
    db_session: AsyncSession,
) -> None:
    foreign = await make_order("bia", _target_status(tool))
    before = await order_state(db_session, foreign.id)
    missing = await missing_order_id(db_session)

    foreign_ctx = ctx_for("alex")
    missing_ctx = ctx_for("alex")
    foreign_result = await registry.execute(
        ToolCall(id="c1", name=tool, arguments={"order_id": foreign.id}), foreign_ctx
    )
    missing_result = await registry.execute(
        ToolCall(id="c2", name=tool, arguments={"order_id": missing}), missing_ctx
    )

    assert foreign_result.ok is False
    assert foreign_result.error_code == ToolErrorCode.ORDER_NOT_FOUND
    assert (foreign_result.ok, foreign_result.error_code) == (
        missing_result.ok,
        missing_result.error_code,
    )
    assert set(foreign_result.content) == set(missing_result.content)
    # Nothing of the owner leaks into the result.
    assert "bia@example.com" not in repr(foreign_result.content)

    assert await order_state(db_session, foreign.id) == before
    assert await refund_count(db_session, foreign.id) == 0
    assert await audit_count(db_session, foreign_ctx.trace_id, "success") == 0
    assert await audit_count(db_session, missing_ctx.trace_id, "success") == 0


async def test_list_orders_returns_only_the_session_customers_orders(
    registry: ToolRegistry,
    ctx_for: ContextFactory,
    make_order: OrderFactory,
    db_session: AsyncSession,
) -> None:
    own = [await make_order("alex", status) for status in OrderStatus]
    foreign = [await make_order("bia", status) for status in OrderStatus]
    ctx = ctx_for("alex")

    result = await registry.execute(
        ToolCall(id="c1", name="list_orders", arguments={"limit": 50}), ctx
    )

    assert result.ok is True
    listed = order_ids_in(result.content)
    assert {order.id for order in own} <= listed
    assert listed.isdisjoint({order.id for order in foreign})
    assert await audit_count(db_session, ctx.trace_id) == 0  # reads are not audited, no effect


async def test_get_subscription_returns_only_the_session_customers_subscription(
    registry: ToolRegistry,
    ctx_for: ContextFactory,
    personas: dict[str, Persona],
    db_session: AsyncSession,
) -> None:
    db_session.add_all(
        [
            Subscription(
                customer_id=personas["alex"].id,
                plan_code="acc-plan-alex",
                status="active",
                price_cents=3490,
                currency="USD",
            ),
            Subscription(
                customer_id=personas["bia"].id,
                plan_code="acc-plan-bia",
                status="active",
                price_cents=1990,
                currency="USD",
            ),
        ]
    )
    await db_session.flush()
    ctx = ctx_for("alex")

    result = await registry.execute(ToolCall(id="c1", name="get_subscription", arguments={}), ctx)

    assert result.ok is True
    assert "acc-plan-alex" in repr(result.content)
    assert "acc-plan-bia" not in repr(result.content)
    assert await audit_count(db_session, ctx.trace_id) == 0


async def test_get_subscription_without_one_is_not_found_and_never_falls_back(
    registry: ToolRegistry,
    ctx_for: ContextFactory,
    personas: dict[str, Persona],
    db_session: AsyncSession,
) -> None:
    db_session.add(
        Subscription(
            customer_id=personas["bia"].id,
            plan_code="acc-plan-bia",
            status="active",
            price_cents=1990,
            currency="USD",
        )
    )
    await db_session.flush()

    result = await registry.execute(
        ToolCall(id="c1", name="get_subscription", arguments={}), ctx_for("alex")
    )

    assert result.ok is False
    assert result.error_code == ToolErrorCode.SUBSCRIPTION_NOT_FOUND
    assert "acc-plan-bia" not in repr(result.content)
