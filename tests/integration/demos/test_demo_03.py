"""Phase 03 RAG demo with hallucination benchmark (TASK-051, RAG-05/06/16/18/19, DOD-02)."""

import csv
import json
import re
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from sqlalchemy import delete

from api.chat.prompt_builder import NO_EXCERPTS_INSTRUCTION, system_prompt
from api.config import get_settings
from api.db.engine import dispose_engine, get_sessionmaker, session_scope
from api.llm.client import ChatMessage, LLMRequest, SystemBlock
from api.messages import Lang
from api.rag.models import TS_CONFIGS, Chunk, Document
from api.rag.retriever import RetrievedChunk
from demos.common import DEFAULT_RESULTS_PATH
from demos.demo_03_rag import (
    DEFAULT_SAMPLE_PATH,
    GUESSES,
    QUESTIONS_PATH,
    REFUSALS,
    SAMPLE_COLUMNS,
    SAMPLE_SIZE,
    AnswerRecord,
    GroundedFakeLLM,
    Question,
    RecordingRetriever,
    RubricFakeJudge,
    load_questions,
    main,
    probe_fulltext,
    probe_synonyms,
    write_sample,
)
from evals.judge import JudgeVerdict, judge_faithfulness

pytestmark = pytest.mark.integration

DOC_PREFIX = "demo03-test-"
KB: dict[str, dict[str, tuple[str, str]]] = {
    "end-subscription": {
        "en": (
            "End your subscription",
            "To end your subscription, open Settings and choose End subscription. "
            "Your access stays active until the last day of the paid period.",
        ),
        "pt": (
            "Encerrar a assinatura",
            "Para encerrar a sua assinatura, abra Configurações e escolha Encerrar assinatura. "
            "O acesso continua ativo até o último dia do período pago.",
        ),
    },
    "refund-policy": {
        "en": (
            "Refund policy",
            "Refund policy: you can ask for a refund within 7 days of the charge. "
            "Refunds are paid in USD to the original payment method.",
        ),
        "pt": (
            "Política de reembolso",
            "Política de reembolso: você pode pedir o reembolso em até 7 dias após a cobrança. "
            "O reembolso é pago em USD no meio de pagamento original.",
        ),
    },
    "password-reset": {
        "en": (
            "Reset your password",
            "To reset your password, click Forgot password on the sign-in page. "
            "The reset link expires after 30 minutes.",
        ),
        "pt": (
            "Redefinir a senha",
            "Para redefinir a sua senha, clique em Esqueci a senha na tela de entrada. "
            "O link de redefinição expira em 30 minutos.",
        ),
    },
}


@pytest.fixture(autouse=True)
def fake_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("ATLAS_LLM_PROVIDER", "fake")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ATLAS_RETRIEVAL_MODE", raising=False)
    monkeypatch.delenv("ATLAS_HISTORY_MODE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def knowledge_base(migrated_database: str) -> AsyncIterator[None]:
    """A tiny indexed corpus in the test database, committed so the demo sessions see it."""
    async with session_scope() as session:
        for key, languages in KB.items():
            for language, (title, content) in languages.items():
                document = Document(
                    doc_key=f"{DOC_PREFIX}{key}",
                    title=title,
                    language=language,
                    doc_type="policy",
                    format="md",
                    source_path=f"corpus/{language}/{key}.md",
                    content_hash=f"{key}-{language}",
                    status="indexed",
                )
                session.add(document)
                await session.flush()
                session.add(
                    Chunk(
                        document_id=document.id,
                        ordinal=0,
                        content=content,
                        language=language,
                        section=title,
                        token_count=len(content.split()),
                        ts_config=TS_CONFIGS[language],
                    )
                )
    await dispose_engine()
    yield
    await dispose_engine()
    async with session_scope() as session:
        await session.execute(delete(Document).where(Document.doc_key.startswith(DOC_PREFIX)))
    await dispose_engine()


def test_questions_file_has_30_questions_15_pt_and_15_en() -> None:
    lines = [line for line in QUESTIONS_PATH.read_text(encoding="utf-8").splitlines() if line]
    questions = load_questions()

    assert len(lines) == len(questions) == 30
    assert sum(q.language == "pt" for q in questions) == 15
    assert sum(q.language == "en" for q in questions) == 15
    assert len({q.id for q in questions}) == 30
    for language in ("pt", "en"):
        flags = {q.in_corpus for q in questions if q.language == language}
        assert flags == {True, False}, language


def _rate(cell: str, language: str) -> int:
    match = re.search(rf"{language} (\d+)% \((\d+)/15\)", cell)
    assert match, f"missing {language} rate in {cell!r}"
    return int(match.group(1))


@pytest.mark.usefixtures("knowledge_base")
def test_fake_run_records_rates_per_language_the_sample_and_the_probes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    results = tmp_path / "results.md"
    sample = tmp_path / "review" / "phase03_sample.csv"
    report = tmp_path / "demo03-report.json"

    exit_code = main(
        [
            "--provider",
            "fake",
            "--results",
            str(results),
            "--sample",
            str(sample),
            "--report",
            str(report),
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    rows = [line for line in results.read_text().splitlines() if line.startswith("| 03 |")]
    assert len(rows) == 1
    _, _, metric, before, after, notes, _ = rows[0].split("|")
    assert "Hallucination rate" in metric
    assert "mode=none" in before and "mode=fulltext" in after
    for language in ("en", "pt"):
        assert _rate(after, language) < _rate(before, language)
    assert f"manual review sample: {SAMPLE_SIZE} answers" in notes

    with sample.open(encoding="utf-8", newline="") as handle:
        sample_rows = list(csv.DictReader(handle))
    assert len(sample_rows) == SAMPLE_SIZE
    assert list(sample_rows[0]) == list(SAMPLE_COLUMNS)
    assert all(row["reviewer_faithful"] == "" for row in sample_rows)

    data = json.loads(report.read_text(encoding="utf-8"))
    probes = {probe["query"]: probe for probe in data["probes"]}
    assert probes["cancelar plano"]["fulltext_synonym_hit"] is False
    assert probes["cancel plan"]["fulltext_synonym_hit"] is False
    assert data["sample"]["size"] == SAMPLE_SIZE
    assert set(data["rates"]) == {"none", "fulltext"}
    without_excerpts = [a for a in data["answers"] if a["mode"] == "fulltext" and not a["chunks"]]
    assert without_excerpts, "expected out-of-corpus questions with no full-text hit"
    for answer in without_excerpts:
        assert answer["answer"] in REFUSALS.values(), answer
        assert answer["verdict"]["faithful"] is True, answer
    assert "[pt] query='cancelar plano' fulltext_synonym_hit=False" in output
    assert "[en] query='cancel plan' fulltext_synonym_hit=False" in output


@pytest.mark.usefixtures("knowledge_base")
async def test_fulltext_probe_misses_the_synonym_but_finds_the_literal_wording() -> None:
    factory = get_sessionmaker()
    target = f"{DOC_PREFIX}end-subscription"

    synonyms = await probe_synonyms(factory, k=5, target_doc_key=target)
    literal_en = await probe_fulltext(
        factory, "en", "end subscription", target, "end subscription", 5
    )
    literal_pt = await probe_fulltext(
        factory, "pt", "encerrar assinatura", target, "encerrar assinatura", 5
    )

    assert [(p.language, p.fulltext_synonym_hit) for p in synonyms] == [
        ("pt", False),
        ("en", False),
    ]
    assert literal_en.fulltext_synonym_hit is True
    assert literal_pt.fulltext_synonym_hit is True
    assert target in literal_pt.returned_doc_keys


async def test_rubric_fake_judge_accepts_grounded_answers_and_rejects_invented_ones() -> None:
    judge = RubricFakeJudge()
    chunk = KB["refund-policy"]["en"][1]
    question = "What is the refund policy?"

    grounded = await judge_faithfulness(
        judge,
        question,
        "Refund policy: you can ask for a refund within 7 days of the charge. [1]",
        [chunk],
    )
    invented = await judge_faithfulness(judge, question, "You have 30 days for a refund.", [chunk])
    refusal = await judge_faithfulness(
        judge, question, "I don't know; I could not find this in the knowledge base.", []
    )

    assert grounded.faithful is True and grounded.score == 1.0
    assert invented.faithful is False and invented.score == 0.0
    assert refusal.faithful is True


def _record(question_id: str, mode: str, faithful: bool) -> AnswerRecord:
    return AnswerRecord(
        question=Question(id=question_id, language="en", question="Q?", in_corpus=True),
        mode=mode,  # type: ignore[arg-type]
        answer="A.",
        chunks=[],
        citations=0,
        verdict=JudgeVerdict(faithful=faithful, score=1.0 if faithful else 0.0, rationale="r"),
    )


def test_write_sample_keeps_the_manual_review_and_counts_agreement(tmp_path: Path) -> None:
    records = [_record(f"q{i:02d}", mode, i % 2 == 0) for i in range(12) for mode in ("none",)]
    path = tmp_path / "sample.csv"

    first = write_sample(records, path, size=4)
    assert first.size == 4 and first.reviewed == 0
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    rows[0]["reviewer_faithful"] = rows[0]["judge_faithful"]
    rows[1]["reviewer_faithful"] = "false" if rows[1]["judge_faithful"] == "true" else "true"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SAMPLE_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    second = write_sample(records, path, size=4)

    assert (second.size, second.reviewed, second.agreed) == (4, 2, 1)
    with path.open(encoding="utf-8", newline="") as handle:
        kept = list(csv.DictReader(handle))
    assert [row["id"] for row in kept] == [row["id"] for row in rows]
    assert kept[0]["reviewer_faithful"] == rows[0]["reviewer_faithful"]


def _retriever(chunks: list[RetrievedChunk]) -> RecordingRetriever:
    retriever = RecordingRetriever(get_sessionmaker(), None, get_settings())
    retriever.last = chunks
    return retriever


def _chunk(content: str, language: Lang = "en") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=1,
        document_title="Refund policy",
        doc_key="refund-policy",
        section=None,
        page=None,
        language=language,
        content=content,
        score=1.0,
    )


@pytest.mark.parametrize("language", ["en", "pt"])
async def test_grounded_fake_refuses_when_the_request_says_no_excerpt_was_found(
    language: Lang,
) -> None:
    fake = GroundedFakeLLM(_retriever([]))
    request = LLMRequest(
        system=[SystemBlock(system_prompt(language)), SystemBlock(NO_EXCERPTS_INSTRUCTION)],
        messages=[ChatMessage("user", "Who won the 1994 World Cup?")],
    )

    response = await fake.complete(request)

    assert response.text == REFUSALS[language]
    assert ("Não sei" if language == "pt" else "I don't know") in response.text


async def test_grounded_fake_guesses_without_retrieval_like_an_ungrounded_model() -> None:
    fake = GroundedFakeLLM(_retriever([]))
    request = LLMRequest(
        system=[SystemBlock(system_prompt("pt"))],
        messages=[ChatMessage("user", "Como encerrar minha assinatura?")],
    )

    response = await fake.complete(request)

    assert response.text == GUESSES["pt"]


async def test_grounded_fake_finds_a_chunk_by_content_whatever_the_excerpt_format() -> None:
    content = KB["refund-policy"]["en"][1]
    fake = GroundedFakeLLM(_retriever([_chunk("Unrelated text."), _chunk(content)]))
    tagged = (
        '<excerpts>\n<excerpt index="1" source="Other">\nUnrelated   text.</excerpt>\n'
        f'<excerpt index="2" source="Refund policy">\n{content.replace(". ", ".\n  ")}\n'
        "</excerpt>\n</excerpts>"
    )
    request = LLMRequest(
        system=[SystemBlock(system_prompt("en"))],
        messages=[ChatMessage("user", f"{tagged}\n\nCustomer question:\nRefund policy?")],
    )

    response = await fake.complete(request)

    assert response.text == "Unrelated text. [1]"


@pytest.mark.usefixtures("knowledge_base")
def test_fake_run_without_paths_writes_to_a_temp_dir_and_never_to_docs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    protected = [DEFAULT_RESULTS_PATH, DEFAULT_SAMPLE_PATH]
    before = {path: path.read_bytes() if path.exists() else None for path in protected}

    exit_code = main(["--provider", "fake"])

    assert exit_code == 0
    after = {path: path.read_bytes() if path.exists() else None for path in protected}
    assert after == before
    written = {path.name for path in tmp_path.rglob("*") if path.is_file()}
    assert {"results.md", "phase03_sample.csv", "demo03-report.json"} <= written
