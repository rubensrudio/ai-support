"""Refund eligibility (CT-41, TOOL-16, TOOL-98, LAC-06). Pure function, no database.

Eligible only if the order was cancelled before shipping (`cancelled`, `shipped_at` null) or
delivered no more than 7 days ago (`now - delivered_at <= 7 days`, inclusive). The amount is
always the full order total. `already_refunded` is decided by the caller (it needs the database).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from api.tools.models import Order, OrderStatus

REFUND_WINDOW = timedelta(days=7)
ReasonCode = Literal[
    "cancelled_before_shipment",
    "delivered_within_7_days",
    "delivered_over_7_days",
    "not_delivered_yet",
    "already_refunded",
]


@dataclass(frozen=True)
class RefundEligibility:
    eligible: bool
    reason_code: ReasonCode
    amount_cents: int


def evaluate_refund(order: Order, now: datetime) -> RefundEligibility:
    """Decide eligibility from the order alone; the amount is always the full total."""
    amount = order.total_cents
    if order.status == OrderStatus.CANCELLED and order.shipped_at is None:
        return RefundEligibility(True, "cancelled_before_shipment", amount)
    if order.status == OrderStatus.DELIVERED and order.delivered_at is not None:
        if now - order.delivered_at <= REFUND_WINDOW:
            return RefundEligibility(True, "delivered_within_7_days", amount)
        return RefundEligibility(False, "delivered_over_7_days", amount)
    return RefundEligibility(False, "not_delivered_yet", amount)
