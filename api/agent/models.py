"""Pending confirmations of destructive tools (CT-44, migration 0008, TOOL-05, TOOL-96).

A destructive tool call (`cancel_order`, `issue_refund`) suspends the agent loop: the loop
state (`agent_state`, JSON) and the call are stored here with status `pending` until the
customer confirms or refuses it, sends a new message (refused, TOOL-97) or it expires.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from api.db.engine import Base

CONFIRMATION_STATUSES = ("pending", "confirmed", "refused", "expired")


class PendingConfirmation(Base):
    __tablename__ = "pending_confirmations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'confirmed', 'refused', 'expired')",
            name="ck_pending_confirmations_status",
        ),
        Index("ix_pending_confirmations_conversation_status", "conversation_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False
    )
    tool: Mapped[str] = mapped_column(Text, nullable=False)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    agent_state: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
