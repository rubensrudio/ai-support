"""`POST /chat` (CT-14, TASK-020, TASK-032) and `POST /chat/confirmations/{id}` (CT-46, TASK-078).

From phase 02 the routes require a Customer session (`require_customer`): without a valid
`atlas_session` cookie they answer 401 `AUTH_REQUIRED` before the service runs, so no LLM call
happens (TOOL-95). The identity comes only from the cookie; the bodies have no customer field.
A `conversation_id` of another Customer gets the same 404 as a missing one (CHAT-94).

`POST /chat/confirmations/{confirmation_id}` confirms or refuses a destructive tool call that is
waiting for the Customer (TOOL-05, TOOL-13) and answers with the next `ChatResponse`. A missing
or another Customer's confirmation is 404 `CONFIRMATION_NOT_FOUND`; one already resolved or
expired is 409 `CONFIRMATION_NOT_PENDING` (TOOL-96).

Demo limits (TASK-145, CT-70): after authentication and before the service, `POST /chat` runs
`enforce_chat_limits` (rate limit, then daily spend cap) and the confirmation runs
`enforce_llm_budget` (spend cap only) -> 429 `RATE_LIMITED` / `DAILY_CAP_REACHED`.
"""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from api.auth.deps import CurrentCustomer, require_customer
from api.chat.schemas import ChatRequest, ChatResponse
from api.chat.service import ChatService, get_chat_service
from api.limits.deps import enforce_chat_limits, enforce_llm_budget

chat_router = APIRouter()


class ConfirmationDecision(BaseModel):
    """Body of `POST /chat/confirmations/{confirmation_id}` (CT-46)."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["confirm", "refuse"]


@chat_router.post("/chat", response_model=ChatResponse, dependencies=[Depends(enforce_chat_limits)])
async def post_chat(
    request: ChatRequest,
    customer: Annotated[CurrentCustomer, Depends(require_customer)],
    service: Annotated[ChatService, Depends(get_chat_service)],
) -> ChatResponse:
    return await service.collect(service.handle_message(request, customer))


@chat_router.post(
    "/chat/confirmations/{confirmation_id}",
    response_model=ChatResponse,
    dependencies=[Depends(enforce_llm_budget)],
)
async def post_confirmation(
    confirmation_id: UUID,
    body: ConfirmationDecision,
    customer: Annotated[CurrentCustomer, Depends(require_customer)],
    service: Annotated[ChatService, Depends(get_chat_service)],
) -> ChatResponse:
    return await service.collect(
        service.resolve_confirmation(confirmation_id, body.decision, customer)
    )
