"""`POST /chat` (CT-14, TASK-020, TASK-032).

From phase 02 the route requires a Customer session (`require_customer`): without a valid
`atlas_session` cookie it answers 401 `AUTH_REQUIRED` before the service runs, so no LLM call
happens (TOOL-95). The identity comes only from the cookie; the body has no customer field.
A `conversation_id` of another Customer gets the same 404 as a missing one (CHAT-94).
"""

from typing import Annotated

from fastapi import APIRouter, Depends

from api.auth.deps import CurrentCustomer, require_customer
from api.chat.schemas import ChatRequest, ChatResponse
from api.chat.service import ChatService, get_chat_service

chat_router = APIRouter()


@chat_router.post("/chat", response_model=ChatResponse)
async def post_chat(
    request: ChatRequest,
    customer: Annotated[CurrentCustomer, Depends(require_customer)],
    service: Annotated[ChatService, Depends(get_chat_service)],
) -> ChatResponse:
    return await service.collect(service.handle_message(request, customer))
