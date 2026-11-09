from collections.abc import AsyncIterator

import numpy as np
import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.config import Settings, get_settings
from api.db.engine import get_sessionmaker
from api.messages import Lang
from api.rag.embeddings import FakeEmbeddingClient
from api.rag.models import TS_CONFIGS, Chunk, Document
from api.rag.retriever import Retriever
from api.rag.vector_bruteforce import search_bruteforce

pytestmark = pytest.mark.integration

DOC_PREFIX = "vector-bruteforce-test-"
SYNONYMS = {
    "cancelar": "encerrar",
    "plano": "assinatura",
    "cancel": "end",
    "plan": "subscription",
}
PT_DISTRACTORS = [
    "O reembolso aparece na fatura em cinco dias úteis.",
    "Nosso escritório fecha nos feriados nacionais.",
    "Atualize o cartão de crédito no painel de pagamentos.",
    "A garantia do produto vale por um ano.",
    "Envie a nota fiscal para o setor financeiro.",
    "O suporte responde por chat em horário comercial.",
]
EN_DISTRACTORS = [
    "Refunds reach your card within five business days.",
    "Our office is closed on public holidays.",
    "Update the credit card on file in the billing panel.",
    "The product warranty lasts for one year.",
    "Send the invoice to the finance team.",
    "Support answers by chat during business hours.",
]


@pytest.fixture
def embedder() -> FakeEmbeddingClient:
    return FakeEmbeddingClient(get_settings().embedding_dimensions, synonyms=SYNONYMS)


async def _document(
    session: AsyncSession, language: Lang, doc_key: str, status: str = "indexed"
) -> Document:
    document = Document(
        doc_key=f"{DOC_PREFIX}{doc_key}",
        title=doc_key.title(),
        language=language,
        doc_type="manual",
        format="md",
        source_path=f"corpus/{language}/{doc_key}.md",
        content_hash="h",
        status=status,
    )
    session.add(document)
    await session.flush()
    return document


async def _chunk(
    session: AsyncSession,
    document: Document,
    content: str,
    embedder: FakeEmbeddingClient | None,
    ordinal: int = 0,
    section: str | None = None,
    page: int | None = None,
) -> int:
    chunk = Chunk(
        document_id=document.id,
        ordinal=ordinal,
        content=content,
        language=document.language,
        section=section,
        page=page,
        token_count=len(content.split()),
        ts_config=TS_CONFIGS[document.language],
        embedding=None if embedder is None else embedder.vector(content),
    )
    session.add(chunk)
    await session.flush()
    return chunk.id


async def _seed_with_distractors(
    session: AsyncSession,
    embedder: FakeEmbeddingClient,
    language: Lang,
    target: str,
    distractors: list[str],
) -> int:
    document = await _document(session, language, f"plans-{language}")
    for ordinal, content in enumerate(distractors[:3]):
        await _chunk(session, document, content, embedder, ordinal)
    target_id = await _chunk(session, document, target, embedder, 3)
    for ordinal, content in enumerate(distractors[3:], start=4):
        await _chunk(session, document, content, embedder, ordinal)
    return target_id


async def test_portuguese_synonym_query_finds_the_chunk_in_the_top_5(
    db_session: AsyncSession, embedder: FakeEmbeddingClient
) -> None:
    target = await _seed_with_distractors(
        db_session,
        embedder,
        "pt",
        "Você pode encerrar a assinatura a qualquer hora.",
        PT_DISTRACTORS,
    )

    hits = await search_bruteforce(db_session, embedder.vector("cancelar plano"), "pt", 5)

    assert len(hits) == 5
    assert target in [hit.chunk_id for hit in hits]
    assert hits[0].chunk_id == target


@pytest.mark.parametrize("query", ["cancel plan", "end subscription"])
async def test_english_synonym_query_finds_the_chunk_in_the_top_5(
    db_session: AsyncSession, embedder: FakeEmbeddingClient, query: str
) -> None:
    target = await _seed_with_distractors(
        db_session,
        embedder,
        "en",
        "You can end the subscription at any time.",
        EN_DISTRACTORS,
    )

    hits = await search_bruteforce(db_session, embedder.vector(query), "en", 5)

    assert len(hits) == 5
    assert hits[0].chunk_id == target


async def test_scores_are_cosine_similarities_in_descending_order(
    db_session: AsyncSession, embedder: FakeEmbeddingClient
) -> None:
    await _seed_with_distractors(
        db_session, embedder, "en", "You can end the subscription at any time.", EN_DISTRACTORS
    )
    query = "Our office is closed on public holidays."
    # Unnormalised query: cosine must not depend on the query norm.
    query_vector = [3.0 * x for x in embedder.vector(query)]

    hits = await search_bruteforce(db_session, query_vector, "en", 10)

    scores = [hit.score for hit in hits]
    assert len(hits) == len(EN_DISTRACTORS) + 1
    assert scores == sorted(scores, reverse=True)
    assert scores[0] == pytest.approx(1.0, abs=1e-5)
    assert all(-1.0 - 1e-6 <= score <= 1.0 + 1e-6 for score in scores)
    assert all(isinstance(score, float) for score in scores)


async def test_only_chunks_of_the_requested_language_are_searched(
    db_session: AsyncSession, embedder: FakeEmbeddingClient
) -> None:
    pt_document = await _document(db_session, "pt", "billing")
    en_document = await _document(db_session, "en", "billing")
    pt_chunk = await _chunk(db_session, pt_document, "fatura invoice", embedder)
    en_chunk = await _chunk(db_session, en_document, "fatura invoice", embedder)

    pt_hits = await search_bruteforce(db_session, embedder.vector("invoice"), "pt", 5)
    en_hits = await search_bruteforce(db_session, embedder.vector("invoice"), "en", 5)

    assert [hit.chunk_id for hit in pt_hits] == [pt_chunk]
    assert [hit.chunk_id for hit in en_hits] == [en_chunk]


async def test_documents_not_indexed_and_chunks_without_embedding_are_skipped(
    db_session: AsyncSession, embedder: FakeEmbeddingClient
) -> None:
    indexed = await _document(db_session, "en", "indexed")
    quarantined = await _document(db_session, "en", "quarantined", status="quarantined")
    failed = await _document(db_session, "en", "failed", status="failed")
    kept = await _chunk(db_session, indexed, "Warranty lasts a year.", embedder, 0)
    await _chunk(db_session, indexed, "Warranty lasts a year.", None, 1)
    await _chunk(db_session, quarantined, "Warranty lasts a year.", embedder)
    await _chunk(db_session, failed, "Warranty lasts a year.", embedder)

    hits = await search_bruteforce(db_session, embedder.vector("warranty"), "en", 10)

    assert [hit.chunk_id for hit in hits] == [kept]


async def test_k_limits_the_results_and_ties_keep_the_lowest_chunk_id_first(
    db_session: AsyncSession, embedder: FakeEmbeddingClient
) -> None:
    document = await _document(db_session, "pt", "ties")
    same = [await _chunk(db_session, document, "prazo de entrega", embedder, n) for n in range(4)]
    await _chunk(db_session, document, "garantia do produto", embedder, 4)

    hits = await search_bruteforce(db_session, embedder.vector("prazo de entrega"), "pt", 2)

    assert [hit.chunk_id for hit in hits] == same[:2]
    assert hits[0].score == hits[1].score


async def test_k_zero_zero_vector_and_empty_index_return_nothing(
    db_session: AsyncSession, embedder: FakeEmbeddingClient
) -> None:
    query = embedder.vector("cancel plan")
    assert await search_bruteforce(db_session, query, "en", 5) == []

    document = await _document(db_session, "en", "plans")
    await _chunk(db_session, document, "You can end the subscription.", embedder)

    assert await search_bruteforce(db_session, query, "en", 0) == []
    assert await search_bruteforce(db_session, query, "en", -1) == []
    zero = np.zeros(len(query)).tolist()
    assert await search_bruteforce(db_session, zero, "en", 5) == []
    assert len(await search_bruteforce(db_session, query, "en", 5)) == 1


@pytest.fixture
async def factory(migrated_database: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    session_factory = get_sessionmaker()
    yield session_factory
    async with session_factory() as session:
        await session.execute(delete(Document).where(Document.doc_key.startswith(DOC_PREFIX)))
        await session.commit()


def _settings(top_k: int = 5) -> Settings:
    return get_settings().model_copy(
        update={"retrieval_mode": "vector_bruteforce", "retrieval_top_k": top_k}
    )


async def test_retriever_hydrates_vector_hits_end_to_end(
    factory: async_sessionmaker[AsyncSession], embedder: FakeEmbeddingClient
) -> None:
    async with factory() as session:
        target = await _seed_with_distractors(
            session,
            embedder,
            "pt",
            "Você pode encerrar a assinatura a qualquer hora.",
            PT_DISTRACTORS,
        )
        other = await _document(session, "en", "plans-en-e2e")
        await _chunk(session, other, "You can end the subscription.", embedder, 0, "Plans", 2)
        await session.commit()
    retriever = Retriever(factory, embedder, _settings(top_k=3))

    chunks = await retriever.retrieve("Como cancelar meu plano?", "pt")

    assert embedder.calls == [["Como cancelar meu plano?"]]
    assert len(chunks) == 3
    first = chunks[0]
    assert first.chunk_id == target
    assert first.document_title == "Plans-Pt"
    assert first.doc_key == f"{DOC_PREFIX}plans-pt"
    assert (first.language, first.section, first.page) == ("pt", None, None)
    assert first.content == "Você pode encerrar a assinatura a qualquer hora."
    assert all(chunk.language == "pt" for chunk in chunks)
    scores = [chunk.score for chunk in chunks]
    assert scores == sorted(scores, reverse=True)
