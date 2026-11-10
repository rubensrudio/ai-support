"""Feedback ORM model (CT-64, OBS-02, migration 0010).

`Feedback(id, message_id UNIQUE FK messages.id ON DELETE CASCADE, customer_id FK customers.id,
rating TEXT CHECK (rating IN ('up','down')), trace_id TEXT NOT NULL, created_at timestamptz
default now())`, table `feedback`, migration `0010_feedback` (down_revision `0009`).

One row per message: a second rating on the same message replaces the first (upsert on
`message_id`). `trace_id` is copied from the rated message so the score links to its trace.
"""

import uuid
from datetime import datetime
from typing import Literal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from api.db.engine import Base

Rating = Literal["up", "down"]
RATINGS: tuple[Rating, ...] = ("up", "down")


class Feedback(Base):
    __tablename__ = "feedback"
    __table_args__ = (
        UniqueConstraint("message_id", name="uq_feedback_message_id"),
        CheckConstraint("rating IN ('up', 'down')", name="ck_feedback_rating"),
        Index("ix_feedback_customer_id", "customer_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False
    )
    rating: Mapped[str] = mapped_column(Text, nullable=False)
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
