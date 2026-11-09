"""HTTP schemas for `POST /chat` (CT-14). Final shape from phase 01 on; unused fields stay empty."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from api.agent.events import ChatStatus, ConfirmationPrompt
from api.messages import Lang


class ChatRequest(BaseModel):
    """Length and blank checks are business rules in `ChatService` (CHAT-90/91), not here,
    so they map to `VALIDATION_ERROR` / `MESSAGE_TOO_LONG` with the catalog messages."""

    model_config = ConfigDict(extra="forbid")

    message: str
    conversation_id: UUID | None = None
    ui_language: Lang | None = None


class Citation(BaseModel):
    index: int
    document: str
    section: str | None = None
    page: int | None = None


class ChatResponse(BaseModel):
    conversation_id: UUID | None = None
    message_id: int | None = None
    status: ChatStatus
    content: str
    language: Lang
    citations: list[Citation] = Field(default_factory=list)
    confirmation: ConfirmationPrompt | None = None
    trace_id: str
