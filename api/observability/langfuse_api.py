"""Langfuse public API client: feedback scores and trace deletion (CT-63, OBS-02, DEMO-10, LAC-36).

Contract (orchestrator skeleton; TASK-134 implements it without changing signatures):

- `LangfuseApi(settings, http=None)`: `enabled` is True only when `LANGFUSE_HOST`,
  `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` are all set. Requests use Basic auth
  `public:secret` against `LANGFUSE_HOST`. When `http` is None the client creates (and owns) an
  `httpx.AsyncClient` with a short timeout; `aclose()` closes only an owned client.
- `push_score(trace_id, value, comment=None, score_id=None)`: creates the score
  `name="user_feedback"`, `value` 1 (up) or -1 (down), linked to `traceId=trace_id`. With
  `score_id` the body carries `id=score_id`, so Langfuse upserts the same score when the customer
  changes the rating (one score per message). True on 2xx.
- `delete_traces_older_than(cutoff)`: lists traces with `toTimestamp=cutoff` page by page and
  deletes them in batches; returns the number of traces deleted.
- Disabled, a network error or a non-2xx response never raises: `push_score` returns False and
  `delete_traces_older_than` returns the count deleted so far (0 when nothing was deleted), with
  a warning log event. The exact endpoints follow the Langfuse public API docs of the version
  in use (risk R-6) and are module constants.

Endpoints (Langfuse public API v3, reference https://api.reference.langfuse.com; risk R-6 — the
docs were not fetched from this sandbox, so they follow the documented v3 routes and must be
re-checked when the Langfuse image is upgraded):

- `POST /api/public/scores` with `{id?, traceId, name, value, dataType: "NUMERIC", comment?}`
  (`id` makes Langfuse upsert the score).
- `GET /api/public/traces?toTimestamp=<ISO-8601>&page=N&limit=100`, paginated by
  `meta.totalPages` (pages start at 1).
- `DELETE /api/public/traces` with `{traceIds: [...]}` (batch delete; Langfuse queues it).

All trace ids are listed before the first delete, so deleting does not shift the pages being
read. Logs carry only the operation, the HTTP status or the error class, never the keys.
"""

from __future__ import annotations

import base64
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx

from api.observability.logging import LOGGER_NAME

if TYPE_CHECKING:
    from api.config import Settings

SCORE_NAME = "user_feedback"
SCORES_PATH = "/api/public/scores"
TRACES_PATH = "/api/public/traces"
PAGE_LIMIT = 100
DELETE_BATCH = 100
MAX_PAGES = 1000
TIMEOUT_SECONDS = 5.0
FAILED_EVENT = "langfuse_api_failed"


def _log_failure(operation: str, reason: str) -> None:
    logging.getLogger(LOGGER_NAME).warning(
        FAILED_EVENT, extra={"event": FAILED_EVENT, "operation": operation, "reason": reason}
    )


def _iso_utc(moment: datetime) -> str:
    aware = moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)
    return aware.isoformat().replace("+00:00", "Z")


class LangfuseApi:
    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._owns_http = http is None
        self._http = http if http is not None else httpx.AsyncClient(timeout=TIMEOUT_SECONDS)

    def _credentials(self) -> tuple[str, str, str] | None:
        settings = self._settings
        host = (settings.langfuse_host or "").strip().rstrip("/")
        public = settings.langfuse_public_key
        secret = settings.langfuse_secret_key
        public_value = public.get_secret_value() if public is not None else ""
        secret_value = secret.get_secret_value() if secret is not None else ""
        if not (host and public_value and secret_value):
            return None
        return host, public_value, secret_value

    @property
    def enabled(self) -> bool:
        return self._credentials() is not None

    async def _request(
        self, operation: str, method: str, path: str, **kwargs: Any
    ) -> httpx.Response | None:
        credentials = self._credentials()
        if credentials is None:
            return None
        host, public, secret = credentials
        token = base64.b64encode(f"{public}:{secret}".encode()).decode()
        try:
            response = await self._http.request(
                method, host + path, headers={"Authorization": f"Basic {token}"}, **kwargs
            )
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
            _log_failure(operation, type(exc).__name__)
            return None
        if not response.is_success:
            _log_failure(operation, f"http_{response.status_code}")
            return None
        return response

    async def push_score(
        self,
        trace_id: str,
        value: int,
        comment: str | None = None,
        score_id: str | None = None,
    ) -> bool:
        body: dict[str, object] = {
            "traceId": trace_id,
            "name": SCORE_NAME,
            "value": value,
            "dataType": "NUMERIC",
        }
        if comment is not None:
            body["comment"] = comment
        if score_id is not None:
            body["id"] = score_id
        response = await self._request("push_score", "POST", SCORES_PATH, json=body)
        return response is not None

    async def _list_trace_ids(self, cutoff: datetime) -> list[str]:
        ids: list[str] = []
        page, total_pages = 1, 1
        while page <= min(total_pages, MAX_PAGES):
            params: dict[str, str | int] = {
                "toTimestamp": _iso_utc(cutoff),
                "page": page,
                "limit": PAGE_LIMIT,
            }
            response = await self._request("list_traces", "GET", TRACES_PATH, params=params)
            if response is None:
                break
            try:
                payload = response.json()
                ids.extend(str(item["id"]) for item in payload.get("data", []))
                total_pages = int(payload.get("meta", {}).get("totalPages", page))
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                _log_failure("list_traces", type(exc).__name__)
                break
            page += 1
        return list(dict.fromkeys(ids))

    async def delete_traces_older_than(self, cutoff: datetime) -> int:
        if not self.enabled:
            return 0
        ids = await self._list_trace_ids(cutoff)
        deleted = 0
        for start in range(0, len(ids), DELETE_BATCH):
            batch = ids[start : start + DELETE_BATCH]
            response = await self._request(
                "delete_traces", "DELETE", TRACES_PATH, json={"traceIds": batch}
            )
            if response is None:
                break
            deleted += len(batch)
        return deleted

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()
