"""Model pricing and cost estimation (CT-7). Skeleton stub: implemented by TASK-007."""

from decimal import Decimal

from api.llm.client import Usage


class UnknownModelPricing(LookupError):
    """The model has no entry in the pricing table; cost is never silently zero."""


def estimate_cost(model: str, usage: Usage) -> Decimal:
    raise NotImplementedError("TASK-007")
