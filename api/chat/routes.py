"""`POST /chat` (CT-14, TASK-020). Phase 01: anonymous and stateless."""

from typing import Annotated

from fastapi import APIRouter, Depends

from api.chat.schemas import ChatRequest, ChatResponse
from api.chat.service import ChatService, get_chat_service

chat_router = APIRouter()


@chat_router.post("/chat", response_model=ChatResponse)
async def post_chat(
    request: ChatRequest, service: Annotated[ChatService, Depends(get_chat_service)]
) -> ChatResponse:
    return await service.collect(service.handle_message(request, None))
