"""Synthetic vector benchmark harness (CT-37, RAG-09, RAG-11, TASK-057).

- `generate_vectors(n, dim, seed)`: `float32` matrix `(n, dim)` of L2-normalised vectors,
  deterministic for a given seed.
- `exact_top_k(vectors, queries, k)`: ground truth, the ids (row indexes) of the `k` most
  cosine-similar vectors to each query, by exact brute force.
- `measure(search_fn, queries, k, *, truth)`: runs `search_fn(query, k) -> Sequence[int]` once
  per query, timing each call, and returns p50/p95 latency in milliseconds and the mean
  recall@5 against `truth` (`|top5(search) ∩ top5(truth)| / 5`, using the first 5 ids).
- `load_bench_table(session, vectors)`: bulk-loads `bench_vectors` with `COPY` (the table is
  created by migration 0006 in phase 05; phase 04 benchmarks brute force in memory only).
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np
from sqlalchemy.ext.asyncio import AsyncSession

SearchFn = Callable[[np.ndarray, int], Sequence[int]]


@dataclass(frozen=True)
class BenchResult:
    p50_ms: float
    p95_ms: float
    recall_at_5: float


def generate_vectors(n: int, dim: int, seed: int) -> np.ndarray:
    raise NotImplementedError("TASK-057")


def exact_top_k(vectors: np.ndarray, queries: np.ndarray, k: int) -> list[list[int]]:
    raise NotImplementedError("TASK-057")


def measure(
    search_fn: SearchFn,
    queries: np.ndarray,
    k: int,
    *,
    truth: Sequence[Sequence[int]],
) -> BenchResult:
    raise NotImplementedError("TASK-057")


async def load_bench_table(session: AsyncSession, vectors: np.ndarray) -> None:
    raise NotImplementedError("TASK-057")
