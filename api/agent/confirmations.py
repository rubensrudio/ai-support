"""Confirmation store (CT-44, TOOL-05, TOOL-13, TOOL-96, TOOL-97).

`resolve` is one `UPDATE ... WHERE id AND customer_id AND status = 'pending' AND created_at >
now - ttl RETURNING`, so a second confirmation, another customer's confirmation or an expired
one never executes (TOOL-96). Errors: missing or another customer's -> `AtlasError(
CONFIRMATION_NOT_FOUND, 404)` (same for both); not pending or expired -> `AtlasError(
CONFIRMATION_NOT_PENDING, 409)` (an expired pending row is marked `expired`). TTL =
`settings.confirmation_ttl_minutes`. This module never executes tools.
"""

import uuid
from datetime import datetime
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from api.agent.models import PendingConfirmation
from api.config import Settings

Decision = Literal["confirm", "refuse"]


class ConfirmationStore:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings

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
        raise NotImplementedError("TASK-075")

    async def resolve(
        self,
        confirmation_id: uuid.UUID,
        customer_id: uuid.UUID,
        decision: Decision,
        now: datetime | None = None,
    ) -> PendingConfirmation:
        raise NotImplementedError("TASK-075")

    async def refuse_pending(self, conversation_id: uuid.UUID) -> list[PendingConfirmation]:
        raise NotImplementedError("TASK-075")

    async def expire_stale(self, now: datetime) -> int:
        raise NotImplementedError("TASK-075")
