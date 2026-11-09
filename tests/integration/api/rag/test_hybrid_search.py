from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.config import Settings, get_settings
from api.db.engine import get_sessionmaker
from api.messages import Lang
from api.rag import hybrid
from api.rag.embeddings import FakeEmbeddingClient
from api.rag.fulltext import search_fulltext
from api.rag.hybrid import search_hybrid
from api.rag.models import TS_CONFIGS, Chunk, Document
from api.rag.retriever import Retriever
from api.rag.vector_bruteforce import search_bruteforce

pytestmark = pytest.mark.integration

DOC_PREFIX = "hybrid-search-test-"
SYNONYMS = {"cancel": "end", "plan": "subscription"}
QUERY = "ZX9041 cancel plan"
# Shares only the exact code `ZX9041` with the question; long, so its embedding stays far.
EXACT_TERM = (
    "Error ZX9041 shows up when the router firmware is older than the release shipped "
    "with the mobile app last spring."
)
# Shares no lexeme with the question: only the synonyms (cancel -> end, plan -> subscription)
# bring it close in the embedding space.
SYNONYM = "End the subscription anytime."
# Close to the question in the embedding space (one synonym each), no full-text match: they
# fill the vector candidates (2 * k) so the exact-term chunk is not one of them.
VECTOR_DISTRACTORS = [
    "The subscription renews monthly.",
    "Your subscription includes support.",
    "Trials end after seven days.",
    "Subscription invoices arrive by email.",
    "Promotions end on Friday.",
    "Pause the subscription online.",
    "Support hours end at six.",
    "Upgrade your subscription anytime.",
    "Discounts end next week.",
    "Gift subscription codes expire.",
    "Shipping delays end soon.",
    "Family subscription covers five.",
]
OTHER = [
    "Our office is closed on public holidays.",
    "The product warranty lasts for one year.",
]
K = 5


@pytest.fixture
def embedder() -> FakeEmbeddingClient:
    return FakeEmbeddingClient(get_settings().embedding_dimensions, synonyms=SYNONYMS)


async def _seed(
    session: AsyncSession, embedder: FakeEmbeddingClient, language: Lang = "en"
) -> tuple[int, int]:
    """Seed one indexed document; return the ids of the exact-term and synonym chunks."""
    document = Document(
        doc_key=f"{DOC_PREFIX}support-{language}",
        title="Support",
        language=language,
        doc_type="manual",
        format="md",
        source_path=f"corpus/{language}/support.md",
        content_hash="h",
        status="indexed",
    )
    session.add(document)
    await session.flush()
    contents = [EXACT_TERM, SYNONYM, *VECTOR_DISTRACTORS, *OTHER]
    chunks = [
        Chunk(
            document_id=document.id,
            ordinal=ordinal,
            content=content,
            language=language,
            section="Help",
            page=None,
            token_count=len(content.split()),
            ts_config=TS_CONFIGS[language],
            embedding=embedder.vector(content),
        )
        for ordinal, content in enumerate(contents)
    ]
    session.add_all(chunks)
    await session.flush()
    return chunks[0].id, chunks[1].id


async def test_exact_term_and_synonym_both_reach_the_hybrid_top_5(
    db_session: AsyncSession, embedder: FakeEmbeddingClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Exact cosine stand-in for the HNSW search, so this test does not depend on the index.
    monkeypatch.setattr(hybrid, "search_hnsw", search_bruteforce)
    exact, synonym = await _seed(db_session, embedder)
    query_vector = embedder.vector(QUERY)

    text_ids = [hit.chunk_id for hit in await search_fulltext(db_session, QUERY, "en", 2 * K)]
    vector_ids = [
        hit.chunk_id for hit in await search_bruteforce(db_session, query_vector, "en", 2 * K)
    ]
    # Preconditions: each chunk is found by only one of the two searches.
    assert exact in text_ids and exact not in vector_ids
    assert synonym in vector_ids and synonym not in text_ids

    hits = await search_hybrid(db_session, QUERY, query_vector, "en", K)

    ids = [hit.chunk_id for hit in hits]
    assert len(ids) == K
    assert exact in ids and synonym in ids
    scores = [hit.score for hit in hits]
    assert scores == sorted(scores, reverse=True)


async def test_hybrid_searches_only_the_question_language(
    db_session: AsyncSession, embedder: FakeEmbeddingClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hybrid, "search_hnsw", search_bruteforce)
    await _seed(db_session, embedder, "en")

    assert await search_hybrid(db_session, QUERY, embedder.vector(QUERY), "pt", K) == []


@pytest.fixture
async def factory(migrated_database: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    session_factory = get_sessionmaker()
    yield session_factory
    async with session_factory() as session:
        await session.execute(delete(Document).where(Document.doc_key.startswith(DOC_PREFIX)))
        await session.commit()


def _settings() -> Settings:
    return get_settings().model_copy(update={"retrieval_mode": "hybrid", "retrieval_top_k": K})


async def test_retriever_hybrid_mode_end_to_end_with_hnsw(
    factory: async_sessionmaker[AsyncSession], embedder: FakeEmbeddingClient
) -> None:
    """Real path: `Retriever` -> `search_hybrid` -> full-text + HNSW (no patching)."""
    async with factory() as session:
        exact, synonym = await _seed(session, embedder)
        await session.commit()
    retriever = Retriever(factory, embedder, _settings())

    chunks = await retriever.retrieve(QUERY, "en")

    assert embedder.calls == [[QUERY]]
    ids = [chunk.chunk_id for chunk in chunks]
    assert len(ids) == K
    assert exact in ids and synonym in ids
    by_id = {chunk.chunk_id: chunk for chunk in chunks}
    assert by_id[exact].content == EXACT_TERM
    assert by_id[synonym].content == SYNONYM
    assert by_id[synonym].doc_key == f"{DOC_PREFIX}support-en"
    assert all(chunk.language == "en" for chunk in chunks)
    scores = [chunk.score for chunk in chunks]
    assert scores == sorted(scores, reverse=True)
