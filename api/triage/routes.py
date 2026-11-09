"""`POST /triage` (CT-49, TRI-01, TRI-05, TRI-07, TRI-90, LAC-35).

Body `TriageRequest` -> 200 `TriageResult`; 422 `VALIDATION_ERROR` (empty text, extra fields such
as `conversation_id`) without calling the LLM; 401 `AUTH_REQUIRED` without a persona session or
a valid `X-Operator-Key` (`require_customer_or_operator`) without calling the LLM; 503
`TRIAGE_UNAVAILABLE` with the error envelope only (no triage object). No rate limit yet.
"""

from fastapi import APIRouter, HTTPException

triage_router = APIRouter(tags=["triage"])


@triage_router.post("/triage")
async def triage() -> None:  # TASK-089 replaces this stub
    raise HTTPException(status_code=501, detail="Not implemented")
