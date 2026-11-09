"""Default registry (CT-43, TOOL-02, TOOL-12, LAC-03): exactly the seven tools, with the
descriptions the model sees; `destructive=True` only for `cancel_order` and `issue_refund`."""

from api.tools.registry import ToolRegistry

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


def build_default_registry() -> ToolRegistry:
    raise NotImplementedError("TASK-073")
