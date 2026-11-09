"""Conversation repository (CT-20). Skeleton stub: implemented by TASK-028.

Every read is filtered by `customer_id`. A missing conversation and another customer's
conversation raise the same `AtlasError(CONVERSATION_NOT_FOUND, 404)` (CHAT-94).
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from api.chat.models import Conversation, Message


class ConversationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_owned(self, conversation_id: UUID, customer_id: UUID) -> Conversation:
        raise NotImplementedError("TASK-028")

    async def create(self, customer_id: UUID) -> Conversation:
        raise NotImplementedError("TASK-028")

    async def append_message(
        self,
        conversation_id: UUID,
        role: str,
        content: str,
        status: str,
        language: str | None,
        trace_id: str,
    ) -> Message:
        raise NotImplementedError("TASK-028")

    async def list_messages(self, conversation_id: UUID) -> list[Message]:
        raise NotImplementedError("TASK-028")

    async def save_summary(self, conversation_id: UUID, summary: str, upto_message_id: int) -> None:
        raise NotImplementedError("TASK-028")

    async def delete_for_customers(self, customer_ids: list[UUID]) -> int:
        raise NotImplementedError("TASK-028")
