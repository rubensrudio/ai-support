"""Public demo usage and maintenance ORM models (CT-66, DEMO-02, DEMO-03, migration 0011).

- `LlmCall(id, trace_id, provider, model, cost_usd NUMERIC(12,6), created_at)`, table
  `llm_calls`, index `ix_llm_calls_created_at`: one row per LLM call (chat, summary, triage,
  topic judge) with its estimated cost; the daily spend cap sums it (DEMO-03).
- `RateLimitEvent(id, customer_id FK customers.id ON DELETE CASCADE, created_at)`, table
  `rate_limit_events`, index `ix_rate_limit_events_customer_created` on
  `(customer_id, created_at)`: one row per accepted persona message (DEMO-02).
- `MaintenanceRun(id, job, run_date DATE, finished_at)`, table `maintenance_runs`,
  `UNIQUE(job, run_date)` (`uq_maintenance_runs_job_run_date`): one row per daily job run, so
  several instances run each job once per UTC day (DA-26).

Migration `0011_usage_and_maintenance` (down_revision `0010`) also turns
`tickets.conversation_id` into a nullable FK with `ON DELETE SET NULL`, so the daily restore
(which deletes the personas' conversations) keeps the tickets; only the 7-day purge removes
them (DEMO-12, TASK-146).
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from api.db.engine import Base


class LlmCall(Base):
    __tablename__ = "llm_calls"
    __table_args__ = (Index("ix_llm_calls_created_at", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RateLimitEvent(Base):
    __tablename__ = "rate_limit_events"
    __table_args__ = (Index("ix_rate_limit_events_customer_created", "customer_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class MaintenanceRun(Base):
    __tablename__ = "maintenance_runs"
    __table_args__ = (UniqueConstraint("job", "run_date", name="uq_maintenance_runs_job_run_date"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job: Mapped[str] = mapped_column(Text, nullable=False)
    run_date: Mapped[date] = mapped_column(Date, nullable=False)
    finished_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
