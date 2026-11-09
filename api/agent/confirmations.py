"""Confirmation store (CT-44, TOOL-05, TOOL-13, TOOL-96, TOOL-97).

`resolve` is one `UPDATE ... WHERE id AND customer_id AND status = 'pending' AND created_at >
now - ttl RETURNING`, so a second confirmation, another customer's confirmation or an expired
one never executes (TOOL-96). Errors: missing or another customer's -> `AtlasError(
CONFIRMATION_NOT_FOUND, 404)` (same for both); not pending or expired -> `AtlasError(
CONFIRMATION_NOT_PENDING, 409)` (an expired pending row is marked `expired`). TTL =
`settings.confirmation_ttl_minutes`. This module never executes tools.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.agent.models import PendingConfirmation
from api.config import Settings
from api.errors import AtlasError, ErrorCode

Decision = Literal["confirm", "refuse"]

_STATUS_BY_DECISION: dict[Decision, str] = {"confirm": "confirmed", "refuse": "refused"}


class ConfirmationStore:
    """Persists pending confirmations; it flushes but never commits (the caller owns the
    transaction)."""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    @property
    def _ttl(self) -> timedelta:
        return timedelta(minutes=self.settings.confirmation_ttl_minutes)

    async def create(
        self,
        *,
        conversation_id: uuid.UUID,
        customer_id: uuid.UUID,
        tool: str,
        arguments: dict[str, Any],
        agent_state: dict[str, Any],
        trace_id: str,
    ) -> PendingConfirmation:
        row = PendingConfirmation(
            conversation_id=conversation_id,
            customer_id=customer_id,
            tool=tool,
            arguments=arguments,
            agent_state=agent_state,
            status="pending",
            trace_id=trace_id,
        )
        self.session.add(row)
        await self.session.flush()
        await self.session.refresh(row)
        return row

    async def resolve(
        self,
        confirmation_id: uuid.UUID,
        customer_id: uuid.UUID,
        decision: Decision,
        now: datetime | None = None,
    ) -> PendingConfirmation:
        current = now or datetime.now(UTC)
        statement = (
            update(PendingConfirmation)
            .where(
                PendingConfirmation.id == confirmation_id,
                PendingConfirmation.customer_id == customer_id,
                PendingConfirmation.status == "pending",
                PendingConfirmation.created_at > current - self._ttl,
            )
            .values(status=_STATUS_BY_DECISION[decision], resolved_at=current)
            .returning(PendingConfirmation)
            .execution_options(populate_existing=True, synchronize_session=False)
        )
        resolved = (await self.session.execute(statement)).scalars().one_or_none()
        if resolved is not None:
            return resolved

        # Nothing changed: find out why, scoped to the owner so another customer's id is
        # indistinguishable from a missing one.
        lookup = (
            select(PendingConfirmation)
            .where(
                PendingConfirmation.id == confirmation_id,
                PendingConfirmation.customer_id == customer_id,
            )
            .execution_options(populate_existing=True)
        )
        existing = (await self.session.execute(lookup)).scalars().one_or_none()
        if existing is None:
            raise AtlasError(ErrorCode.CONFIRMATION_NOT_FOUND, 404)
        if existing.status == "pending" and existing.created_at <= current - self._ttl:
            await self.session.execute(
                update(PendingConfirmation)
                .where(
                    PendingConfirmation.id == confirmation_id,
                    PendingConfirmation.status == "pending",
                )
                .values(status="expired", resolved_at=current)
                .execution_options(synchronize_session=False)
            )
            await self.session.flush()
        raise AtlasError(ErrorCode.CONFIRMATION_NOT_PENDING, 409)

    async def refuse_pending(self, conversation_id: uuid.UUID) -> list[PendingConfirmation]:
        statement = (
            update(PendingConfirmation)
            .where(
                PendingConfirmation.conversation_id == conversation_id,
                PendingConfirmation.status == "pending",
            )
            .values(status="refused", resolved_at=datetime.now(UTC))
            .returning(PendingConfirmation)
            .execution_options(populate_existing=True, synchronize_session=False)
        )
        return list((await self.session.execute(statement)).scalars().all())

    async def expire_stale(self, now: datetime) -> int:
        statement = (
            update(PendingConfirmation)
            .where(
                PendingConfirmation.status == "pending",
                PendingConfirmation.created_at <= now - self._ttl,
            )
            .values(status="expired", resolved_at=now)
            .returning(PendingConfirmation.id)
            .execution_options(synchronize_session=False)
        )
        return len((await self.session.execute(statement)).scalars().all())
