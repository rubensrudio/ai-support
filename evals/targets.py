"""In-process eval targets (EVAL-01, EVAL-02, TASK-126). No HTTP server.

- `ChatTarget(settings)`: runs the case message through `ChatService` with a persona seeded in an
  ephemeral database (personas + commerce seed), confirming destructive tools automatically,
  and returns the output dict of `evals.graders` (text, status, language, tool trajectory from
  `tool_start` events, citation doc_keys, error).
- `TriageTarget(settings)`: runs `TriageService.classify` and returns the output dict with
  `triage` set (the parsed result as a dict, or None when it failed).
Both implement `evals.runner.EvalTarget` (`async run(case) -> dict`).
"""

from typing import Any

from api.config import Settings
from evals.dataset import EvalCase


class ChatTarget:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def run(self, case: EvalCase) -> dict[str, Any]:
        raise NotImplementedError("TASK-126")


class TriageTarget:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def run(self, case: EvalCase) -> dict[str, Any]:
        raise NotImplementedError("TASK-126")
