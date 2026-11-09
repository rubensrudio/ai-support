"""Embedding clients (CT-34, DA-19, RAG-07, RAG-93, RAG-94).

`EmbeddingClient.embed` is async: every caller (ingestion, retriever) already runs in an event
loop and the real client does network I/O. It returns one vector per input text, in input order,
each with `ATLAS_EMBEDDING_DIMENSIONS` positions. Any failure of the provider becomes
`EmbeddingError`, whose message never carries the provider response or the input texts.

- `OpenAIEmbeddingClient(settings)`: `text-embedding-3-small` with `dimensions =
  settings.embedding_dimensions`, in batches of at most `MAX_BATCH_SIZE` texts (TASK-053).
- `FakeEmbeddingClient(dim, synonyms=None)`: deterministic, offline. Each word (lower-cased,
  after the optional `synonyms` mapping word -> canonical word) gets a pseudo-random vector
  seeded by its SHA-256; a text is the normalised sum of its words. Same text, same vector;
  texts that share canonical words are close in cosine similarity.
- `build_embedder(settings)`: `openai` or `fake`, from `settings.embedding_provider`.
"""

import hashlib
import re
from collections.abc import Mapping
from typing import Any, Protocol

import httpx
import numpy as np

from api.config import Settings

MAX_BATCH_SIZE = 100
_OPENAI_EMBEDDINGS_URL = "https://api.openai.com/v1/embeddings"
_WORD = re.compile(r"\w+", re.UNICODE)


class EmbeddingError(RuntimeError):
    """The embedding provider failed (HTTP error, timeout, malformed response, missing key)."""


class EmbeddingClient(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class FakeEmbeddingClient:
    """Deterministic hash embedder for tests, CI and `--provider fake` demos."""

    def __init__(self, dim: int, synonyms: Mapping[str, str] | None = None) -> None:
        if dim <= 0:
            raise ValueError("dim must be positive")
        self.dim = dim
        self.synonyms = {k.lower(): v.lower() for k, v in (synonyms or {}).items()}
        self.calls: list[list[str]] = []

    def _word_vector(self, word: str) -> np.ndarray:
        seed = int.from_bytes(hashlib.sha256(word.encode("utf-8")).digest()[:8], "big")
        return np.random.default_rng(seed).standard_normal(self.dim)

    def vector(self, text: str) -> list[float]:
        words = [self.synonyms.get(w, w) for w in _WORD.findall(text.lower())]
        total = np.zeros(self.dim)
        for word in words or [""]:
            total += self._word_vector(word)
        norm = float(np.linalg.norm(total))
        return [float(x) for x in (total / norm if norm else total)]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self.vector(text) for text in texts]


class OpenAIEmbeddingClient:
    """OpenAI embeddings in batches of `MAX_BATCH_SIZE` (TASK-053).

    Talks to the REST endpoint through `httpx` directly (the installed `openai` SDK runs on
    `httpx2`). There is no retry here: resilience is phase 12. Building the client never fails,
    even without a key: `get_chat_service` builds it (once per process, cached) while serving a
    request, so a missing key must not break the chat; `embed` raises `EmbeddingError` instead.
    Error messages are generic on purpose: they never carry the key, the provider response or
    the input texts.

    Without an injected `http_client`, the instance opens its own `httpx.AsyncClient` on the
    first `embed` and reuses it (keep-alive, one TLS handshake); `aclose()` closes only that
    owned client. An injected client is never closed here: its owner closes it.
    """

    def __init__(self, settings: Settings, *, http_client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.model = settings.embedding_model
        self.dimensions = settings.embedding_dimensions
        self.timeout_seconds = settings.llm_timeout_seconds
        self._injected_client = http_client
        self._owned_client: httpx.AsyncClient | None = None

    def _api_key(self) -> str:
        key = self.settings.openai_api_key
        value = key.get_secret_value() if key is not None else ""
        if not value:
            raise EmbeddingError("Missing required configuration: OPENAI_API_KEY")
        return value

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        key = self._api_key()
        batches = [texts[i : i + MAX_BATCH_SIZE] for i in range(0, len(texts), MAX_BATCH_SIZE)]
        return await self._embed_batches(self._client(), key, batches)

    def _client(self) -> httpx.AsyncClient:
        if self._injected_client is not None:
            return self._injected_client
        if self._owned_client is None or self._owned_client.is_closed:
            self._owned_client = _new_http_client()
        return self._owned_client

    async def aclose(self) -> None:
        """Close the HTTP client this instance created, if any; never the injected one."""
        owned, self._owned_client = self._owned_client, None
        if owned is not None:
            await owned.aclose()

    async def _embed_batches(
        self, client: httpx.AsyncClient, key: str, batches: list[list[str]]
    ) -> list[list[float]]:
        vectors: list[list[float]] = []
        for batch in batches:
            vectors.extend(await self._embed_batch(client, key, batch))
        return vectors

    async def _embed_batch(
        self, client: httpx.AsyncClient, key: str, batch: list[str]
    ) -> list[list[float]]:
        try:
            response = await client.post(
                _OPENAI_EMBEDDINGS_URL,
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": self.model,
                    "input": batch,
                    "dimensions": self.dimensions,
                    "encoding_format": "float",
                },
                timeout=self.timeout_seconds,
            )
        except httpx.TimeoutException:
            raise EmbeddingError("Embedding provider timed out") from None
        except httpx.HTTPError:
            raise EmbeddingError("Embedding provider request failed") from None
        if response.status_code >= 400:
            raise EmbeddingError(f"Embedding provider returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            raise _malformed() from None
        return _parse_vectors(payload, expected=len(batch), dim=self.dimensions)


def _new_http_client() -> httpx.AsyncClient:
    """HTTP client owned by an `OpenAIEmbeddingClient` (patched in tests)."""
    return httpx.AsyncClient()


def _malformed() -> EmbeddingError:
    return EmbeddingError("Embedding provider returned a malformed response")


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _item_index(item: Any, expected: int, seen: dict[int, list[float]]) -> int:
    """The item's `index`: an int in `[0, expected)` not seen before."""
    index = item.get("index") if isinstance(item, dict) else None
    if not isinstance(index, int) or isinstance(index, bool):
        raise _malformed()
    if not 0 <= index < expected or index in seen:
        raise _malformed()
    return index


def _item_vector(item: Any, dim: int) -> list[float]:
    """The item's `embedding`: a list of `dim` numbers."""
    embedding = item.get("embedding") if isinstance(item, dict) else None
    if not isinstance(embedding, list) or not all(_is_number(x) for x in embedding):
        raise _malformed()
    if len(embedding) != dim:
        raise EmbeddingError(
            f"Embedding provider returned vectors of {len(embedding)} dimensions, expected {dim}"
        )
    return [float(x) for x in embedding]


def _parse_vectors(payload: Any, *, expected: int, dim: int) -> list[list[float]]:
    """Validate the `data` list and return its vectors sorted by `index`."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise _malformed()
    if len(data) != expected:
        raise EmbeddingError(
            f"Embedding provider returned {len(data)} vectors for {expected} inputs"
        )
    by_index: dict[int, list[float]] = {}
    for item in data:
        index = _item_index(item, expected, by_index)
        by_index[index] = _item_vector(item, dim)
    return [by_index[i] for i in range(expected)]


def build_embedder(settings: Settings) -> EmbeddingClient:
    """`fake` -> `FakeEmbeddingClient(settings.embedding_dimensions)`; `openai` -> OpenAI client."""
    if settings.embedding_provider == "fake":
        return FakeEmbeddingClient(settings.embedding_dimensions)
    return OpenAIEmbeddingClient(settings)
