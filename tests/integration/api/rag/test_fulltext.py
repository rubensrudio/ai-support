import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from api.messages import Lang
from api.rag.fulltext import search_fulltext
from api.rag.models import TS_CONFIGS, Chunk, Document

pytestmark = pytest.mark.integration


async def _document(
    session: AsyncSession, language: Lang, doc_key: str, status: str = "indexed"
) -> Document:
    document = Document(
        doc_key=doc_key,
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


async def _chunk(session: AsyncSession, document: Document, content: str, ordinal: int = 0) -> int:
    chunk = Chunk(
        document_id=document.id,
        ordinal=ordinal,
        content=content,
        language=document.language,
        token_count=len(content.split()),
        ts_config=TS_CONFIGS[document.language],
    )
    session.add(chunk)
    await session.flush()
    return chunk.id


async def test_portuguese_query_matches_stems_but_not_synonyms(db_session: AsyncSession) -> None:
    document = await _document(db_session, "pt", "plans")
    cancel = await _chunk(db_session, document, "Para cancelar o seu plano, abra o painel.", 0)
    synonym = await _chunk(
        db_session, document, "Você pode encerrar a assinatura a qualquer hora.", 1
    )

    found = [hit.chunk_id for hit in await search_fulltext(db_session, "cancelar plano", "pt", 5)]

    assert found == [cancel]
    assert synonym not in found


async def test_english_query_matches_stems_but_not_synonyms(db_session: AsyncSession) -> None:
    document = await _document(db_session, "en", "plans")
    cancel = await _chunk(db_session, document, "To cancel your plans, open the dashboard.", 0)
    synonym = await _chunk(db_session, document, "You can end the subscription at any time.", 1)

    found = [hit.chunk_id for hit in await search_fulltext(db_session, "cancel plan", "en", 5)]

    assert found == [cancel]
    assert synonym not in found


async def test_natural_english_question_retrieves_the_relevant_chunk(
    db_session: AsyncSession,
) -> None:
    document = await _document(db_session, "en", "refund-times")
    relevant = await _chunk(
        db_session, document, "Refunds reach your card within five business days.", 0
    )
    partial = await _chunk(db_session, document, "You can update the card on file at any time.", 1)
    unrelated = await _chunk(db_session, document, "Our office is closed on public holidays.", 2)

    hits = await search_fulltext(
        db_session, "How long does a refund take to reach my card?", "en", 5
    )

    found = [hit.chunk_id for hit in hits]
    assert found[0] == relevant
    assert partial in found
    assert unrelated not in found
    assert hits[0].score > hits[1].score


async def test_natural_portuguese_question_retrieves_the_relevant_chunk(
    db_session: AsyncSession,
) -> None:
    document = await _document(db_session, "pt", "prazos-reembolso")
    relevant = await _chunk(
        db_session, document, "O reembolso chega ao seu cartão em até cinco dias úteis.", 0
    )
    unrelated = await _chunk(db_session, document, "O escritório fecha nos feriados.", 1)

    hits = await search_fulltext(
        db_session, "Quanto tempo demora para o reembolso chegar no meu cartão?", "pt", 5
    )

    found = [hit.chunk_id for hit in hits]
    assert found[0] == relevant
    assert unrelated not in found


async def test_terms_are_ored_but_synonyms_still_do_not_match(db_session: AsyncSession) -> None:
    document = await _document(db_session, "en", "plan-changes")
    both = await _chunk(db_session, document, "Cancel the plan from the billing page.", 0)
    one = await _chunk(db_session, document, "Compare every plan side by side.", 1)
    synonym = await _chunk(db_session, document, "You can end the subscription at any time.", 2)

    found = [hit.chunk_id for hit in await search_fulltext(db_session, "cancel plan", "en", 5)]

    assert found == [both, one]
    assert synonym not in found


async def test_quoted_phrase_still_requires_adjacent_terms(db_session: AsyncSession) -> None:
    document = await _document(db_session, "en", "phrases")
    phrase = await _chunk(db_session, document, "Gift cards never expire.", 0)
    await _chunk(db_session, document, "Cards and gifts ship separately.", 1)

    found = [hit.chunk_id for hit in await search_fulltext(db_session, '"gift card"', "en", 5)]

    assert found == [phrase]


async def test_excluded_term_still_filters_results(db_session: AsyncSession) -> None:
    document = await _document(db_session, "en", "exclusions")
    kept = await _chunk(db_session, document, "Refunds go back to the original wallet.", 0)
    await _chunk(db_session, document, "Refunds go back to the original card.", 1)
    await _chunk(db_session, document, "Our office is closed on public holidays.", 2)

    found = [hit.chunk_id for hit in await search_fulltext(db_session, "refund -card", "en", 5)]

    assert found == [kept]


async def test_only_chunks_of_the_query_language_are_returned(db_session: AsyncSession) -> None:
    pt = await _document(db_session, "pt", "refunds")
    en = await _document(db_session, "en", "refunds")
    pt_chunk = await _chunk(db_session, pt, "O reembolso cai em cinco dias úteis.")
    await _chunk(db_session, en, "A reembolso is what Brazilian customers call a refund.")

    found = [hit.chunk_id for hit in await search_fulltext(db_session, "reembolso", "pt", 5)]

    assert found == [pt_chunk]


async def test_results_are_ordered_by_relevance_and_limited_to_k(
    db_session: AsyncSession,
) -> None:
    document = await _document(db_session, "en", "invoices")
    weak = await _chunk(db_session, document, "Invoice history lives in the billing area.", 0)
    strong = await _chunk(
        db_session, document, "Invoice details: each invoice lists the invoice total.", 1
    )
    medium = await _chunk(db_session, document, "Download an invoice, then share the invoice.", 2)

    hits = await search_fulltext(db_session, "invoice", "en", 2)

    assert [hit.chunk_id for hit in hits] == [strong, medium]
    assert hits[0].score >= hits[1].score > 0
    assert weak not in [hit.chunk_id for hit in hits]


@pytest.mark.parametrize("status", ["quarantined", "failed"])
async def test_chunks_of_documents_not_indexed_are_excluded(
    db_session: AsyncSession, status: str
) -> None:
    indexed = await _document(db_session, "en", "shipping")
    hidden = await _document(db_session, "en", "shipping-draft", status=status)
    visible = await _chunk(db_session, indexed, "Shipping takes three days.")
    await _chunk(db_session, hidden, "Shipping takes three days, maybe.")

    found = [hit.chunk_id for hit in await search_fulltext(db_session, "shipping", "en", 5)]

    assert found == [visible]


@pytest.mark.parametrize("query", ["", "   ", "the of and"])
async def test_query_without_search_terms_returns_nothing(
    db_session: AsyncSession, query: str
) -> None:
    document = await _document(db_session, "en", "misc")
    await _chunk(db_session, document, "The terms of service and the privacy policy.")

    assert await search_fulltext(db_session, query, "en", 5) == []
