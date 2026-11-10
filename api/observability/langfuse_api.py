"""Langfuse public API client: feedback scores and trace deletion (CT-63, OBS-02, DEMO-10, LAC-36).

Contract (orchestrator skeleton; TASK-134 implements it without changing signatures):

- `LangfuseApi(settings, http=None)`: `enabled` is True only when `LANGFUSE_HOST`,
  `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` are all set. Requests use Basic auth
  `public:secret` against `LANGFUSE_HOST`. When `http` is None the client creates (and owns) an
  `httpx.AsyncClient` with a short timeout; `aclose()` closes only an owned client.
- `push_score(trace_id, value, comment=None)`: creates the score `name="user_feedback"`,
  `value` 1 (up) or -1 (down), linked to `traceId=trace_id`. True on 2xx.
- `delete_traces_older_than(cutoff)`: lists traces with `toTimestamp=cutoff` page by page and
  deletes them in batches; returns the number of traces deleted.
- Disabled, a network error or a non-2xx response never raises: `push_score` returns False and
  `delete_traces_older_than` returns the count deleted so far (0 when nothing was deleted), with
  a warning log event. The exact endpoints follow the Langfuse public API docs of the version
  in use (risk R-6) and are module constants.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from api.config import Settings

SCORE_NAME = "user_feedback"


class LangfuseApi:
    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._http = http

    @property
    def enabled(self) -> bool:
        raise NotImplementedError("TASK-134")

    async def push_score(self, trace_id: str, value: int, comment: str | None = None) -> bool:
        raise NotImplementedError("TASK-134")

    async def delete_traces_older_than(self, cutoff: datetime) -> int:
        raise NotImplementedError("TASK-134")

    async def aclose(self) -> None:
        raise NotImplementedError("TASK-134")
