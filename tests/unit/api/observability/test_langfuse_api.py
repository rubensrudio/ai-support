import base64
import json
import logging
from collections.abc import Callable, Iterator
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr

from api.config import Settings
from api.observability import logging as atlas_logging
from api.observability.langfuse_api import (
    SCORE_NAME,
    SCORES_PATH,
    TRACES_PATH,
    LangfuseApi,
)

HOST = "https://lf.example.com"
PUBLIC = "pk-lf-public"
SECRET = "sk-lf-secret"  # noqa: S105 - test fixture


def _settings(enabled: bool = True) -> Settings:
    if not enabled:
        return Settings(_env_file=None)  # type: ignore[call-arg]
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        langfuse_host=HOST + "/",
        langfuse_public_key=SecretStr(PUBLIC),
        langfuse_secret_key=SecretStr(SECRET),
    )


class _Recorder:
    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self._handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))


@pytest.fixture
def warnings() -> Iterator[list[logging.LogRecord]]:
    records: list[logging.LogRecord] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Handler()
    logger = logging.getLogger(atlas_logging.LOGGER_NAME)
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)


def test_enabled_requires_host_and_both_keys() -> None:
    assert LangfuseApi(_settings()).enabled is True
    assert LangfuseApi(_settings(enabled=False)).enabled is False
    partial = Settings(  # type: ignore[call-arg]
        _env_file=None, langfuse_host=HOST, langfuse_public_key=SecretStr(PUBLIC)
    )
    assert LangfuseApi(partial).enabled is False


async def test_push_score_posts_score_linked_to_trace_with_basic_auth() -> None:
    recorder = _Recorder(lambda request: httpx.Response(200, json={"id": "score-1"}))
    async with recorder.client() as http:
        api = LangfuseApi(_settings(), http)
        assert await api.push_score("abc123", -1, comment="wrong answer") is True

    (request,) = recorder.requests
    assert request.method == "POST"
    assert str(request.url) == HOST + SCORES_PATH
    body = json.loads(request.content)
    assert body == {
        "traceId": "abc123",
        "name": SCORE_NAME,
        "value": -1,
        "dataType": "NUMERIC",
        "comment": "wrong answer",
    }
    expected = base64.b64encode(f"{PUBLIC}:{SECRET}".encode()).decode()
    assert request.headers["Authorization"] == f"Basic {expected}"


async def test_push_score_without_comment_omits_it() -> None:
    recorder = _Recorder(lambda request: httpx.Response(201, json={"id": "score-1"}))
    async with recorder.client() as http:
        assert await LangfuseApi(_settings(), http).push_score("abc", 1) is True
    body = json.loads(recorder.requests[0].content)
    assert body["value"] == 1
    assert "comment" not in body


async def test_push_score_http_500_returns_false_and_logs(
    warnings: list[logging.LogRecord],
) -> None:
    recorder = _Recorder(lambda request: httpx.Response(500, text="boom"))
    async with recorder.client() as http:
        assert await LangfuseApi(_settings(), http).push_score("abc", 1) is False
    failed = [r for r in warnings if getattr(r, "event", None) == "langfuse_api_failed"]
    assert failed
    assert failed[0].levelno == logging.WARNING
    dumped = json.dumps({k: str(v) for k, v in vars(failed[0]).items()})
    assert SECRET not in dumped
    assert PUBLIC not in dumped


async def test_push_score_network_error_returns_false() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable", request=request)

    async with _Recorder(fail).client() as http:
        assert await LangfuseApi(_settings(), http).push_score("abc", 1) is False


async def test_disabled_client_sends_no_request() -> None:
    recorder = _Recorder(lambda request: httpx.Response(200, json={}))
    async with recorder.client() as http:
        api = LangfuseApi(_settings(enabled=False), http)
        assert await api.push_score("abc", 1) is False
        assert await api.delete_traces_older_than(datetime(2026, 1, 1, tzinfo=UTC)) == 0
    assert recorder.requests == []


def _paged_handler(pages: dict[int, list[str]]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            page = int(request.url.params["page"])
            data = [{"id": trace_id} for trace_id in pages.get(page, [])]
            meta = {"page": page, "limit": 100, "totalPages": len(pages)}
            return httpx.Response(200, json={"data": data, "meta": meta})
        ids = json.loads(request.content)["traceIds"]
        return httpx.Response(200, json={"message": f"deleted {len(ids)}"})

    return handler


async def test_delete_traces_paginates_and_sums() -> None:
    recorder = _Recorder(_paged_handler({1: ["t1", "t2", "t3"], 2: ["t4", "t5"]}))
    cutoff = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    async with recorder.client() as http:
        assert await LangfuseApi(_settings(), http).delete_traces_older_than(cutoff) == 5

    gets = [r for r in recorder.requests if r.method == "GET"]
    assert [r.url.params["page"] for r in gets] == ["1", "2"]
    assert all(r.url.path == TRACES_PATH for r in gets)
    assert gets[0].url.params["toTimestamp"] == "2026-11-01T12:00:00Z"
    deletes = [r for r in recorder.requests if r.method == "DELETE"]
    assert deletes
    assert all(r.url.path == TRACES_PATH for r in deletes)
    deleted = [i for r in deletes for i in json.loads(r.content)["traceIds"]]
    assert sorted(deleted) == ["t1", "t2", "t3", "t4", "t5"]


async def test_delete_traces_naive_cutoff_is_treated_as_utc() -> None:
    recorder = _Recorder(_paged_handler({1: []}))
    async with recorder.client() as http:
        await LangfuseApi(_settings(), http).delete_traces_older_than(datetime(2026, 11, 1))
    assert recorder.requests[0].url.params["toTimestamp"] == "2026-11-01T00:00:00Z"


async def test_delete_traces_failure_returns_partial_count(
    warnings: list[logging.LogRecord],
) -> None:
    pages = {1: [f"a{i}" for i in range(100)], 2: [f"b{i}" for i in range(20)]}
    base = _paged_handler(pages)
    deletes = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal deletes
        if request.method == "DELETE":
            deletes += 1
            if deletes > 1:
                return httpx.Response(500, text="boom")
        return base(request)

    async with _Recorder(handler).client() as http:
        count = await LangfuseApi(_settings(), http).delete_traces_older_than(
            datetime(2026, 11, 1, tzinfo=UTC)
        )
    assert count == 100
    assert any(getattr(r, "event", None) == "langfuse_api_failed" for r in warnings)


async def test_delete_traces_listing_error_returns_zero() -> None:
    recorder = _Recorder(lambda request: httpx.Response(503, text="down"))
    async with recorder.client() as http:
        count = await LangfuseApi(_settings(), http).delete_traces_older_than(
            datetime(2026, 11, 1, tzinfo=UTC)
        )
    assert count == 0


async def test_aclose_closes_only_owned_client() -> None:
    external = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    api = LangfuseApi(_settings(), external)
    await api.aclose()
    assert external.is_closed is False
    await external.aclose()

    owned = LangfuseApi(_settings())
    await owned.aclose()
    assert owned._http.is_closed is True


async def test_push_score_with_score_id_sends_id_for_upsert() -> None:
    recorder = _Recorder(lambda request: httpx.Response(200, json={"id": "fb-42"}))
    async with recorder.client() as http:
        api = LangfuseApi(_settings(), http)
        assert await api.push_score("abc", 1, score_id="fb-42") is True
        assert await api.push_score("abc", -1, score_id="fb-42") is True

    bodies = [json.loads(r.content) for r in recorder.requests]
    assert [b["id"] for b in bodies] == ["fb-42", "fb-42"]
    assert [b["value"] for b in bodies] == [1, -1]
    assert all(b["traceId"] == "abc" for b in bodies)


async def test_push_score_without_score_id_omits_id() -> None:
    recorder = _Recorder(lambda request: httpx.Response(200, json={"id": "x"}))
    async with recorder.client() as http:
        assert await LangfuseApi(_settings(), http).push_score("abc", 1) is True
    assert "id" not in json.loads(recorder.requests[0].content)


@pytest.mark.parametrize("host", ["http://host:abc", "http://[::1", "lf.example.com"])
async def test_malformed_host_returns_false_and_logs(
    host: str, warnings: list[logging.LogRecord]
) -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        langfuse_host=host,
        langfuse_public_key=SecretStr(PUBLIC),
        langfuse_secret_key=SecretStr(SECRET),
    )
    recorder = _Recorder(lambda request: httpx.Response(200, json={}))
    async with recorder.client() as http:
        api = LangfuseApi(settings, http)
        assert await api.push_score("abc", 1) is False
        assert await api.delete_traces_older_than(datetime(2026, 11, 1, tzinfo=UTC)) == 0
    assert any(getattr(r, "event", None) == "langfuse_api_failed" for r in warnings)
