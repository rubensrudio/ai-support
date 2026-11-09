import uuid
from datetime import UTC, datetime, timedelta

import pytest

from api.tools.models import Order, OrderStatus
from api.tools.refund_policy import REFUND_WINDOW, evaluate_refund

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


def _order(status: OrderStatus, **overrides: object) -> Order:
    values: dict[str, object] = {
        "id": 101,
        "customer_id": uuid.uuid4(),
        "status": status,
        "total_cents": 4990,
        "currency": "USD",
        "items": [{"sku": "box-coffee", "qty": 1}],
        "created_at": NOW - timedelta(days=20),
    }
    values.update(overrides)
    return Order(**values)


def test_refund_window_is_seven_days() -> None:
    assert REFUND_WINDOW == timedelta(days=7)


def test_delivered_exactly_seven_days_ago_is_eligible() -> None:
    order = _order(
        OrderStatus.DELIVERED,
        shipped_at=NOW - timedelta(days=9),
        delivered_at=NOW - timedelta(hours=7 * 24),
    )

    result = evaluate_refund(order, NOW)

    assert result.eligible is True
    assert result.reason_code == "delivered_within_7_days"


def test_delivered_seven_days_and_one_second_ago_is_not_eligible() -> None:
    order = _order(
        OrderStatus.DELIVERED,
        shipped_at=NOW - timedelta(days=9),
        delivered_at=NOW - timedelta(days=7, seconds=1),
    )

    result = evaluate_refund(order, NOW)

    assert result.eligible is False
    assert result.reason_code == "delivered_over_7_days"


def test_delivered_today_is_eligible() -> None:
    order = _order(OrderStatus.DELIVERED, delivered_at=NOW - timedelta(hours=1))

    assert evaluate_refund(order, NOW).eligible is True


def test_cancelled_before_shipment_is_eligible() -> None:
    order = _order(OrderStatus.CANCELLED, cancelled_at=NOW - timedelta(days=30))

    result = evaluate_refund(order, NOW)

    assert result.eligible is True
    assert result.reason_code == "cancelled_before_shipment"


def test_cancelled_after_shipment_is_not_eligible() -> None:
    order = _order(
        OrderStatus.CANCELLED,
        shipped_at=NOW - timedelta(days=3),
        cancelled_at=NOW - timedelta(days=1),
    )

    result = evaluate_refund(order, NOW)

    assert result.eligible is False
    assert result.reason_code == "not_delivered_yet"


@pytest.mark.parametrize(
    ("status", "shipped_at"),
    [
        (OrderStatus.PENDING, None),
        (OrderStatus.PAID, None),
        (OrderStatus.SHIPPED, NOW - timedelta(days=1)),
    ],
)
def test_orders_not_delivered_are_not_eligible(
    status: OrderStatus, shipped_at: datetime | None
) -> None:
    order = _order(status, shipped_at=shipped_at)

    result = evaluate_refund(order, NOW)

    assert result.eligible is False
    assert result.reason_code == "not_delivered_yet"


@pytest.mark.parametrize(
    "order",
    [
        _order(OrderStatus.DELIVERED, total_cents=12345, delivered_at=NOW - timedelta(days=1)),
        _order(OrderStatus.DELIVERED, total_cents=777, delivered_at=NOW - timedelta(days=30)),
        _order(OrderStatus.CANCELLED, total_cents=2500),
        _order(OrderStatus.PAID, total_cents=999),
    ],
)
def test_amount_is_always_the_full_order_total(order: Order) -> None:
    assert evaluate_refund(order, NOW).amount_cents == order.total_cents
