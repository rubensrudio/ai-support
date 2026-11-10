"""LLM call ledger, rate limit events, maintenance runs; tickets survive conversation deletes.

CT-66, DEMO-02, DEMO-03, DEMO-12, DA-26.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TICKETS_FK = "tickets_conversation_id_fkey"


def upgrade() -> None:
    op.create_table(
        "llm_calls",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("trace_id", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_llm_calls_created_at", "llm_calls", ["created_at"])

    op.create_table(
        "rate_limit_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "customer_id",
            UUID(as_uuid=True),
            sa.ForeignKey("customers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_rate_limit_events_customer_created",
        "rate_limit_events",
        ["customer_id", "created_at"],
    )

    op.create_table(
        "maintenance_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("job", sa.Text(), nullable=False),
        sa.Column("run_date", sa.Date(), nullable=False),
        sa.Column(
            "finished_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("job", "run_date", name="uq_maintenance_runs_job_run_date"),
    )

    op.drop_constraint(TICKETS_FK, "tickets", type_="foreignkey")
    op.alter_column("tickets", "conversation_id", nullable=True)
    op.create_foreign_key(
        TICKETS_FK, "tickets", "conversations", ["conversation_id"], ["id"], ondelete="SET NULL"
    )


def downgrade() -> None:
    op.drop_constraint(TICKETS_FK, "tickets", type_="foreignkey")
    op.execute("DELETE FROM tickets WHERE conversation_id IS NULL")
    op.alter_column("tickets", "conversation_id", nullable=False)
    op.create_foreign_key(
        TICKETS_FK, "tickets", "conversations", ["conversation_id"], ["id"], ondelete="CASCADE"
    )
    op.drop_table("maintenance_runs")
    op.drop_table("rate_limit_events")
    op.drop_table("llm_calls")
