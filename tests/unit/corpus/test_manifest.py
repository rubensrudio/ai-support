"""English support corpus and manifest (TASK-040, RAG-02, RAG-14, RAG-16, TOOL-16, TOOL-19)."""

import re
from typing import Any

import pytest
from corpus_helpers import (
    CORPUS,
    INJECTION_MARKERS,
    LANGUAGES,
    by_key,
    documents,
    headings,
    load_manifest,
    source_text,
)

from api.rag.models import DOC_FORMATS, DOC_TYPES

REQUIRED_FIELDS = ("doc_key", "title", "doc_type", "format", "languages", "malicious", "source")
DOC_KEY = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
# Every stem a full-text query for "cancel plan" would match (english snowball: cancel, plan).
CANCEL_OR_PLAN = re.compile(r"cancel|\bplan", re.IGNORECASE)
END_SUBSCRIPTION_EN = re.compile(r"\bend(?:ing|s)?\s+(?:your\s+|the\s+|a\s+)?subscription", re.I)


def _documents() -> list[dict[str, Any]]:
    return documents()


def test_every_item_has_required_fields_and_valid_values() -> None:
    docs = _documents()
    keys = [doc["doc_key"] for doc in docs]
    assert len(keys) == len(set(keys)), "doc_key must be unique"
    for doc in docs:
        missing = [field for field in REQUIRED_FIELDS if field not in doc]
        assert not missing, f"{doc.get('doc_key')}: missing {missing}"
        assert DOC_KEY.match(doc["doc_key"]), doc["doc_key"]
        assert doc["doc_type"] in DOC_TYPES, doc["doc_key"]
        assert doc["format"] in DOC_FORMATS, doc["doc_key"]
        assert list(doc["languages"]) == list(LANGUAGES), doc["doc_key"]
        assert isinstance(doc["malicious"], bool), doc["doc_key"]
        for language in LANGUAGES:
            assert str(doc["title"][language]).strip(), f"{doc['doc_key']}: title.{language}"


def test_corpus_has_about_twenty_documents() -> None:
    assert 18 <= len(_documents()) <= 24  # LAC-37: ~20 documents per language


def test_every_item_has_an_english_source_file() -> None:
    for doc in _documents():
        source = doc["source"]["en"]
        assert source == f"en/{doc['doc_key']}.md", doc["doc_key"]
        assert (CORPUS / source).is_file(), source


def test_ingested_file_path_follows_the_format() -> None:
    for doc in _documents():
        for language in LANGUAGES:
            file = doc["file"][language]
            if doc["format"] == "md":
                assert file == doc["source"][language], doc["doc_key"]
            else:
                assert file == f"pdf/{language}/{doc['doc_key']}.pdf", doc["doc_key"]


def test_manifest_covers_three_types_and_two_formats() -> None:
    docs = _documents()
    assert {doc["doc_type"] for doc in docs} == set(DOC_TYPES)
    assert {doc["format"] for doc in docs} == set(DOC_FORMATS)


def test_exactly_one_malicious_item_and_it_is_a_pdf() -> None:
    malicious = [doc for doc in _documents() if doc["malicious"]]
    assert len(malicious) == 1
    assert malicious[0]["format"] == "pdf"


def test_synonym_pair_is_documented() -> None:
    pairs = load_manifest()["synonyms"]
    assert {
        "en": ["cancel plan", "end subscription"],
        "pt": ["cancelar plano", "encerrar assinatura"],
    } in pairs


@pytest.mark.parametrize("doc_key", [doc["doc_key"] for doc in documents()])
def test_english_h1_is_the_manifest_title(doc_key: str) -> None:
    doc = by_key()[doc_key]
    found = headings(source_text(doc, "en"))
    assert found[0] == (1, doc["title"]["en"])
    assert [level for level, _ in found].count(1) == 1


def test_end_subscription_uses_only_end_subscription_wording() -> None:
    text = source_text(by_key()["end-subscription"], "en")
    assert "end subscription" in text.lower()
    assert "cancel plan" not in text.lower()
    # RAG-16: the keyword search for "cancel plan" must not reach this document at all.
    assert not CANCEL_OR_PLAN.findall(text)


def test_only_end_subscription_talks_about_ending_a_subscription() -> None:
    for doc in _documents():
        if doc["doc_key"] != "end-subscription":
            assert not END_SUBSCRIPTION_EN.search(source_text(doc, "en")), doc["doc_key"]


def test_refund_policy_states_the_tool_16_rule_in_usd() -> None:
    text = source_text(by_key()["refund-policy"], "en")
    assert "7 days" in text
    assert "USD" in text
    assert "cancelled before it ships" in text
    assert "delivered no more than 7 days ago" in text
    assert "full amount of the order" in text


def test_money_is_always_usd() -> None:
    for doc in _documents():
        text = source_text(doc, "en")
        assert "$" not in text, doc["doc_key"]
        assert not re.search(r"\b(?:BRL|EUR|GBP)\b", text), doc["doc_key"]


def test_injection_wording_appears_only_in_the_malicious_document() -> None:
    for doc in _documents():
        text = source_text(doc, "en").lower()
        found = [marker for marker in INJECTION_MARKERS if marker in text]
        if doc["malicious"]:
            assert len(found) >= 2, doc["doc_key"]
        else:
            assert not found, f"{doc['doc_key']}: {found}"
