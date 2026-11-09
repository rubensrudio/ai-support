"""Triage service (CT-74, TRI-02, TRI-03, TRI-07, TRI-08).

`classify(text, mode=None)`: `mode` defaults to `settings.triage_mode`.
- `schema`: the request carries `response_schema = TriageResult.model_json_schema()` (native
  structured output, TRI-02).
- `prompt_only`: only the instructions of `prompts/triage.md`, no `response_schema` (the "before"
  of demo 08).
The answer is validated with `TriageResult`; on failure the call is repeated up to
`settings.triage_max_repairs` times (default 2), each new request including the validation error
(TRI-03). Exhausted -> `AtlasError(TRIAGE_UNAVAILABLE, 503)` and the log `triage_failed`
(TRI-07). The input text never goes to the logs.
"""

from typing import Literal

from api.config import Settings
from api.llm.client import LLMClient
from api.triage.schemas import TriageResult

TriageMode = Literal["prompt_only", "schema"]


class TriageService:
    def __init__(self, llm: LLMClient, settings: Settings) -> None:
        self.llm = llm
        self.settings = settings

    async def classify(self, text: str, mode: TriageMode | None = None) -> TriageResult:
        raise NotImplementedError("TASK-088")
