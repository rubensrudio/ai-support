"""Commerce tables for the tools: orders, subscriptions, refunds, audit log, email outbox.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_USD = "currency = 'USD'"


def _customer_fk() -> sa.Column:  # type: ignore[type-arg]
    return sa.Column(
        "customer_id",
        UUID(as_uuid=True),
        sa.ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False,
    )


def _created_at() -> sa.Column:  # type: ignore[type-arg]
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "orders",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        _customer_fk(),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("total_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("items", JSONB(), nullable=False),
        _created_at(),
        sa.Column("shipped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'paid', 'shipped', 'delivered', 'cancelled')",
            name="ck_orders_status",
        ),
        sa.CheckConstraint("total_cents > 0", name="ck_orders_total_positive"),
        sa.CheckConstraint(_USD, name="ck_orders_currency"),
    )
    op.create_index("ix_orders_customer_id", "orders", ["customer_id"])
    op.create_table(
        "subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "customer_id",
            UUID(as_uuid=True),
            sa.ForeignKey("customers.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("plan_code", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("price_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("renews_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(_USD, name="ck_subscriptions_currency"),
    )
    op.create_table(
        "refunds",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "order_id", sa.Integer(), sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
        ),
        _customer_fk(),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        _created_at(),
        sa.Column("trace_id", sa.Text(), nullable=False),
        sa.UniqueConstraint("order_id", name="uq_refunds_order_id"),
        sa.CheckConstraint(_USD, name="ck_refunds_currency"),
        sa.CheckConstraint("amount_cents > 0", name="ck_refunds_amount_positive"),
    )
    op.create_index("ix_refunds_customer_id", "refunds", ["customer_id"])
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        _customer_fk(),
        sa.Column("tool", sa.Text(), nullable=False),
        sa.Column("arguments", JSONB(), nullable=False),
        sa.Column("result", JSONB(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("trace_id", sa.Text(), nullable=False),
        _created_at(),
        sa.CheckConstraint(
            "outcome IN ('success', 'error', 'refused')", name="ck_audit_log_outcome"
        ),
    )
    op.create_index("ix_audit_log_trace_id", "audit_log", ["trace_id"])
    op.create_index("ix_audit_log_created_at", "audit_log", ["created_at"])
    op.create_index("ix_audit_log_customer_id", "audit_log", ["customer_id"])
    op.create_table(
        "email_outbox",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        _customer_fk(),
        sa.Column("recipient", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("trace_id", sa.Text(), nullable=False),
        _created_at(),
    )
    op.create_index("ix_email_outbox_created_at", "email_outbox", ["created_at"])
    op.create_index("ix_email_outbox_customer_id", "email_outbox", ["customer_id"])


def downgrade() -> None:
    op.drop_table("email_outbox")
    op.drop_table("audit_log")
    op.drop_table("refunds")
    op.drop_table("subscriptions")
    op.drop_table("orders")
