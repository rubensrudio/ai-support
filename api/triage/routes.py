"""`POST /triage` (CT-49, TRI-01, TRI-05, TRI-07, TRI-90, LAC-35).

Body `TriageRequest` -> 200 `TriageResult`; 422 `VALIDATION_ERROR` (empty text, extra fields such
as `conversation_id`) without calling the LLM; 401 `AUTH_REQUIRED` without a persona session or
a valid `X-Operator-Key` (`require_customer_or_operator`) without calling the LLM; 503
`TRIAGE_UNAVAILABLE` with the error envelope only (no triage object). No rate limit yet.

The principal dependency is resolved before the service runs, so a rejected request never
reaches the LLM. `AtlasError(TRIAGE_UNAVAILABLE)` from the service is left to the standard
error handler, which renders the envelope without any triage field.
"""

from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends

from api.auth.deps import Principal, require_customer_or_operator
from api.config import get_settings
from api.llm.client import LLMClient
from api.llm.factory import build_llm_client
from api.triage.schemas import TriageRequest, TriageResult
from api.triage.service import TriageService

triage_router = APIRouter(tags=["triage"])


@lru_cache(maxsize=1)
def _llm_client() -> LLMClient:
    """One LLM client per process, built from the settings on first use."""
    return build_llm_client(get_settings())


def get_triage_service() -> TriageService:
    """FastAPI dependency: the triage service over the cached LLM client.

    Tests override it with `app.dependency_overrides`.
    """
    return TriageService(_llm_client(), get_settings())


@triage_router.post("/triage", response_model=TriageResult)
async def post_triage(
    request: TriageRequest,
    principal: Annotated[Principal, Depends(require_customer_or_operator)],
    service: Annotated[TriageService, Depends(get_triage_service)],
) -> TriageResult:
    # `principal` is only the access gate (persona session OR operator key); the triage
    # itself does not depend on who asked.
    del principal
    return await service.classify(request.text)
