"""Feedback on assistant messages (CT-65, OBS-02, OBS-92).

Contract (orchestrator skeleton; TASK-136 implements it without changing the route):

`POST /messages/{message_id}/feedback` with body `{"rating": "up" | "down"}` and the persona
session cookie (`current_customer`, CT-16):

- 204 with no body: the message exists, has `role == "assistant"` and belongs to a conversation
  of the session customer. The feedback row is upserted (one per message; a second call replaces
  the rating) with the message `trace_id`, and the score (up = 1, down = -1) is pushed to
  Langfuse in the background (`LangfuseApi.push_score`, best effort; a failure never changes the
  response).
- 404 `MESSAGE_NOT_FOUND`: the message does not exist, is not an assistant message or belongs to
  another customer. Same body for all three cases and nothing is written (OBS-92).
- 401 `AUTH_REQUIRED`: no valid session. 422 for an invalid body.
"""

from fastapi import APIRouter

feedback_router = APIRouter(tags=["feedback"])


@feedback_router.post("/messages/{message_id}/feedback", status_code=501)
async def post_feedback(message_id: int) -> None:
    raise NotImplementedError("TASK-136")
