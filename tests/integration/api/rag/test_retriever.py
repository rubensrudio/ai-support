from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.config import RetrievalMode, Settings, get_settings
from api.db.engine import get_sessionmaker
from api.messages import Lang
from api.rag import retriever as retriever_module
from api.rag.fulltext import ScoredChunk
from api.rag.models import TS_CONFIGS, Chunk, Document
from api.rag.retriever import RetrievalUnavailable, Retriever, register_mode

pytestmark = pytest.mark.integration

DOC_PREFIX = "retriever-test-"


def _settings(mode: RetrievalMode = "fulltext", top_k: int = 5) -> Settings:
    return get_settings().model_copy(update={"retrieval_mode": mode, "retrieval_top_k": top_k})


@pytest.fixture
async def factory(migrated_database: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    session_factory = get_sessionmaker()
    yield session_factory
    async with session_factory() as session:
        await session.execute(delete(Document).where(Document.doc_key.startswith(DOC_PREFIX)))
        await session.commit()


@pytest.fixture
def isolated_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retriever_module, "_SEARCHERS", dict(retriever_module._SEARCHERS))


async def _seed(
    factory: async_sessionmaker[AsyncSession],
    language: Lang,
    doc_key: str,
    title: str,
    chunks: list[tuple[str, str | None, int | None]],
    status: str = "indexed",
) -> list[int]:
    async with factory() as session:
        document = Document(
            doc_key=f"{DOC_PREFIX}{doc_key}",
            title=title,
            language=language,
            doc_type="manual",
            format="pdf",
            source_path=f"corpus/pdf/{doc_key}.pdf",
            content_hash="h",
            status=status,
        )
        session.add(document)
        await session.flush()
        rows = [
            Chunk(
                document_id=document.id,
                ordinal=ordinal,
                content=content,
                language=language,
                section=section,
                page=page,
                token_count=len(content.split()),
                ts_config=TS_CONFIGS[language],
            )
            for ordinal, (content, section, page) in enumerate(chunks)
        ]
        session.add_all(rows)
        await session.commit()
        return [row.id for row in rows]


class _ExplodingFactory:
    """Session factory that fails the test if the retriever ever opens a session."""

    def __call__(self) -> AsyncSession:
        raise AssertionError("the database must not be queried")


async def test_empty_index_returns_no_chunks(factory: async_sessionmaker[AsyncSession]) -> None:
    retriever = Retriever(factory, None, _settings())

    assert await retriever.retrieve("how do I cancel my plan?", "en") == []


async def test_mode_none_returns_nothing_without_touching_the_database() -> None:
    retriever = Retriever(_ExplodingFactory(), None, _settings(mode="none"))  # type: ignore[arg-type]

    assert await retriever.retrieve("reembolso", "pt") == []
    assert await retriever.retrieve("refund", "en", mode="none") == []


async def test_fulltext_hydrates_chunks_in_relevance_order(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    weak, strong = await _seed(
        factory,
        "pt",
        "refunds",
        "Política de reembolso",
        [
            ("O reembolso aparece na fatura.", "Prazos", 3),
            (
                "Reembolso: o reembolso integral sai em cinco dias; reembolso parcial também.",
                None,
                4,
            ),
        ],
    )
    await _seed(factory, "en", "refunds", "Refund policy", [("A reembolso, i.e. refund.", None, 1)])
    retriever = Retriever(factory, None, _settings(mode="none"))

    chunks = await retriever.retrieve("reembolso", "pt", mode="fulltext")

    assert [chunk.chunk_id for chunk in chunks] == [strong, weak]
    first, second = chunks
    assert first.document_title == "Política de reembolso"
    assert first.doc_key == f"{DOC_PREFIX}refunds"
    assert (first.section, first.page, first.language) == (None, 4, "pt")
    assert first.content.startswith("Reembolso: o reembolso integral")
    assert (second.section, second.page) == ("Prazos", 3)
    assert first.score >= second.score > 0


async def test_k_defaults_to_settings_and_can_be_overridden(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(
        factory,
        "en",
        "invoices",
        "Invoices",
        [(f"Invoice note number {n}.", None, None) for n in range(4)],
    )
    retriever = Retriever(factory, None, _settings(top_k=3))

    assert len(await retriever.retrieve("invoice", "en")) == 3
    assert len(await retriever.retrieve("invoice", "en", k=1)) == 1


async def test_documents_not_indexed_are_not_retrieved(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(
        factory, "en", "draft", "Draft", [("Warranty lasts a year.", None, None)], "quarantined"
    )
    retriever = Retriever(factory, None, _settings())

    assert await retriever.retrieve("warranty", "en") == []


@pytest.mark.parametrize("mode", ["vector_bruteforce", "vector_hnsw", "hybrid"])
async def test_mode_without_searcher_is_unavailable(
    factory: async_sessionmaker[AsyncSession], mode: RetrievalMode
) -> None:
    retriever = Retriever(factory, None, _settings(mode=mode))

    with pytest.raises(RetrievalUnavailable, match=mode):
        await retriever.retrieve("refund", "en")


@pytest.mark.usefixtures("isolated_registry")
async def test_searcher_failure_becomes_retrieval_unavailable(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async def broken(
        session: AsyncSession, query: str, language: Lang, k: int
    ) -> list[ScoredChunk]:
        raise OperationalError("SELECT secret FROM chunks", {}, Exception("connection refused"))

    register_mode("fulltext", broken)
    retriever = Retriever(factory, None, _settings())

    with pytest.raises(RetrievalUnavailable) as caught:
        await retriever.retrieve("refund", "en")

    assert "SELECT" not in str(caught.value)
    assert "connection refused" not in str(caught.value)


@pytest.mark.usefixtures("isolated_registry")
async def test_registered_mode_is_dispatched(factory: async_sessionmaker[AsyncSession]) -> None:
    (chunk_id,) = await _seed(factory, "en", "custom", "Custom", [("Anything.", "Intro", 2)])
    calls: list[tuple[str, Lang, int]] = []

    async def fake(session: AsyncSession, query: str, language: Lang, k: int) -> list[ScoredChunk]:
        calls.append((query, language, k))
        return [ScoredChunk(chunk_id=chunk_id, score=0.42)]

    register_mode("hybrid", fake)
    retriever = Retriever(factory, None, _settings(mode="hybrid", top_k=7))

    chunks = await retriever.retrieve("question", "en")

    assert calls == [("question", "en", 7)]
    assert [(chunk.chunk_id, chunk.section, chunk.score) for chunk in chunks] == [
        (chunk_id, "Intro", 0.42)
    ]


async def test_database_failure_becomes_retrieval_unavailable(migrated_database: str) -> None:
    def broken_factory() -> AsyncSession:
        raise OperationalError("SELECT 1", {}, Exception("server closed the connection"))

    retriever = Retriever(broken_factory, None, _settings())  # type: ignore[arg-type]

    with pytest.raises(RetrievalUnavailable) as caught:
        await retriever.retrieve("refund", "en")

    assert "SELECT" not in str(caught.value)
