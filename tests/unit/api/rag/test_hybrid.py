from typing import Any

import pytest

from api.config import get_settings
from api.messages import Lang
from api.rag import hybrid
from api.rag.fulltext import ScoredChunk
from api.rag.hybrid import rrf_fuse, search_hybrid


def _ranking(*chunk_ids: int) -> list[ScoredChunk]:
    """A best-first ranking; the input scores are irrelevant to RRF."""
    return [ScoredChunk(chunk_id=chunk_id, score=100.0 - n) for n, chunk_id in enumerate(chunk_ids)]


def _ids(chunks: list[ScoredChunk]) -> list[int]:
    return [chunk.chunk_id for chunk in chunks]


def test_item_first_in_both_rankings_is_first_in_the_fused_result() -> None:
    fused = rrf_fuse([_ranking(7, 1, 2, 3), _ranking(7, 4, 5, 6)], k=5)

    assert fused[0].chunk_id == 7


def test_item_present_in_only_one_ranking_still_reaches_the_top_k() -> None:
    fused = rrf_fuse([_ranking(1, 2, 3), _ranking(9)], k=3)

    assert 9 in _ids(fused)


def test_scores_follow_the_exact_rrf_formula_with_rrf_k_60() -> None:
    fused = rrf_fuse([_ranking(10, 20, 30), _ranking(30, 10)], k=10)

    scores = {chunk.chunk_id: chunk.score for chunk in fused}
    assert scores[10] == pytest.approx(1 / 61 + 1 / 62, rel=1e-12)
    assert scores[30] == pytest.approx(1 / 63 + 1 / 61, rel=1e-12)
    assert scores[20] == pytest.approx(1 / 62, rel=1e-12)
    assert _ids(fused) == [10, 30, 20]
    assert all(isinstance(chunk.score, float) for chunk in fused)


def test_custom_rrf_k_changes_the_constant() -> None:
    fused = rrf_fuse([_ranking(1, 2)], k=2, rrf_k=1)

    assert [chunk.score for chunk in fused] == pytest.approx([1 / 2, 1 / 3])


def test_a_chunk_repeated_in_one_ranking_counts_only_its_first_position() -> None:
    fused = rrf_fuse([_ranking(5, 6, 5, 5)], k=5)

    assert _ids(fused) == [5, 6]
    assert fused[0].score == pytest.approx(1 / 61)
    assert fused[1].score == pytest.approx(1 / 62)


def test_ties_prefer_the_best_individual_rank_then_the_lowest_chunk_id() -> None:
    # 8 and 3 are both at rank 1 of one list (same score): lowest id first.
    assert _ids(rrf_fuse([_ranking(8), _ranking(3)], k=2)) == [3, 8]
    # Both chunks score 1/61 + 1/63 (ranks 1 and 3): the tie stays exact, lowest id first.
    assert _ids(rrf_fuse([_ranking(4, 0, 2), _ranking(2, 0, 4)], k=2)) == [2, 4]
    # rrf_k=1: ranks (2, 2) and (1, 5) both sum to 2/3, but 11 holds rank 1.
    fused = rrf_fuse([_ranking(11, 12), _ranking(1, 12, 2, 3, 11)], k=3, rrf_k=1)
    assert fused[0].score == fused[1].score
    assert _ids(fused)[:2] == [11, 12]


def test_k_limits_the_result_size() -> None:
    rankings = [_ranking(1, 2, 3, 4), _ranking(5, 6, 7)]

    assert len(rrf_fuse(rankings, k=2)) == 2
    assert len(rrf_fuse(rankings, k=100)) == 7


@pytest.mark.parametrize("k", [0, -1])
def test_k_not_positive_returns_nothing(k: int) -> None:
    assert rrf_fuse([_ranking(1, 2)], k=k) == []


def test_empty_rankings_return_nothing() -> None:
    assert rrf_fuse([], k=5) == []
    assert rrf_fuse([[], []], k=5) == []


@pytest.mark.parametrize("rrf_k", [0, -60])
def test_rrf_k_not_positive_is_rejected(rrf_k: int) -> None:
    with pytest.raises(ValueError, match="rrf_k"):
        rrf_fuse([_ranking(1)], k=5, rrf_k=rrf_k)


class _FakeSearches:
    """Stand-ins for `search_fulltext` / `search_hnsw` that record their calls."""

    def __init__(self, text: list[ScoredChunk], vector: list[ScoredChunk]) -> None:
        self.text = text
        self.vector = vector
        self.text_calls: list[tuple[str, Lang, int]] = []
        self.vector_calls: list[tuple[list[float], Lang, int]] = []

    async def fulltext(self, _: Any, query: str, language: Lang, k: int) -> list[ScoredChunk]:
        self.text_calls.append((query, language, k))
        return self.text[:k]

    async def hnsw(
        self, _: Any, query_vector: list[float], language: Lang, k: int
    ) -> list[ScoredChunk]:
        self.vector_calls.append((query_vector, language, k))
        return self.vector[:k]


def _install(
    monkeypatch: pytest.MonkeyPatch, text: list[ScoredChunk], vector: list[ScoredChunk]
) -> _FakeSearches:
    fakes = _FakeSearches(text, vector)
    monkeypatch.setattr(hybrid, "search_fulltext", fakes.fulltext)
    monkeypatch.setattr(hybrid, "search_hnsw", fakes.hnsw)
    return fakes


async def test_search_hybrid_asks_both_searches_for_twice_k_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fakes = _install(monkeypatch, _ranking(1, 2, 3), _ranking(3, 4))

    fused = await search_hybrid(None, "cancel plan", [0.1, 0.2], "en", 3)  # type: ignore[arg-type]

    assert fakes.text_calls == [("cancel plan", "en", 6)]
    assert fakes.vector_calls == [([0.1, 0.2], "en", 6)]
    assert _ids(fused) == [3, 1, 2]  # 2 and 4 tie at rank 2: lowest id


async def test_search_hybrid_uses_rrf_k_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _ranking(1), _ranking(2, 1))
    settings = get_settings().model_copy(update={"rrf_k": 1})
    monkeypatch.setattr(hybrid, "get_settings", lambda: settings)

    fused = await search_hybrid(None, "q", [1.0], "pt", 5)  # type: ignore[arg-type]

    assert _ids(fused) == [1, 2]
    assert [chunk.score for chunk in fused] == pytest.approx([1 / 2 + 1 / 3, 1 / 2])


async def test_search_hybrid_without_text_hits_keeps_the_vector_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, [], _ranking(9, 4, 7, 1))

    fused = await search_hybrid(None, "q", [1.0], "en", 3)  # type: ignore[arg-type]

    assert _ids(fused) == [9, 4, 7]


async def test_search_hybrid_without_vector_hits_keeps_the_text_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, _ranking(9, 4, 7, 1), [])

    fused = await search_hybrid(None, "q", [1.0], "en", 3)  # type: ignore[arg-type]

    assert _ids(fused) == [9, 4, 7]


@pytest.mark.parametrize("k", [0, -2])
async def test_search_hybrid_with_k_not_positive_searches_nothing(
    monkeypatch: pytest.MonkeyPatch, k: int
) -> None:
    fakes = _install(monkeypatch, _ranking(1), _ranking(1))

    assert await search_hybrid(None, "q", [1.0], "en", k) == []  # type: ignore[arg-type]
    assert fakes.text_calls == [] and fakes.vector_calls == []
