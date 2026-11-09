"""Default registry (CT-43, TOOL-02, TOOL-12, LAC-03): exactly the seven tools, with the
descriptions the model sees; `destructive=True` only for `cancel_order` and `issue_refund`."""

from api.messages import MessageKey
from api.tools.email import SendEmailArgs, send_email
from api.tools.orders import (
    CancelOrderArgs,
    GetOrderArgs,
    ListOrdersArgs,
    cancel_order,
    get_order,
    list_orders,
    precheck_cancel_order,
)
from api.tools.refunds import (
    CheckRefundArgs,
    IssueRefundArgs,
    check_refund_policy,
    issue_refund,
    precheck_issue_refund,
)
from api.tools.registry import ToolDefinition, ToolRegistry
from api.tools.subscriptions import EmptyArgs, get_subscription

TOOL_NAMES = (
    "list_orders",
    "get_order",
    "cancel_order",
    "get_subscription",
    "check_refund_policy",
    "issue_refund",
    "send_email",
)
DESTRUCTIVE_TOOLS = frozenset({"cancel_order", "issue_refund"})

_CONFIRMATION_NOTE = (
    " This action needs the customer's explicit confirmation: the system asks for it before "
    "running the tool, so call it directly instead of asking in your reply."
)


def _definitions() -> tuple[ToolDefinition, ...]:
    return (
        ToolDefinition(
            name="list_orders",
            description=(
                "List the current customer's most recent orders, newest first, with id (order "
                "number), status, total in USD cents, items and dates. Read-only."
            ),
            args_model=ListOrdersArgs,
            destructive=False,
            handler=list_orders,
            label_key=MessageKey.STEP_LIST_ORDERS.value,
        ),
        ToolDefinition(
            name="get_order",
            description=(
                "Get one of the current customer's orders by its order number: status "
                "(pending, paid, shipped, delivered, cancelled), total in USD cents, items and "
                "dates. Returns ORDER_NOT_FOUND when the customer has no such order. Read-only."
            ),
            args_model=GetOrderArgs,
            destructive=False,
            handler=get_order,
            label_key=MessageKey.STEP_GET_ORDER.value,
        ),
        ToolDefinition(
            name="cancel_order",
            description=(
                "Cancel one of the current customer's orders by its order number. Only pending "
                "or paid orders can be cancelled; otherwise it returns ORDER_NOT_CANCELLABLE."
                + _CONFIRMATION_NOTE
            ),
            args_model=CancelOrderArgs,
            destructive=True,
            handler=cancel_order,
            label_key=MessageKey.STEP_CANCEL_ORDER.value,
            precheck=precheck_cancel_order,
        ),
        ToolDefinition(
            name="get_subscription",
            description=(
                "Get the current customer's subscription (plan, status, price in USD cents and "
                "renewal date). Returns SUBSCRIPTION_NOT_FOUND when there is none. Read-only."
            ),
            args_model=EmptyArgs,
            destructive=False,
            handler=get_subscription,
            label_key=MessageKey.STEP_GET_SUBSCRIPTION.value,
        ),
        ToolDefinition(
            name="check_refund_policy",
            description=(
                "Check whether one of the current customer's orders is eligible for a refund "
                "and why. An order is eligible only if it was cancelled before shipping or was "
                "delivered at most 7 days ago; the refund is the full order total. Read-only."
            ),
            args_model=CheckRefundArgs,
            destructive=False,
            handler=check_refund_policy,
            label_key=MessageKey.STEP_CHECK_REFUND_POLICY.value,
        ),
        ToolDefinition(
            name="issue_refund",
            description=(
                "Issue a full refund for one of the current customer's orders. The server "
                "re-checks eligibility and returns REFUND_NOT_ELIGIBLE or ALREADY_REFUNDED "
                "when it does not apply." + _CONFIRMATION_NOTE
            ),
            args_model=IssueRefundArgs,
            destructive=True,
            handler=issue_refund,
            label_key=MessageKey.STEP_ISSUE_REFUND.value,
            precheck=precheck_issue_refund,
        ),
        ToolDefinition(
            name="send_email",
            description=(
                "Send an email with a subject and a body to the current customer. The recipient "
                "is always the customer's own address on file and cannot be chosen."
            ),
            args_model=SendEmailArgs,
            destructive=False,
            handler=send_email,
            label_key=MessageKey.STEP_SEND_EMAIL.value,
        ),
    )


def build_default_registry() -> ToolRegistry:
    """A new registry with the seven tools, in `TOOL_NAMES` order."""
    registry = ToolRegistry()
    for definition in _definitions():
        registry.register(definition)
    return registry
