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
from typing import Protocol

import numpy as np

from api.config import Settings

MAX_BATCH_SIZE = 100
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
    """OpenAI embeddings in batches of `MAX_BATCH_SIZE` (TASK-053)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError("TASK-053")


def build_embedder(settings: Settings) -> EmbeddingClient:
    """`fake` -> `FakeEmbeddingClient(settings.embedding_dimensions)`; `openai` -> OpenAI client."""
    raise NotImplementedError("TASK-053")
