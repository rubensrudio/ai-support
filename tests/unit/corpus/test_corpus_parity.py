"""Portuguese corpus mirrors the English one (TASK-041, RAG-05, RAG-14, TOOL-16, TOOL-19)."""

import re
from collections import Counter

import pytest
from corpus_helpers import (
    CORPUS,
    INJECTION_MARKERS,
    LANGUAGES,
    by_key,
    documents,
    headings,
    source_text,
)

# Every stem a full-text query for "cancelar plano" would match (portuguese snowball).
CANCEL_OR_PLAN = re.compile(r"cancel|\bplan", re.IGNORECASE)
END_SUBSCRIPTION_PT = re.compile(r"\bencerr\w*\s+(?:a\s+|sua\s+|uma\s+)?assinatura", re.I)
NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
DOC_KEYS = [doc["doc_key"] for doc in documents()]


def _numbers(text: str, language: str) -> Counter[str]:
    found = NUMBER.findall(text)
    if language == "pt":  # pt-BR writes decimals with a comma: USD 19,90
        found = [number.replace(",", ".") for number in found]
    return Counter(found)


def test_every_doc_key_has_a_source_in_both_languages() -> None:
    for doc in documents():
        for language in LANGUAGES:
            source = doc["source"][language]
            assert source == f"{language}/{doc['doc_key']}.md", doc["doc_key"]
            assert (CORPUS / source).is_file(), source


def test_no_orphan_source_files() -> None:
    for language in LANGUAGES:
        on_disk = {path.stem for path in (CORPUS / language).glob("*.md")}
        assert on_disk == set(DOC_KEYS), language


@pytest.mark.parametrize("doc_key", DOC_KEYS)
def test_portuguese_h1_is_the_manifest_title(doc_key: str) -> None:
    doc = by_key()[doc_key]
    assert headings(source_text(doc, "pt"))[0] == (1, doc["title"]["pt"])


@pytest.mark.parametrize("doc_key", DOC_KEYS)
def test_same_section_structure_in_both_languages(doc_key: str) -> None:
    doc = by_key()[doc_key]
    en = [level for level, _ in headings(source_text(doc, "en"))]
    pt = [level for level, _ in headings(source_text(doc, "pt"))]
    assert en == pt


@pytest.mark.parametrize("doc_key", DOC_KEYS)
def test_same_numbers_in_both_languages(doc_key: str) -> None:
    doc = by_key()[doc_key]
    assert _numbers(source_text(doc, "pt"), "pt") == _numbers(source_text(doc, "en"), "en")


def test_end_subscription_uses_only_encerrar_assinatura_wording() -> None:
    text = (CORPUS / "pt" / "end-subscription.md").read_text(encoding="utf-8")
    assert "encerrar assinatura" in text.lower()
    assert "cancelar plano" not in text.lower()
    # RAG-05: the keyword search for "cancelar plano" must not reach this document at all.
    assert not CANCEL_OR_PLAN.findall(text)


def test_only_end_subscription_talks_about_ending_a_subscription() -> None:
    for doc in documents():
        if doc["doc_key"] != "end-subscription":
            assert not END_SUBSCRIPTION_PT.search(source_text(doc, "pt")), doc["doc_key"]


def test_refund_policy_in_portuguese_states_the_rule_in_usd() -> None:
    text = (CORPUS / "pt" / "refund-policy.md").read_text(encoding="utf-8")
    assert "7 dias" in text
    assert "USD" in text
    assert "cancelado antes do envio" in text
    assert "entregue há no máximo 7 dias" in text
    assert "valor integral do pedido" in text


def test_portuguese_money_is_always_usd() -> None:
    for doc in documents():
        text = source_text(doc, "pt")
        assert "$" not in text, doc["doc_key"]
        assert not re.search(r"\b(?:BRL|EUR|reais)\b", text), doc["doc_key"]


def test_injection_wording_appears_only_in_the_malicious_document() -> None:
    for doc in documents():
        text = source_text(doc, "pt").lower()
        found = [marker for marker in INJECTION_MARKERS if marker in text]
        if doc["malicious"]:
            assert len(found) >= 2, doc["doc_key"]
        else:
            assert not found, f"{doc['doc_key']}: {found}"
