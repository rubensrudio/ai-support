from collections.abc import Sequence
from types import TracebackType
from typing import Any, cast

import numpy as np
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from demos.bench import BenchResult, exact_top_k, generate_vectors, load_bench_table, measure


def test_generate_vectors_is_deterministic_and_normalised() -> None:
    first = generate_vectors(1000, 8, seed=1)
    second = generate_vectors(1000, 8, seed=1)

    assert first.shape == (1000, 8)
    assert first.dtype == np.float32
    np.testing.assert_array_equal(first, second)
    norms = np.linalg.norm(first.astype(np.float64), axis=1)
    assert np.all(np.abs(norms - 1.0) <= 1e-6)


def test_generate_vectors_differs_across_seeds() -> None:
    assert not np.array_equal(generate_vectors(10, 4, seed=1), generate_vectors(10, 4, seed=2))


@pytest.mark.parametrize(("n", "dim"), [(0, 8), (8, 0), (-1, 8), (8, -3)])
def test_generate_vectors_rejects_non_positive_sizes(n: int, dim: int) -> None:
    with pytest.raises(ValueError):
        generate_vectors(n, dim, seed=1)


def test_exact_top_k_matches_hand_computed_cosine_ranking() -> None:
    vectors = np.array(
        [
            [1.0, 0.0],  # 0
            [0.0, 1.0],  # 1
            [1.0, 1.0],  # 2: cos 0.707 to both axes
            [-1.0, 0.0],  # 3
            [3.0, 0.1],  # 4: unnormalised, cos ~0.9994 to x axis
        ],
        dtype=np.float32,
    )
    queries = np.array([[1.0, 0.0], [0.0, 2.0]], dtype=np.float32)

    # query x: 0 (1.0) > 4 (0.9994) > 2 (0.707) > 1 (0.0) > 3 (-1.0)
    # query y: 1 (1.0) > 2 (0.707) > 4 (0.033) > 0 (0.0) > 3 (0.0)
    assert exact_top_k(vectors, queries, 3) == [[0, 4, 2], [1, 2, 4]]


def test_exact_top_k_blocks_give_the_same_answer_as_one_pass() -> None:
    vectors = generate_vectors(500, 16, seed=3)
    queries = generate_vectors(37, 16, seed=4)

    expected = np.argsort(-(queries @ vectors.T), axis=1, kind="stable")[:, :5].tolist()

    assert exact_top_k(vectors, queries, 5, block_size=4) == expected
    assert exact_top_k(vectors, queries, 5) == expected


def test_exact_top_k_caps_k_at_the_number_of_vectors() -> None:
    vectors = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)

    assert exact_top_k(vectors, np.array([[0.0, 1.0]], dtype=np.float32), 10) == [[1, 0]]


def test_exact_top_k_rejects_bad_input() -> None:
    vectors = generate_vectors(10, 4, seed=1)
    with pytest.raises(ValueError):
        exact_top_k(vectors, generate_vectors(2, 4, seed=2), 0)
    with pytest.raises(ValueError):
        exact_top_k(vectors, generate_vectors(2, 3, seed=2), 5)


def _exact_searcher(vectors: np.ndarray) -> Any:
    def search(query: np.ndarray, k: int) -> Sequence[int]:
        return exact_top_k(vectors, query[np.newaxis, :], k)[0]

    return search


def test_measure_exact_search_has_full_recall() -> None:
    vectors = generate_vectors(1000, 8, seed=1)
    queries = generate_vectors(20, 8, seed=2)
    truth = exact_top_k(vectors, queries, 5)

    result = measure(_exact_searcher(vectors), queries, 5, truth=truth)

    assert isinstance(result, BenchResult)
    assert result.recall_at_5 == 1.0
    assert 0.0 <= result.p50_ms <= result.p95_ms


def test_measure_bad_search_has_partial_recall() -> None:
    vectors = generate_vectors(1000, 8, seed=1)
    queries = generate_vectors(20, 8, seed=2)
    truth = exact_top_k(vectors, queries, 5)
    calls: list[int] = []

    def wrong_order(query: np.ndarray, k: int) -> Sequence[int]:
        calls.append(k)
        exact = exact_top_k(vectors, query[np.newaxis, :], 10)[0]
        return exact[:2] + exact[7:10]  # 2 of the true top 5, then 3 misses

    result = measure(wrong_order, queries, 5, truth=truth)

    assert result.recall_at_5 == pytest.approx(0.4)
    assert calls == [5] * 20
    assert result.p50_ms <= result.p95_ms


def test_measure_reports_latency_percentiles_in_milliseconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticks = iter([0.0, 0.001, 1.0, 1.003, 2.0, 2.010])  # 1 ms, 3 ms, 10 ms
    monkeypatch.setattr("demos.bench.time.perf_counter", lambda: next(ticks))
    queries = generate_vectors(3, 2, seed=1)

    result = measure(lambda q, k: [0, 1, 2, 3, 4], queries, 5, truth=[[0, 1, 2, 3, 4]] * 3)

    assert result.p50_ms == pytest.approx(3.0)
    assert result.p95_ms == pytest.approx(float(np.percentile([1.0, 3.0, 10.0], 95)))
    assert result.recall_at_5 == 1.0


def test_measure_rejects_mismatched_truth_and_empty_queries() -> None:
    queries = generate_vectors(3, 2, seed=1)
    with pytest.raises(ValueError):
        measure(lambda q, k: [0], queries, 5, truth=[[0]] * 2)
    with pytest.raises(ValueError):
        measure(lambda q, k: [0], np.empty((0, 2), dtype=np.float32), 5, truth=[])


class _FakeCopy:
    def __init__(self) -> None:
        self.rows: list[tuple[str, ...]] = []

    async def __aenter__(self) -> "_FakeCopy":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    async def write_row(self, row: tuple[str, ...]) -> None:
        self.rows.append(row)


class _FakeCursor:
    def __init__(self, copy: _FakeCopy, statements: list[str]) -> None:
        self._copy = copy
        self._statements = statements

    async def __aenter__(self) -> "_FakeCursor":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def copy(self, statement: str) -> _FakeCopy:
        self._statements.append(statement)
        return self._copy


class _FakeDriver:
    def __init__(self, copy: _FakeCopy, statements: list[str]) -> None:
        self._copy = copy
        self._statements = statements

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._copy, self._statements)


class _FakeRaw:
    def __init__(self, driver: _FakeDriver) -> None:
        self.driver_connection = driver


class _FakeConnection:
    def __init__(self, raw: _FakeRaw) -> None:
        self._raw = raw

    async def get_raw_connection(self) -> _FakeRaw:
        return self._raw


class _FakeSession:
    def __init__(self) -> None:
        self.copy = _FakeCopy()
        self.statements: list[str] = []
        self._connection = _FakeConnection(_FakeRaw(_FakeDriver(self.copy, self.statements)))

    async def connection(self) -> _FakeConnection:
        return self._connection


async def test_load_bench_table_copies_vector_literals() -> None:
    session = _FakeSession()
    vectors = np.array([[0.5, -0.25], [1.0, 0.0]], dtype=np.float32)

    await load_bench_table(cast(AsyncSession, session), vectors)

    assert session.statements == ["COPY bench_vectors (embedding) FROM STDIN"]
    assert session.copy.rows == [("[0.5,-0.25]",), ("[1.0,0.0]",)]


async def test_load_bench_table_rejects_non_matrix() -> None:
    session = _FakeSession()
    with pytest.raises(ValueError):
        await load_bench_table(cast(AsyncSession, session), np.zeros(3, dtype=np.float32))
    assert session.statements == []


def test_exact_top_k_breaks_ties_at_the_k_th_place_by_lowest_id() -> None:
    # ids 0..199 [1, 0] share the top score, then 200..399 [1, 1] tie at 0.707; the k-th
    # place (k=203) falls inside the second tie group, so only its 3 lowest ids may enter.
    vectors = np.array([[1.0, 0.0]] * 200 + [[1.0, 1.0]] * 200, dtype=np.float32)
    queries = np.array([[1.0, 0.0], [1.0, 0.0]], dtype=np.float32)

    expected = list(range(200)) + [200, 201, 202]

    assert exact_top_k(vectors, queries, 203) == [expected, expected]
    assert exact_top_k(vectors, queries, 203, block_size=1) == [expected, expected]
