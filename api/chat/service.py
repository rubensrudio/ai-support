"""Chat service (CT-15). Skeleton stub: implemented by TASK-019."""

from collections.abc import AsyncIterator

from api.agent.events import AgentEvent
from api.auth.deps import CurrentCustomer
from api.chat.schemas import ChatRequest, ChatResponse
from api.config import Settings
from api.llm.client import LLMClient


class ChatService:
    def __init__(self, llm: LLMClient, settings: Settings) -> None:
        self.llm = llm
        self.settings = settings

    def handle_message(
        self, request: ChatRequest, principal: CurrentCustomer | None
    ) -> AsyncIterator[AgentEvent]:
        raise NotImplementedError("TASK-019")

    async def collect(self, events: AsyncIterator[AgentEvent]) -> ChatResponse:
        raise NotImplementedError("TASK-019")


def get_chat_service() -> ChatService:
    """FastAPI dependency; tests override it with `app.dependency_overrides`."""
    raise NotImplementedError("TASK-019")
