"""Streaming chat endpoints (CT-76, STR-01, STR-04, STR-05, STR-90, STR-91).

`POST /chat/stream` (body CT-14 `ChatRequest`) and `POST /chat/confirmations/{id}/stream` (body
CT-46) -> 200 `text/event-stream` (`Cache-Control: no-cache`, `X-Accel-Buffering: no`) with the
CT-45 events encoded by `api.streaming.events`; 401 before opening the stream. The JSON
`POST /chat` stays (demos and evals use it).
"""

from fastapi import APIRouter

stream_router = APIRouter(tags=["chat"])
