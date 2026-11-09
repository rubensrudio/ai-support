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

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
from sqlalchemy.ext.asyncio import AsyncSession

SearchFn = Callable[[np.ndarray, int], Sequence[int]]

RECALL_AT = 5
BENCH_COPY_SQL = "COPY bench_vectors (embedding) FROM STDIN"
# Upper bound, in cells, of the (block, n) float32 score matrix built by `exact_top_k`.
# The peak per block is that matrix plus the partitioned copy used to find each row's k-th
# score (2 x 4 bytes per cell): 2**24 cells = 64 MiB of scores, ~128 MiB peak, so
# 1,000,000 vectors are scored 16 queries at a time.
_SCORE_BLOCK_CELLS = 2**24


@dataclass(frozen=True)
class BenchResult:
    p50_ms: float
    p95_ms: float
    recall_at_5: float


def generate_vectors(n: int, dim: int, seed: int) -> np.ndarray:
    """`(n, dim)` float32 rows drawn from a standard normal and scaled to unit L2 norm."""
    if n <= 0 or dim <= 0:
        raise ValueError(f"n and dim must be positive, got n={n}, dim={dim}")
    rng = np.random.default_rng(seed)
    vectors = rng.standard_normal((n, dim), dtype=np.float32)
    norms = np.sqrt(np.einsum("ij,ij->i", vectors, vectors, dtype=np.float64))
    norms[norms == 0.0] = 1.0
    vectors /= norms[:, np.newaxis].astype(np.float32)
    return vectors


def exact_top_k(
    vectors: np.ndarray, queries: np.ndarray, k: int, *, block_size: int | None = None
) -> list[list[int]]:
    """Row ids of the `k` most cosine-similar vectors per query, best first (exact).

    Queries are scored in blocks so the score matrix never exceeds `_SCORE_BLOCK_CELLS`
    cells (`block_size` overrides the derived block). Equal scores rank the lower id first,
    including ties at the k-th place (threshold technique, as `api.rag.vector_bruteforce`).
    `k` larger than the number of vectors returns every id.
    """
    if k <= 0:
        raise ValueError(f"k must be positive, got {k}")
    if vectors.ndim != 2 or queries.ndim != 2 or vectors.shape[1] != queries.shape[1]:
        raise ValueError(
            f"vectors and queries must be 2-D with the same width, "
            f"got {vectors.shape} and {queries.shape}"
        )
    n = vectors.shape[0]
    k = min(k, n)
    if block_size is None:
        block_size = max(1, _SCORE_BLOCK_CELLS // max(n, 1))
    if block_size <= 0:
        raise ValueError(f"block_size must be positive, got {block_size}")

    vectors = np.asarray(vectors, dtype=np.float32)
    vector_norms = np.sqrt(np.einsum("ij,ij->i", vectors, vectors)).astype(np.float32)
    vector_norms[vector_norms == 0.0] = 1.0

    result: list[list[int]] = []
    for start in range(0, queries.shape[0], block_size):
        block = np.asarray(queries[start : start + block_size], dtype=np.float32)
        query_norms = np.linalg.norm(block, axis=1, keepdims=True)
        query_norms[query_norms == 0.0] = 1.0
        scores = (block / query_norms) @ vectors.T
        scores /= vector_norms
        # k-th best score per row (ascending partition: it sits at index n - k).
        thresholds = np.partition(scores, n - k, axis=1)[:, n - k]
        for row, threshold in zip(scores, thresholds, strict=True):
            result.append(_top_k_row(row, threshold, k))
    return result


def _top_k_row(scores: np.ndarray, threshold: np.floating[Any], k: int) -> list[int]:
    """Best `k` ids of one score row, descending, ties broken by the lowest id.

    Every score tied with the k-th best (`threshold`) is kept as a candidate, so the
    tie-break is exact even when the tie straddles the k-th place.
    """
    candidates = np.flatnonzero(scores >= threshold)
    order = np.lexsort((candidates, -scores[candidates]))
    top: list[int] = candidates[order][:k].tolist()
    return top


def measure(
    search_fn: SearchFn,
    queries: np.ndarray,
    k: int,
    *,
    truth: Sequence[Sequence[int]],
) -> BenchResult:
    """Time `search_fn(query, k)` once per query; p50/p95 in ms and mean recall@5 vs `truth`."""
    if len(queries) == 0:
        raise ValueError("measure needs at least one query")
    if len(truth) != len(queries):
        raise ValueError(f"truth has {len(truth)} rows for {len(queries)} queries")

    latencies_ms: list[float] = []
    recalls: list[float] = []
    for query, expected in zip(queries, truth, strict=True):
        started = time.perf_counter()
        found = search_fn(query, k)
        latencies_ms.append((time.perf_counter() - started) * 1000.0)
        hits = {int(i) for i in list(found)[:RECALL_AT]} & {int(i) for i in expected[:RECALL_AT]}
        recalls.append(len(hits) / RECALL_AT)

    return BenchResult(
        p50_ms=float(np.percentile(latencies_ms, 50)),
        p95_ms=float(np.percentile(latencies_ms, 95)),
        recall_at_5=float(np.mean(recalls)),
    )


def _vector_literal(row: np.ndarray) -> str:
    """pgvector text input, `[x,y,...]`, with the shortest round-trip repr of each value."""
    return "[" + ",".join(repr(value) for value in row.tolist()) + "]"


async def load_bench_table(session: AsyncSession, vectors: np.ndarray) -> None:
    """Bulk-load `vectors` into `bench_vectors(embedding)` with psycopg 3 `COPY ... FROM STDIN`.

    The table comes from migration 0006 (phase 05) and is never created here. The rows are
    written inside the session's current transaction; the caller commits. Requires the
    psycopg driver (`postgresql+psycopg://`), whose async connection exposes `cursor().copy`.
    """
    if vectors.ndim != 2:
        raise ValueError(f"vectors must be a 2-D matrix, got shape {vectors.shape}")
    connection = await session.connection()
    raw = await connection.get_raw_connection()
    driver: Any = raw.driver_connection
    async with driver.cursor() as cursor, cursor.copy(BENCH_COPY_SQL) as copy:
        for row in vectors:
            await copy.write_row((_vector_literal(row),))
