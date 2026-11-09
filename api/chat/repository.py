"""Conversation repository (CT-20, TASK-028).

Every read is filtered by `customer_id`. A missing conversation and another customer's
conversation raise the same `AtlasError(CONVERSATION_NOT_FOUND, 404)` (CHAT-94), so the
response never reveals that a conversation exists.

Writes only `flush()`: the unit of work (`session_scope` / `get_db_session`) commits.
`append_message` is a single INSERT with no lock; the `bigserial` id gives the order (CHAT-93).
"""

from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.chat.models import Conversation, Message
from api.errors import AtlasError, ErrorCode


class ConversationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_owned(self, conversation_id: UUID, customer_id: UUID) -> Conversation:
        statement = select(Conversation).where(
            Conversation.id == conversation_id, Conversation.customer_id == customer_id
        )
        conversation = (await self.session.execute(statement)).scalar_one_or_none()
        if conversation is None:
            raise AtlasError(ErrorCode.CONVERSATION_NOT_FOUND, 404)
        return conversation

    async def create(self, customer_id: UUID) -> Conversation:
        conversation = Conversation(customer_id=customer_id)
        self.session.add(conversation)
        await self.session.flush()
        return conversation

    async def append_message(
        self,
        conversation_id: UUID,
        role: str,
        content: str,
        status: str,
        language: str | None,
        trace_id: str,
    ) -> Message:
        message = Message(
            conversation_id=conversation_id,
            role=role,
            content=content,
            status=status,
            language=language,
            trace_id=trace_id,
        )
        self.session.add(message)
        await self.session.flush()
        return message

    async def list_messages(self, conversation_id: UUID) -> list[Message]:
        statement = (
            select(Message).where(Message.conversation_id == conversation_id).order_by(Message.id)
        )
        return list((await self.session.execute(statement)).scalars().all())

    async def save_summary(self, conversation_id: UUID, summary: str, upto_message_id: int) -> None:
        statement = (
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(summary=summary, summary_upto_message_id=upto_message_id)
        )
        await self.session.execute(statement)
        await self.session.flush()

    async def delete_for_customers(self, customer_ids: list[UUID]) -> int:
        """Delete every conversation of `customer_ids` (messages cascade); returns how many."""
        if not customer_ids:
            return 0
        statement = (
            delete(Conversation)
            .where(Conversation.customer_id.in_(customer_ids))
            .returning(Conversation.id)
        )
        deleted = (await self.session.execute(statement)).scalars().all()
        await self.session.flush()
        return len(deleted)
