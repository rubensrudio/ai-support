import json
import math
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

import api.rag.embeddings as embeddings_module
from api.config import Settings
from api.rag.embeddings import (
    MAX_BATCH_SIZE,
    EmbeddingError,
    FakeEmbeddingClient,
    OpenAIEmbeddingClient,
    build_embedder,
)

API_HOST = "api.openai.com"
FAKE_KEY = "sk-openai-test-0123456789-secret"
DIM = 8
SECRET_BODY = "upstream-internal-detail-do-not-leak"

Handler = Callable[[httpx.Request], httpx.Response]


def _settings(
    key: str | None = FAKE_KEY, provider: str = "openai", dim: int = DIM, **extra: Any
) -> Settings:
    values: dict[str, Any] = {
        "embedding_provider": provider,
        "openai_api_key": SecretStr(key) if key is not None else None,
        "embedding_model": "text-embedding-3-small",
        "embedding_dimensions": dim,
        "llm_timeout_seconds": 5.0,
    }
    values.update(extra)
    return Settings(_env_file=None, **values)


def _client(
    handler: Handler,
    captured: list[httpx.Request] | None = None,
    settings: Settings | None = None,
) -> OpenAIEmbeddingClient:
    sink = captured if captured is not None else []

    def guarded(request: httpx.Request) -> httpx.Response:
        if request.url.host != API_HOST:
            raise AssertionError(f"unexpected network call to {request.url.host}")
        sink.append(request)
        return handler(request)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(guarded))
    return OpenAIEmbeddingClient(settings or _settings(), http_client=http_client)


def _marker_vector(marker: float, dim: int = DIM) -> list[float]:
    return [marker] + [0.0] * (dim - 1)


def _marker(text: str) -> float:
    return float(text.removeprefix("text-"))


def _ok_handler(*, reverse: bool = False, dim: int = DIM) -> Handler:
    """Echo each input as a vector whose first position is the number in `text-<n>`."""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        data = [
            {"object": "embedding", "index": i, "embedding": _marker_vector(_marker(t), dim)}
            for i, t in enumerate(payload["input"])
        ]
        if reverse:
            data.reverse()
        return httpx.Response(
            200,
            json={"object": "list", "data": data, "model": payload["model"], "usage": {}},
        )

    return handler


def _texts(n: int) -> list[str]:
    return [f"text-{i}" for i in range(n)]


# --- OpenAIEmbeddingClient ---------------------------------------------------------------


async def test_batches_250_texts_into_3_requests_preserving_order() -> None:
    captured: list[httpx.Request] = []
    client = _client(_ok_handler(), captured)

    vectors = await client.embed(_texts(250))

    assert len(captured) == 3
    sizes = [len(json.loads(r.content)["input"]) for r in captured]
    assert sizes == [MAX_BATCH_SIZE, MAX_BATCH_SIZE, 50]
    assert [v[0] for v in vectors] == [float(i) for i in range(250)]
    assert all(len(v) == DIM for v in vectors)


async def test_request_carries_model_dimensions_and_bearer_key() -> None:
    captured: list[httpx.Request] = []
    client = _client(_ok_handler(), captured)

    await client.embed(["text-1"])

    (request,) = captured
    assert request.method == "POST"
    assert request.url.path == "/v1/embeddings"
    assert request.headers["authorization"] == f"Bearer {FAKE_KEY}"
    payload = json.loads(request.content)
    assert payload["model"] == "text-embedding-3-small"
    assert payload["dimensions"] == DIM
    assert payload["input"] == ["text-1"]


async def test_out_of_order_response_is_sorted_by_index() -> None:
    client = _client(_ok_handler(reverse=True))

    vectors = await client.embed(_texts(5))

    assert [v[0] for v in vectors] == [0.0, 1.0, 2.0, 3.0, 4.0]


async def test_empty_input_returns_empty_without_request() -> None:
    captured: list[httpx.Request] = []
    client = _client(_ok_handler(), captured)

    assert await client.embed([]) == []
    assert captured == []


def _assert_generic(error: EmbeddingError, texts: list[str]) -> None:
    message = str(error)
    assert FAKE_KEY not in message
    assert SECRET_BODY not in message
    for text in texts:
        assert text not in message


async def test_http_500_becomes_embedding_error_without_leaking() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": SECRET_BODY, "key": FAKE_KEY}})

    client = _client(handler)
    texts = ["confidential customer question"]

    with pytest.raises(EmbeddingError) as exc_info:
        await client.embed(texts)

    _assert_generic(exc_info.value, texts)
    assert exc_info.value.__cause__ is None or SECRET_BODY not in str(exc_info.value.__cause__)


async def test_http_429_becomes_embedding_error() -> None:
    client = _client(lambda request: httpx.Response(429, text=SECRET_BODY))

    with pytest.raises(EmbeddingError):
        await client.embed(["text-1"])


async def test_missing_key_constructs_but_embed_raises() -> None:
    captured: list[httpx.Request] = []
    client = _client(_ok_handler(), captured, settings=_settings(key=None))

    with pytest.raises(EmbeddingError) as exc_info:
        await client.embed(["text-1"])

    assert "OPENAI_API_KEY" in str(exc_info.value)
    assert captured == []


async def test_empty_key_is_treated_as_missing() -> None:
    captured: list[httpx.Request] = []
    client = _client(_ok_handler(), captured, settings=_settings(key=""))

    with pytest.raises(EmbeddingError):
        await client.embed(["text-1"])
    assert captured == []


async def test_construct_without_key_and_without_http_client_never_fails() -> None:
    client = OpenAIEmbeddingClient(_settings(key=None))

    with pytest.raises(EmbeddingError):
        await client.embed(["text-1"])


async def test_wrong_dimension_becomes_embedding_error() -> None:
    client = _client(_ok_handler(dim=DIM + 1))

    with pytest.raises(EmbeddingError):
        await client.embed(["text-1"])


async def test_wrong_vector_count_becomes_embedding_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": _marker_vector(0.0)}]})

    client = _client(handler)

    with pytest.raises(EmbeddingError):
        await client.embed(["text-0", "text-1"])


@pytest.mark.parametrize(
    "body",
    [
        "not json at all",
        json.dumps({"no_data": []}),
        json.dumps({"data": [{"index": 0}]}),
        json.dumps({"data": [{"index": 0, "embedding": ["x"] * DIM}]}),
        json.dumps({"data": [{"index": 5, "embedding": [0.0] * DIM}]}),
        json.dumps({"data": [{"index": "0", "embedding": [0.0] * DIM}]}),
        json.dumps([1, 2, 3]),
    ],
)
async def test_malformed_response_becomes_embedding_error(body: str) -> None:
    client = _client(lambda request: httpx.Response(200, text=body))

    with pytest.raises(EmbeddingError):
        await client.embed(["text-0"])


async def test_duplicate_index_becomes_embedding_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        item = {"index": 0, "embedding": [0.0] * DIM}
        return httpx.Response(200, json={"data": [item, item]})

    client = _client(handler)

    with pytest.raises(EmbeddingError):
        await client.embed(["text-0", "text-1"])


async def test_timeout_becomes_embedding_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    client = _client(handler)

    with pytest.raises(EmbeddingError):
        await client.embed(["text-1"])


async def test_network_error_becomes_embedding_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"refused {FAKE_KEY}", request=request)

    client = _client(handler)

    with pytest.raises(EmbeddingError) as exc_info:
        await client.embed(["text-1"])
    _assert_generic(exc_info.value, ["text-1"])


async def test_failure_issues_a_single_request_no_retry() -> None:
    captured: list[httpx.Request] = []
    client = _client(lambda request: httpx.Response(503), captured)

    with pytest.raises(EmbeddingError):
        await client.embed(["text-1"])
    assert len(captured) == 1


async def test_timeout_comes_from_settings() -> None:
    client = OpenAIEmbeddingClient(_settings(llm_timeout_seconds=7.5))

    assert client.timeout_seconds == 7.5


# --- FakeEmbeddingClient -----------------------------------------------------------------


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


async def test_fake_same_text_same_vector() -> None:
    fake = FakeEmbeddingClient(64)
    other = FakeEmbeddingClient(64)

    first, second = await fake.embed(["Cancel my plan", "Cancel my plan"])

    assert first == second
    assert first == other.vector("Cancel my plan")
    assert len(first) == 64


async def test_fake_vectors_have_unit_norm() -> None:
    fake = FakeEmbeddingClient(32)

    for vector in await fake.embed(["hello world", "", "x"]):
        assert math.isclose(math.sqrt(sum(x * x for x in vector)), 1.0, rel_tol=1e-9)


async def test_fake_synonyms_bring_texts_closer() -> None:
    plain = FakeEmbeddingClient(256)
    synonyms = FakeEmbeddingClient(256, synonyms={"encerrar": "cancelar", "assinatura": "plano"})

    query, target = "cancelar plano", "encerrar assinatura"

    assert _cosine(synonyms.vector(query), synonyms.vector(target)) > 0.99
    assert _cosine(plain.vector(query), plain.vector(target)) < 0.5


async def test_fake_records_calls() -> None:
    fake = FakeEmbeddingClient(8)

    await fake.embed(["a", "b"])
    await fake.embed([])

    assert fake.calls == [["a", "b"], []]


# --- build_embedder ----------------------------------------------------------------------


def test_build_embedder_fake() -> None:
    embedder = build_embedder(_settings(provider="fake", dim=12))

    assert isinstance(embedder, FakeEmbeddingClient)
    assert embedder.dim == 12


def test_build_embedder_openai_without_key_does_not_fail() -> None:
    embedder = build_embedder(_settings(provider="openai", key=None))

    assert isinstance(embedder, OpenAIEmbeddingClient)


# --- HTTP client lifecycle ---------------------------------------------------------------


def _counting_factory(
    monkeypatch: pytest.MonkeyPatch, captured: list[httpx.Request]
) -> list[httpx.AsyncClient]:
    """Patch the module's client factory so owned clients use a MockTransport."""
    created: list[httpx.AsyncClient] = []
    handler = _ok_handler()

    def transport_handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != API_HOST:
            raise AssertionError(f"unexpected network call to {request.url.host}")
        captured.append(request)
        return handler(request)

    def factory() -> httpx.AsyncClient:
        client = httpx.AsyncClient(transport=httpx.MockTransport(transport_handler))
        created.append(client)
        return client

    monkeypatch.setattr(embeddings_module, "_new_http_client", factory)
    return created


async def test_owned_http_client_is_created_lazily_and_reused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[httpx.Request] = []
    created = _counting_factory(monkeypatch, captured)
    client = OpenAIEmbeddingClient(_settings())

    assert created == []
    await client.embed(["text-1"])
    await client.embed(_texts(150))

    assert len(created) == 1
    assert len(captured) == 3
    assert not created[0].is_closed
    await client.aclose()


async def test_aclose_closes_owned_client_and_next_embed_reopens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[httpx.Request] = []
    created = _counting_factory(monkeypatch, captured)
    client = OpenAIEmbeddingClient(_settings())

    await client.embed(["text-1"])
    await client.aclose()
    assert created[0].is_closed

    await client.embed(["text-2"])
    assert len(created) == 2
    assert not created[1].is_closed
    await client.aclose()


async def test_aclose_without_embed_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    created = _counting_factory(monkeypatch, [])
    client = OpenAIEmbeddingClient(_settings())

    await client.aclose()
    await client.aclose()

    assert created == []


async def test_missing_key_does_not_open_owned_client(monkeypatch: pytest.MonkeyPatch) -> None:
    created = _counting_factory(monkeypatch, [])
    client = OpenAIEmbeddingClient(_settings(key=None))

    with pytest.raises(EmbeddingError):
        await client.embed(["text-1"])
    assert created == []


async def test_aclose_never_closes_injected_client(monkeypatch: pytest.MonkeyPatch) -> None:
    created = _counting_factory(monkeypatch, [])
    injected = httpx.AsyncClient(transport=httpx.MockTransport(_ok_handler()))
    client = OpenAIEmbeddingClient(_settings(), http_client=injected)

    await client.embed(["text-1"])
    await client.aclose()

    assert not injected.is_closed
    assert created == []
    await injected.aclose()
