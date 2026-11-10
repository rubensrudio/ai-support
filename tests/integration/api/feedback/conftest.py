"""Fixtures shared by the feedback integration tests (TASK-135, TASK-136)."""

import uuid
from collections.abc import Awaitable, Callable

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.models import Customer
from api.chat.models import Conversation, Message


@pytest.fixture
def make_customer(db_session: AsyncSession) -> Callable[[], Awaitable[Customer]]:
    async def _make() -> Customer:
        suffix = uuid.uuid4().hex[:12]
        customer = Customer(
            username=f"feedback-{suffix}",
            display_name=f"Feedback {suffix}",
            email=f"feedback-{suffix}@example.test",
            password_hash="not-a-real-hash",  # noqa: S106 - fixture value, never verified
            language="en",
        )
        db_session.add(customer)
        await db_session.flush()
        return customer

    return _make


@pytest.fixture
def make_message(db_session: AsyncSession) -> Callable[..., Awaitable[Message]]:
    """A message in a new conversation of `customer`, with a fresh 32-hex trace id."""

    async def _make(customer: Customer, role: str = "assistant") -> Message:
        conversation = Conversation(customer_id=customer.id)
        db_session.add(conversation)
        await db_session.flush()
        message = Message(
            conversation_id=conversation.id,
            role=role,
            content="Your order ships tomorrow.",
            status="complete",
            language="en",
            trace_id=uuid.uuid4().hex,
        )
        db_session.add(message)
        await db_session.flush()
        return message

    return _make
