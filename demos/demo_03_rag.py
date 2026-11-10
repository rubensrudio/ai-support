"""Phase 03 demo: full-text RAG and its hallucination benchmark (RAG-05/06/16/18/19, DOD-02).

1. **Synonym probes.** Runs the full-text search (`search_fulltext`, CT-29) for
   "cancelar plano" (pt) and "cancel plan" (en). The `end-subscription` document only says
   "encerrar assinatura" / "end subscription", so lexical search finds no hit: the problem
   that embeddings solve in phase 04 (`fulltext_synonym_hit=False`, RAG-05, RAG-16).
2. **Hallucination benchmark.** Sends the 30 reference questions of
   `demos/data/phase03_questions.jsonl` (15 pt + 15 en, some covered by the corpus and some
   not) through `ChatService` twice: without RAG (`retrieval_mode="none"`, the phase-02
   behaviour) and with RAG (`fulltext`). Every answer is judged by the LLM-as-judge
   (`evals.judge.judge_faithfulness`) against the excerpts the chat actually retrieved; an
   answer is a hallucination when the judge finds it unfaithful (RAG-18). The rates per mode
   and language go to the `| 03 |` row of `docs/results.md` (RAG-06, RAG-19).
3. **Manual review sample.** Writes 10 answers to `docs/review/phase03_sample.csv` with the
   judge verdict and empty `reviewer_*` columns. A rerun keeps what a reviewer filled in for
   the same answers and reports how many reviewed answers agree with the judge, next to the
   sample size (RAG-18). The whole run is also written as JSON to `--report`.

Prerequisites: the database (`ATLAS_DATABASE_URL`) with the corpus ingested
(`uv run python -m ingest run`). The demo seeds the personas `alex` (en) and `bia` (pt).

`--provider fake` runs offline: `GroundedFakeLLM` answers from the excerpts that reach its
request (quoting the first sentence of a retrieved chunk with its `[n]` marker), says it does
not know when the request carries the prompt builder's `NO_EXCERPTS_INSTRUCTION` (RAG-04), and
guesses an invented answer when there is no retrieval at all, which is the failure mode the
benchmark measures; `RubricFakeJudge` applies the rubric literally (every sentence must appear
in an excerpt). A fake run writes its results, sample and report under
`<tmp>/atlas-demo03/` unless the paths are given, so it never touches `docs/`.

Usage:
    uv run python -m demos.demo_03_rag --provider fake --results /tmp/results.md \
        --sample /tmp/phase03_sample.csv --report /tmp/demo03-report.json
    uv run python -m demos.demo_03_rag --provider fake   # writes under <tmp>/atlas-demo03/
    uv run python -m demos.demo_03_rag --provider real   # writes docs/results.md and docs/review
"""

import argparse
import asyncio
import csv
import json
import random
import re
import sys
import tempfile
from collections.abc import AsyncIterator, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.auth.deps import CurrentCustomer
from api.chat.prompt_builder import NO_EXCERPTS_INSTRUCTION
from api.chat.schemas import ChatRequest, ChatResponse
from api.chat.service import ChatService
from api.config import RetrievalMode, Settings, get_settings
from api.db.engine import dispose_engine, get_sessionmaker, session_scope
from api.llm.client import LLMClient, LLMRequest, LLMResponse, LLMStreamEvent, Usage
from api.llm.fake import FAKE_MODEL, FAKE_PROVIDER
from api.llm.instrumented import InstrumentedLLMClient
from api.llm.tokens import count_messages, count_tokens
from api.messages import Lang
from api.rag.embeddings import EmbeddingClient
from api.rag.fulltext import search_fulltext
from api.rag.models import Chunk, Document
from api.rag.retriever import RetrievedChunk, Retriever
from demos.common import (
    DEFAULT_RESULTS_PATH,
    ROOT_DIR,
    build_demo_llm,
    demo_parser,
    record_result,
)
from demos.demo_01_stateless import seed_demo_principals
from evals.judge import JudgeVerdict, judge_faithfulness

PHASE = 3
METRIC = "Hallucination rate (LLM judge), 30 questions, without vs with full-text RAG"
MODES: tuple[RetrievalMode, ...] = ("none", "fulltext")
LANGUAGES: tuple[Lang, ...] = ("en", "pt")
QUESTIONS_PATH = ROOT_DIR / "demos" / "data" / "phase03_questions.jsonl"
DEFAULT_SAMPLE_PATH = ROOT_DIR / "docs" / "review" / "phase03_sample.csv"
DEFAULT_REPORT_PATH = ROOT_DIR / "reports" / "demo03-report.json"
SAMPLE_SIZE = 10
SAMPLE_SEED = 3
SAMPLE_COLUMNS: tuple[str, ...] = (
    "id",
    "mode",
    "language",
    "question",
    "answer",
    "excerpts",
    "judge_faithful",
    "judge_score",
    "judge_rationale",
    "reviewer_faithful",
    "reviewer_notes",
)
TARGET_DOC_KEY = "end-subscription"


@dataclass(frozen=True)
class SynonymProbe:
    language: Lang
    query: str
    target_phrase: str
    """Wording the target document uses instead of the query's words."""


SYNONYM_PROBES: tuple[SynonymProbe, ...] = (
    SynonymProbe("pt", "cancelar plano", "encerrar assinatura"),
    SynonymProbe("en", "cancel plan", "end subscription"),
)


@dataclass(frozen=True)
class Question:
    id: str
    language: Lang
    question: str
    in_corpus: bool
    """Whether the reference corpus is expected to answer it (informative only)."""


def load_questions(path: Path = QUESTIONS_PATH) -> list[Question]:
    """Questions of the JSONL file, one per non-blank line. Raises `ValueError` on bad data."""
    questions: list[Question] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        raw = json.loads(line)
        language = raw.get("language")
        text = raw.get("question")
        if language not in LANGUAGES or not isinstance(text, str) or not text.strip():
            raise ValueError(f"{path}:{line_number}: invalid question {raw!r}")
        questions.append(
            Question(
                id=str(raw["id"]),
                language="pt" if language == "pt" else "en",
                question=text,
                in_corpus=bool(raw.get("in_corpus", False)),
            )
        )
    return questions


# --- 1. Synonym probes ------------------------------------------------------------------------


@dataclass(frozen=True)
class ProbeResult:
    language: Lang
    query: str
    target_doc_key: str
    returned_doc_keys: list[str]
    fulltext_synonym_hit: bool


async def probe_fulltext(
    session_factory: async_sessionmaker[AsyncSession],
    language: Lang,
    query: str,
    target_doc_key: str,
    target_phrase: str,
    k: int,
) -> ProbeResult:
    """Full-text search for `query`; a hit is any chunk of the target doc or with its wording."""
    async with session_factory() as session:
        scored = await search_fulltext(session, query, language, k)
        ids = [item.chunk_id for item in scored]
        rows = (
            (
                await session.execute(
                    select(Chunk.id, Document.doc_key, Chunk.content)
                    .join(Document, Document.id == Chunk.document_id)
                    .where(Chunk.id.in_(ids))
                )
            ).all()
            if ids
            else []
        )
    by_id = {chunk_id: (doc_key, content) for chunk_id, doc_key, content in rows}
    found = [by_id[chunk_id] for chunk_id in ids if chunk_id in by_id]
    hit = any(
        doc_key == target_doc_key or target_phrase.lower() in content.lower()
        for doc_key, content in found
    )
    return ProbeResult(
        language=language,
        query=query,
        target_doc_key=target_doc_key,
        returned_doc_keys=[doc_key for doc_key, _ in found],
        fulltext_synonym_hit=hit,
    )


async def probe_synonyms(
    session_factory: async_sessionmaker[AsyncSession],
    k: int,
    target_doc_key: str = TARGET_DOC_KEY,
) -> list[ProbeResult]:
    return [
        await probe_fulltext(
            session_factory, probe.language, probe.query, target_doc_key, probe.target_phrase, k
        )
        for probe in SYNONYM_PROBES
    ]


# --- Offline models ---------------------------------------------------------------------------


class RecordingRetriever(Retriever):
    """`Retriever` that keeps the chunks of its last call, so the judge sees what the chat saw."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        embedder: EmbeddingClient | None,
        settings: Settings,
    ) -> None:
        super().__init__(session_factory, embedder, settings)
        self.last: list[RetrievedChunk] = []

    async def retrieve(
        self,
        query: str,
        language: Lang,
        k: int | None = None,
        mode: RetrievalMode | None = None,
    ) -> list[RetrievedChunk]:
        self.last = await super().retrieve(query, language, k, mode)
        return self.last


_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_MARKER = re.compile(r"\s*\[\d+\]")
GUESSES: dict[Lang, str] = {
    "en": (
        "Sure! Go to Settings > Plan and confirm the change. It takes effect within 24 hours "
        "and you get a USD 10 credit on your next invoice."
    ),
    "pt": (
        "Claro! Vá em Configurações > Plano e confirme a alteração. Ela vale em até 24 horas "
        "e você ganha um crédito de USD 10 na próxima fatura."
    ),
}


REFUSALS: dict[Lang, str] = {
    "en": "I don't know. I could not find this information in the Assinaflow knowledge base.",
    "pt": "Não sei. Não encontrei essa informação na base de conhecimento da Assinaflow.",
}


def _first_sentence(text: str) -> str:
    return _SENTENCE_END.split(text.strip(), maxsplit=1)[0]


def _reply(request: LLMRequest, text: str) -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=[],
        usage=Usage(
            input_tokens=count_messages(request.system, request.messages),
            output_tokens=count_tokens(text),
        ),
        model=FAKE_MODEL,
        provider=FAKE_PROVIDER,
        stop_reason="end_turn",
    )


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


class _CompleteOnly:
    """Streams the `complete` answer as one delta (shared by the offline models)."""

    async def complete(self, request: LLMRequest) -> LLMResponse:
        raise NotImplementedError

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        return self._stream(request)

    async def _stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        response = await self.complete(request)
        yield LLMStreamEvent(kind="text_delta", text=response.text)
        yield LLMStreamEvent(kind="usage", usage=response.usage)
        yield LLMStreamEvent(kind="stop", stop_reason=response.stop_reason)


class GroundedFakeLLM(_CompleteOnly):
    """Offline chat model that answers only from excerpts present in its request.

    It looks for the first sentence of each chunk the retriever returned inside the request
    (system blocks and messages, whitespace-insensitive), so it does not depend on how the
    prompt builder formats the excerpts. The first chunk found is quoted (first sentence) with
    its `[n]` marker. With no chunk in the request it follows `NO_EXCERPTS_INSTRUCTION` when the
    request carries it (RAG-04: "I don't know" / "Não sei"); without that instruction (no
    retrieval, the phase-02 request) it guesses an invented answer, as an ungrounded model does.
    """

    def __init__(self, retriever: RecordingRetriever) -> None:
        self.retriever = retriever
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if request.purpose != "chat":
            return _reply(request, "Summary of the earlier conversation.")
        texts = [block.text for block in request.system] + [m.content for m in request.messages]
        sent = _normalize("\n".join(texts))
        for index, chunk in enumerate(self.retriever.last, start=1):
            quote = _first_sentence(chunk.content)
            if quote and _normalize(quote) in sent:
                return _reply(request, f"{quote} [{index}]")
        # The reply language sits in a variable system block after the cached prefix (phase 11).
        system = "\n".join(block.text for block in request.system)
        language: Lang = "pt" if "Brazilian Portuguese" in system else "en"
        if _normalize(NO_EXCERPTS_INSTRUCTION) in sent:
            return _reply(request, REFUSALS[language])
        return _reply(request, GUESSES[language])


_EXCERPT = re.compile(r'<excerpt index="\d+">\n(.*?)\n</excerpt>', re.DOTALL)
_ANSWER = re.compile(r"<answer>\n(.*?)\n</answer>", re.DOTALL)
_REFUSAL = re.compile(
    r"don't know|do not know|could not find|couldn't find|não sei|não encontrei", re.IGNORECASE
)


class RubricFakeJudge(_CompleteOnly):
    """Offline judge: a sentence is supported only when it appears verbatim in an excerpt."""

    async def complete(self, request: LLMRequest) -> LLMResponse:
        content = request.messages[-1].content if request.messages else ""
        excerpts = _normalize(" ".join(_EXCERPT.findall(content)))
        match = _ANSWER.search(content)
        answer = _MARKER.sub("", match.group(1)).strip() if match else ""
        if _REFUSAL.search(answer):
            verdict = {"faithful": True, "score": 1.0, "rationale": "The answer declines."}
        else:
            claims = [s for s in _SENTENCE_END.split(answer) if s.strip()]
            supported = [claim for claim in claims if _normalize(claim) in excerpts]
            score = len(supported) / len(claims) if claims else 1.0
            unsupported = len(claims) - len(supported)
            verdict = {
                "faithful": unsupported == 0,
                "score": round(score, 2),
                "rationale": f"{unsupported} of {len(claims)} claims are not in the excerpts.",
            }
        return _reply(request, json.dumps(verdict))


# --- 2. Hallucination benchmark ---------------------------------------------------------------


@dataclass(frozen=True)
class AnswerRecord:
    question: Question
    mode: RetrievalMode
    answer: str
    chunks: list[str]
    citations: int
    verdict: JudgeVerdict

    @property
    def hallucinated(self) -> bool:
        return not self.verdict.faithful


@dataclass(frozen=True)
class Rate:
    hallucinated: int
    total: int

    @property
    def percent(self) -> int:
        return round(100 * self.hallucinated / self.total) if self.total else 0


async def send_rag_message(
    llm: LLMClient,
    settings: Settings,
    principal: CurrentCustomer,
    request: ChatRequest,
    retriever: Retriever,
) -> ChatResponse:
    """`demo_01.send_message` with a retriever: one message in its own unit of work."""
    async with session_scope() as session:
        service = ChatService(llm, settings, session, retriever)
        return await service.collect(service.handle_message(request, principal))


async def run_benchmark(
    chat_llm: LLMClient,
    judge_llm: LLMClient,
    settings: Settings,
    questions: Sequence[Question],
    retrievers: dict[RetrievalMode, RecordingRetriever],
) -> list[AnswerRecord]:
    """Every question once per retriever mode, each in a new conversation, judged on the
    excerpts that retriever returned for it."""
    principals = await seed_demo_principals(settings)
    records: list[AnswerRecord] = []
    for mode, retriever in retrievers.items():
        for question in questions:
            retriever.last = []
            response = await send_rag_message(
                chat_llm,
                retriever.settings,
                principals[question.language],
                ChatRequest(message=question.question, ui_language=question.language),
                retriever,
            )
            chunks = [chunk.content for chunk in retriever.last]
            verdict = await judge_faithfulness(
                judge_llm, question.question, response.content, chunks
            )
            records.append(
                AnswerRecord(
                    question=question,
                    mode=mode,
                    answer=response.content,
                    chunks=chunks,
                    citations=len(response.citations),
                    verdict=verdict,
                )
            )
    return records


def hallucination_rates(records: Sequence[AnswerRecord], mode: RetrievalMode) -> dict[Lang, Rate]:
    rates: dict[Lang, Rate] = {}
    for language in LANGUAGES:
        selected = [r for r in records if r.mode == mode and r.question.language == language]
        rates[language] = Rate(sum(r.hallucinated for r in selected), len(selected))
    return rates


# --- 3. Manual review sample ------------------------------------------------------------------


@dataclass(frozen=True)
class SampleSummary:
    path: str
    size: int
    reviewed: int
    agreed: int


def _flag(value: str) -> bool | None:
    normalized = value.strip().lower()
    if normalized in {"true", "yes", "y", "1", "sim", "s"}:
        return True
    if normalized in {"false", "no", "n", "0", "nao", "não"}:
        return False
    return None


def _read_reviews(path: Path) -> dict[tuple[str, str], dict[str, str]]:
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8", newline="") as handle:
        return {(row.get("mode", ""), row.get("id", "")): row for row in csv.DictReader(handle)}


def write_sample(
    records: Sequence[AnswerRecord], path: Path, size: int = SAMPLE_SIZE
) -> SampleSummary:
    """Deterministic sample of `size` answers; keeps reviewer columns of an earlier run."""
    previous = _read_reviews(path)
    ordered = sorted(records, key=lambda r: (r.mode, r.question.id))
    chosen = random.Random(SAMPLE_SEED).sample(ordered, min(size, len(ordered)))  # noqa: S311
    chosen.sort(key=lambda r: (r.mode, r.question.id))
    rows: list[dict[str, str]] = []
    reviewed = agreed = 0
    for record in chosen:
        old = previous.get((record.mode, record.question.id), {})
        row = {
            "id": record.question.id,
            "mode": record.mode,
            "language": record.question.language,
            "question": record.question.question,
            "answer": record.answer,
            "excerpts": "\n---\n".join(record.chunks),
            "judge_faithful": "true" if record.verdict.faithful else "false",
            "judge_score": f"{record.verdict.score:.2f}",
            "judge_rationale": record.verdict.rationale,
            "reviewer_faithful": old.get("reviewer_faithful", "") or "",
            "reviewer_notes": old.get("reviewer_notes", "") or "",
        }
        human = _flag(row["reviewer_faithful"])
        if human is not None:
            reviewed += 1
            agreed += human == record.verdict.faithful
        rows.append(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SAMPLE_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return SampleSummary(path=str(path), size=len(rows), reviewed=reviewed, agreed=agreed)


# --- Orchestration ----------------------------------------------------------------------------


@dataclass
class DemoReport:
    provider: str
    questions: int
    probes: list[ProbeResult]
    rates: dict[str, dict[str, dict[str, int]]]
    sample: SampleSummary | None = None
    answers: list[dict[str, Any]] = field(default_factory=list)


def _chat_llms(provider: str, retriever: RecordingRetriever) -> tuple[LLMClient, LLMClient]:
    if provider == "fake":
        chat: LLMClient = InstrumentedLLMClient(
            GroundedFakeLLM(retriever), model=FAKE_MODEL, provider=FAKE_PROVIDER
        )
        judge: LLMClient = InstrumentedLLMClient(
            RubricFakeJudge(), model=FAKE_MODEL, provider=FAKE_PROVIDER
        )
        return chat, judge
    llm = build_demo_llm(provider)
    return llm, llm


async def _run(
    provider: str, settings: Settings, questions: list[Question]
) -> tuple[list[ProbeResult], list[AnswerRecord]]:
    try:
        factory = get_sessionmaker()
        probes = await probe_synonyms(factory, settings.retrieval_top_k)
        retrievers = {
            mode: RecordingRetriever(
                factory, None, settings.model_copy(update={"retrieval_mode": mode})
            )
            for mode in MODES
        }
        records: list[AnswerRecord] = []
        for mode in MODES:
            chat_llm, judge_llm = _chat_llms(provider, retrievers[mode])
            records += await run_benchmark(
                chat_llm, judge_llm, settings, questions, {mode: retrievers[mode]}
            )
        return probes, records
    finally:
        # The engine's pooled connections belong to this event loop; close them here.
        await dispose_engine()


def _rates_cell(mode: RetrievalMode, rates: dict[Lang, Rate]) -> str:
    parts = [
        f"{lang} {rates[lang].percent}% ({rates[lang].hallucinated}/{rates[lang].total})"
        for lang in LANGUAGES
    ]
    return f"mode={mode}: " + ", ".join(parts)


def _display_path(path: Path) -> str:
    resolved = path.resolve()
    return str(resolved.relative_to(ROOT_DIR)) if resolved.is_relative_to(ROOT_DIR) else str(path)


def _review_note(sample: SampleSummary, path: Path) -> str:
    if sample.reviewed:
        status = f"reviewed {sample.reviewed}/{sample.size}, judge agreement "
        status += f"{sample.agreed}/{sample.reviewed}"
    else:
        status = "manual review pending"
    return f"manual review sample: {sample.size} answers in {_display_path(path)} ({status})"


def _resolve_output_paths(args: argparse.Namespace) -> None:
    """Fill the output paths left out on the command line.

    `real` writes the tracked files (`docs/results.md`, `docs/review/phase03_sample.csv`).
    `fake` writes under a temporary directory, so an offline run never replaces the real
    `| 03 |` row or a reviewed sample; pass the paths explicitly to write elsewhere.
    """
    if args.provider == "fake":
        base = Path(tempfile.gettempdir()) / "atlas-demo03"
        defaults = (base / "results.md", base / "phase03_sample.csv", base / "demo03-report.json")
    else:
        defaults = (DEFAULT_RESULTS_PATH, DEFAULT_SAMPLE_PATH, DEFAULT_REPORT_PATH)
    for name, default in zip(("results", "sample", "report"), defaults, strict=True):
        if getattr(args, name) is None:
            setattr(args, name, default)


def main(argv: list[str] | None = None) -> int:
    parser = demo_parser(
        "Phase 03: full-text synonym probes and hallucination rate without vs with RAG"
    )
    parser.add_argument("--questions", type=Path, default=QUESTIONS_PATH)
    parser.add_argument("--sample", type=Path, default=None)
    parser.add_argument("--report", type=Path, default=None)
    parser.set_defaults(results=None)
    args = parser.parse_args(argv)
    _resolve_output_paths(args)
    settings = get_settings()
    questions = load_questions(args.questions)

    probes, records = asyncio.run(_run(args.provider, settings, questions))
    for probe in probes:
        print(
            f"[{probe.language}] query={probe.query!r} "
            f"fulltext_synonym_hit={probe.fulltext_synonym_hit} "
            f"returned={probe.returned_doc_keys}"
        )
    rates = {mode: hallucination_rates(records, mode) for mode in MODES}
    for mode, by_language in rates.items():
        for language, rate in by_language.items():
            print(f"[{mode}] [{language}] hallucinated={rate.hallucinated}/{rate.total}")

    sample = write_sample(records, args.sample)
    print(f"Review sample written to {args.sample} ({sample.size} answers)")

    report = DemoReport(
        provider=args.provider,
        questions=len(questions),
        probes=probes,
        rates={
            mode: {lang: {**asdict(rate), "percent": rate.percent} for lang, rate in by.items()}
            for mode, by in rates.items()
        },
        sample=sample,
        answers=[
            {
                "id": r.question.id,
                "mode": r.mode,
                "language": r.question.language,
                "in_corpus": r.question.in_corpus,
                "answer": r.answer,
                "chunks": len(r.chunks),
                "citations": r.citations,
                "verdict": asdict(r.verdict),
            }
            for r in records
        ],
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(asdict(report), indent=2, ensure_ascii=False) + "\n")
    print(f"Report written to {args.report}")

    counts = {lang: sum(q.language == lang for q in questions) for lang in LANGUAGES}
    probe_note = ", ".join(
        f"'{p.query}' {'yes' if p.fulltext_synonym_hit else 'no'}" for p in probes
    )
    record_result(
        PHASE,
        METRIC,
        _rates_cell("none", rates["none"]),
        _rates_cell("fulltext", rates["fulltext"]),
        notes=(
            f"provider={args.provider}; {len(questions)} questions ({counts['pt']} pt + "
            f"{counts['en']} en), top-{settings.retrieval_top_k} chunks; LLM judge with the "
            f"faithfulness rubric (prompts/judge_faithfulness.md); "
            f"{_review_note(sample, args.sample)}; "
            f"full-text synonym hit: {probe_note}"
        ),
        results_path=args.results,
    )
    print(f"Recorded phase {PHASE:02d} in {args.results}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
