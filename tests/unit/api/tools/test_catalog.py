"""Default registry (TASK-073, TOOL-02, TOOL-12, LAC-03)."""

import pytest

from api.messages import MessageKey
from api.tools import email, orders, refunds, subscriptions
from api.tools.catalog import DESTRUCTIVE_TOOLS, TOOL_NAMES, build_default_registry


def test_registry_holds_exactly_the_seven_tools() -> None:
    registry = build_default_registry()

    assert set(registry.names()) == {
        "list_orders",
        "get_order",
        "cancel_order",
        "get_subscription",
        "check_refund_policy",
        "issue_refund",
        "send_email",
    }
    assert registry.names() == list(TOOL_NAMES)
    assert [spec.name for spec in registry.specs()] == list(TOOL_NAMES)


def test_only_cancel_order_and_issue_refund_are_destructive() -> None:
    registry = build_default_registry()

    destructive = {name for name in registry.names() if registry.is_destructive(name)}

    assert destructive == {"cancel_order", "issue_refund"}
    assert destructive == DESTRUCTIVE_TOOLS


def test_each_build_returns_an_independent_registry() -> None:
    assert build_default_registry() is not build_default_registry()


@pytest.mark.parametrize("name", TOOL_NAMES)
def test_every_tool_has_a_description_and_its_own_step_label(name: str) -> None:
    definition = build_default_registry().get(name)

    assert definition is not None
    assert len(definition.description.strip()) >= 20
    assert definition.label_key == MessageKey[f"STEP_{name.upper()}"].value


def test_handlers_and_args_models_are_the_tool_modules() -> None:
    registry = build_default_registry()
    expected = {
        "list_orders": (orders.list_orders, orders.ListOrdersArgs),
        "get_order": (orders.get_order, orders.GetOrderArgs),
        "cancel_order": (orders.cancel_order, orders.CancelOrderArgs),
        "get_subscription": (subscriptions.get_subscription, subscriptions.EmptyArgs),
        "check_refund_policy": (refunds.check_refund_policy, refunds.CheckRefundArgs),
        "issue_refund": (refunds.issue_refund, refunds.IssueRefundArgs),
        "send_email": (email.send_email, email.SendEmailArgs),
    }
    for name, (handler, args_model) in expected.items():
        definition = registry.get(name)
        assert definition is not None
        assert definition.handler is handler
        assert definition.args_model is args_model


def test_specs_expose_no_customer_identity_field() -> None:
    for spec in build_default_registry().specs():
        properties = spec.input_schema.get("properties", {})
        assert not {"customer_id", "user_id", "email", "to", "recipient"} & set(properties)


def test_destructive_tools_have_their_read_only_precheck() -> None:
    registry = build_default_registry()
    prechecks = {name: getattr(registry.get(name), "precheck", None) for name in TOOL_NAMES}

    assert prechecks == {
        "list_orders": None,
        "get_order": None,
        "cancel_order": orders.precheck_cancel_order,
        "get_subscription": None,
        "check_refund_policy": None,
        "issue_refund": refunds.precheck_issue_refund,
        "send_email": None,
    }
