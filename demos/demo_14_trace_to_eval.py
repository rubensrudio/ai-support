"""Phase 14 demo: from a customer complaint to a passing eval case (OBS-05, DOD-02).

The five steps (printed as `[n/5]`):

1. **Outdated document.** The corpus is copied to a temporary folder (`<tmp>/corpus`, so the
   stored source paths stay `corpus/<lang>/...`) and a test bulletin with an old Essential
   shipping fee (`STALE_DOC_KEY`) is added to the copy's manifest. The copy is ingested into the
   configured database (`ATLAS_DATABASE_URL`) without embeddings: the demo retrieves with
   `fulltext`, so the ranking is lexical and deterministic. The versioned `corpus/` is never
   written.
2. **Wrong answer.** The persona `alex` asks `QUESTION` through `ChatService`, inside a
   `request` span with a fresh trace id, as `POST /chat` does behind `TraceIdMiddleware`. The
   bulletin wins retrieval and the answer quotes the old fee, citing it.
3. **Negative feedback.** The customer rates the answer `down` through the real route
   (`POST /messages/{id}/feedback`, `api.main.create_app` over an in-process ASGI transport,
   persona session cookie). The route stores the `feedback` row with the message `trace_id` and
   pushes the score -1 to Langfuse; without `--langfuse` that push goes to a local recorder.
4. **Trace.** The `retrieval` spans of that trace id are read from an `InMemorySpanExporter`
   attached for the run. With `--langfuse` (needs `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`,
   `LANGFUSE_SECRET_KEY`; never required) the trace is read back from the Langfuse public API
   (`GET /api/public/traces/{traceId}`), polling while Langfuse ingests it, and the demo falls
   back to the in-memory spans when that fails. The cited `doc_key` and its chunk text are
   shown: the culprit.
5. **Fix and eval case.** The bulletin of the temporary copy is rewritten (archived, no fee),
   the copy is ingested again, and `DEMO_CASE` (the question, expecting a citation of
   `plans-and-pricing`) is written to `--dataset-out`. The case is graded with the default
   `rag` grader of `evals` (citation + faithfulness): on the stale answer of step 2 (FAIL) and
   on a fresh in-process run (`evals.targets.ChatTarget`) after the fix (PASS).

The bulletin's rows are deleted at the end, so the configured database keeps no trace of it.
`--dataset-out` defaults to `evals/datasets/phase_14.jsonl` for `--provider real` (where the
case already is: the write is a no-op unless the case changed) and to `<tmp>/atlas-demo14/` for
`--provider fake`, which starts from a copy of the versioned file. The `| 14 |` row of
`docs/results.md` (fake: `<tmp>/atlas-demo14/results.md`) records both scores.

`--provider fake` runs offline: `CitationRuleFakeLLM` (demo 13) quotes the first sentence of
the first retrieved excerpt with its `[n]` marker, and the faithfulness judge is the rubric judge
of demo 03 (`RubricFakeJudge`). `real` uses the configured chat provider and
`ATLAS_JUDGE_MODEL` (or the chat model) for the judge.

Warning: like demos 03 to 13 this runs in the configured database: it ingests the corpus there
(when it is not ingested yet), seeds the personas and resets their orders.

Exit code: 0 when the case failed before the fix and passes after it, 1 otherwise.

Usage:
    uv run python -m demos.demo_14_trace_to_eval --provider fake
    uv run python -m demos.demo_14_trace_to_eval --provider real --langfuse
"""

import argparse
import asyncio
import base64
import json
import shutil
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import delete, select

from api.auth.deps import SESSION_COOKIE
from api.auth.service import create_session
from api.chat.schemas import ChatRequest, ChatResponse
from api.chat.service import ChatService
from api.config import Settings, get_settings
from api.db.engine import dispose_engine, get_sessionmaker, session_scope
from api.feedback.models import Feedback
from api.feedback.routes import get_langfuse_api
from api.llm.client import LLMClient
from api.main import create_app
from api.messages import Lang
from api.observability.langfuse_api import LangfuseApi
from api.observability.logging import bind_trace_id, new_trace_id, reset_trace_id
from api.observability.tracing import add_span_exporter, start_span
from api.rag.models import Chunk, Document
from demos.common import DEFAULT_RESULTS_PATH, ROOT_DIR, build_demo_llm, demo_parser, record_result
from demos.demo_01_stateless import seed_demo_principals
from demos.demo_03_rag import RecordingRetriever
from demos.demo_13_eval_gate import CitationRuleFakeLLM, build_judge
from evals.__main__ import build_graders
from evals.dataset import EvalCase, load_cases
from evals.runner import EvalReport, run_cases
from evals.targets import ChatTarget
from ingest.pipeline import MANIFEST_NAME, ingest_corpus

PHASE = 14
METRIC = (
    "Trace to eval: score (0-100) of the regression case born from a negative-feedback trace, "
    "on the stale answer vs after the document fix"
)
CORPUS_DIR = ROOT_DIR / "corpus"
PHASE_14_DATASET = ROOT_DIR / "evals" / "datasets" / "phase_14.jsonl"
# Fake outputs (dataset copy, results); read at call time, so tests can monkeypatch it.
FAKE_DIR = Path(tempfile.gettempdir()) / "atlas-demo14"
RETRIEVAL_MODE = "fulltext"

STEPS: tuple[str, ...] = (
    "Outdated document in a temporary corpus copy",
    "Customer question retrieves the wrong chunk",
    "Negative feedback on the answer",
    "Reading the retrieval spans of the trace",
    "Fix, re-ingest and new eval case",
)

STALE_DOC_KEY = "demo14-pricing-bulletin-2023"
STALE_LANGUAGE: Lang = "en"
STALE_PATH = f"{STALE_LANGUAGE}/{STALE_DOC_KEY}.md"
STALE_TEXT = """# Essential tier shipping (2023 bulletin)

Essential tier shipping per box: you pay USD 4.90 shipping per box on the Essential tier.
Every Essential box ships with standard shipping.
"""
FIXED_TEXT = """# Archived 2023 bulletin

This bulletin was archived and no longer applies.
See the current tables in the help center.
"""
QUESTION = "How much shipping per box do I pay on the Essential tier?"
EXPECTED_DOC_KEY = "plans-and-pricing"
PERSONA = "alex"
DEMO_CASE = EvalCase(
    id="phase14-rag-en-001",
    phase=PHASE,
    category="rag",
    language="en",
    input={"message": QUESTION, "persona": PERSONA},
    expected={"citation_doc_key": EXPECTED_DOC_KEY},
    grader="rag",
)

LANGFUSE_TRACE_PATH = "/api/public/traces/{trace_id}"
LANGFUSE_POLL_SECONDS = 3.0
ATTR_MODE = "atlas.retrieval.mode"
ATTR_DOC_KEYS = "atlas.retrieval.doc_keys"
ATTR_CHUNK_IDS = "atlas.retrieval.chunk_ids"
ATTR_SCORES = "atlas.retrieval.scores"
RETRIEVAL_SPAN = "retrieval"

EXIT_OK = 0
EXIT_FAILED = 1


def step(number: int) -> None:
    print(f"\n[{number}/{len(STEPS)}] {STEPS[number - 1]}")


# --- 1. temporary corpus ---------------------------------------------------------------------


def stale_manifest_entry() -> dict[str, Any]:
    return {
        "doc_key": STALE_DOC_KEY,
        "doc_type": "policy",
        "format": "md",
        "languages": [STALE_LANGUAGE],
        "malicious": False,
        "title": {STALE_LANGUAGE: "Essential tier shipping (2023 bulletin)"},
        "source": {STALE_LANGUAGE: STALE_PATH},
        "file": {STALE_LANGUAGE: STALE_PATH},
    }


def build_temp_corpus(parent: Path) -> Path:
    """Copy `corpus/` to `<parent>/corpus` and add the stale bulletin to the copy only."""
    corpus = parent / "corpus"
    shutil.copytree(CORPUS_DIR, corpus)
    manifest_path = corpus / MANIFEST_NAME
    manifest: dict[str, Any] = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["documents"].append(stale_manifest_entry())
    manifest_path.write_text(
        yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    (corpus / STALE_PATH).write_text(STALE_TEXT, encoding="utf-8")
    return corpus


async def ingest(corpus: Path) -> str:
    report = await ingest_corpus(corpus, get_sessionmaker(), skip_embeddings=True)
    [stale] = [doc for doc in report.documents if doc.doc_key == STALE_DOC_KEY]
    totals = ", ".join(f"{name} {count}" for name, count in report.totals().items())
    return f"{len(report.documents)} documents ({totals}); {STALE_DOC_KEY}: {stale.status}"


async def delete_stale_document() -> None:
    async with session_scope() as session:
        await session.execute(delete(Document).where(Document.doc_key == STALE_DOC_KEY))


# --- 2. the wrong answer ---------------------------------------------------------------------


async def ask(
    llm: LLMClient, settings: Settings, retriever: RecordingRetriever, customer: Any
) -> ChatResponse:
    """One `POST /chat` in process: its own trace id, a `request` root span, one unit of work."""
    token = bind_trace_id(new_trace_id())
    try:
        with start_span("request", **{"http.method": "POST", "url.path": "/chat"}):
            async with session_scope() as session:
                service = ChatService(llm, settings, session, retriever)
                request = ChatRequest(message=QUESTION, ui_language=STALE_LANGUAGE)
                return await service.collect(service.handle_message(request, customer))
    finally:
        reset_trace_id(token)


def chat_output(response: ChatResponse, chunks: Sequence[str]) -> dict[str, Any]:
    """The `evals.graders` output dict of a chat answer (same shape as `ChatTarget`)."""
    return {
        "text": response.content,
        "status": response.status,
        "language": response.language,
        "trajectory": [],
        "citations": [c.doc_key for c in response.citations if c.doc_key],
        "chunks": list(chunks),
        "triage": None,
        "error": None,
    }


# --- 3. feedback -----------------------------------------------------------------------------


class RecordingLangfuse:
    """Stands in for `LangfuseApi` without `--langfuse`: keeps the pushed scores locally."""

    def __init__(self) -> None:
        self.scores: list[tuple[str, int]] = []
        self.score_ids: list[str | None] = []

    async def push_score(
        self,
        trace_id: str,
        value: int,
        comment: str | None = None,
        score_id: str | None = None,
    ) -> bool:
        """Same signature as `LangfuseApi.push_score` (CT-63), `score_id` included."""
        self.scores.append((trace_id, value))
        self.score_ids.append(score_id)
        return True


async def send_feedback(
    message_id: int, customer_id: Any, langfuse: LangfuseApi | RecordingLangfuse
) -> int:
    """`POST /messages/{id}/feedback {"rating": "down"}` with the customer's session cookie."""
    async with session_scope() as session:
        token = await create_session(session, customer_id)
    app = create_app()
    app.dependency_overrides[get_langfuse_api] = lambda: langfuse
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://demo") as http:
        http.cookies.set(SESSION_COOKIE, token)
        response = await http.post(f"/messages/{message_id}/feedback", json={"rating": "down"})
    return response.status_code


async def stored_rating(message_id: int) -> tuple[str, str] | None:
    async with session_scope() as session:
        row = (
            await session.execute(
                select(Feedback.rating, Feedback.trace_id).where(Feedback.message_id == message_id)
            )
        ).first()
    return None if row is None else (row[0], row[1])


# --- 4. reading the trace --------------------------------------------------------------------


@dataclass(frozen=True)
class RetrievalTrace:
    mode: str | None
    doc_keys: list[str]
    chunk_ids: list[int]
    scores: list[float]


def _as_list(value: object) -> list[Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return [value]
    if isinstance(value, list | tuple):
        return list(value)
    return [] if value is None else [value]


def _retrieval(attributes: Mapping[str, Any]) -> RetrievalTrace:
    mode = attributes.get(ATTR_MODE)
    return RetrievalTrace(
        mode=None if mode is None else str(mode),
        doc_keys=[str(key) for key in _as_list(attributes.get(ATTR_DOC_KEYS))],
        chunk_ids=[int(chunk_id) for chunk_id in _as_list(attributes.get(ATTR_CHUNK_IDS))],
        scores=[float(score) for score in _as_list(attributes.get(ATTR_SCORES))],
    )


def retrievals_from_spans(spans: Sequence[ReadableSpan], trace_id: str) -> list[RetrievalTrace]:
    """The `retrieval` spans of `trace_id` (32 hex), in the order they ended."""
    wanted = int(trace_id, 16)
    return [
        _retrieval(dict(span.attributes or {}))
        for span in spans
        if span.name == RETRIEVAL_SPAN and span.context is not None
        if span.context.trace_id == wanted
    ]


def _find_attributes(node: object) -> Mapping[str, Any] | None:
    """The first mapping under `node` that carries the retrieval doc keys."""
    if isinstance(node, Mapping):
        if ATTR_DOC_KEYS in node:
            return node
        children: list[object] = list(node.values())
    elif isinstance(node, list):
        children = list(node)
    else:
        return None
    for child in children:
        found = _find_attributes(child)
        if found is not None:
            return found
    return None


async def fetch_langfuse_retrievals(
    http: httpx.AsyncClient, trace_id: str, public_key: str, secret_key: str
) -> list[RetrievalTrace]:
    """The `retrieval` observations of a Langfuse trace; `[]` when it is not there (yet).

    Langfuse keeps the OTLP span attributes in the observation metadata; the exact nesting
    depends on its version (risk R-6), so the attributes are looked up anywhere inside it.
    """
    token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    response = await http.get(
        LANGFUSE_TRACE_PATH.format(trace_id=trace_id),
        headers={"Authorization": f"Basic {token}"},
    )
    if response.status_code != httpx.codes.OK:
        return []
    observations = response.json().get("observations") or []
    found: list[RetrievalTrace] = []
    for observation in observations:
        if not isinstance(observation, Mapping) or observation.get("name") != RETRIEVAL_SPAN:
            continue
        attributes = _find_attributes(observation)
        if attributes is not None:
            found.append(_retrieval(attributes))
    return found


async def poll_langfuse(settings: Settings, trace_id: str, wait: float) -> list[RetrievalTrace]:
    """Poll Langfuse until the trace's retrieval spans arrive or `wait` seconds pass."""
    host = (settings.langfuse_host or "").rstrip("/")
    public = settings.langfuse_public_key.get_secret_value() if settings.langfuse_public_key else ""
    secret = settings.langfuse_secret_key.get_secret_value() if settings.langfuse_secret_key else ""
    deadline = time.monotonic() + wait
    async with httpx.AsyncClient(base_url=host, timeout=10.0) as http:
        while True:
            try:
                found = await fetch_langfuse_retrievals(http, trace_id, public, secret)
            except httpx.HTTPError as error:
                print(f"    Langfuse API error: {type(error).__name__}")
                found = []
            if found or time.monotonic() >= deadline:
                return found
            await asyncio.sleep(LANGFUSE_POLL_SECONDS)


async def chunk_text(chunk_id: int) -> str:
    async with session_scope() as session:
        content = await session.scalar(select(Chunk.content).where(Chunk.id == chunk_id))
    return content or ""


# --- 5. eval case ----------------------------------------------------------------------------


def _case_line(case: EvalCase) -> str:
    return json.dumps(case.model_dump(mode="json"), ensure_ascii=False) + "\n"


def upsert_case(path: Path, case: EvalCase) -> bool:
    """Write `case` to the JSONL file, replacing the line with its id. Returns whether the file
    changed: an identical case leaves the file byte for byte as it was."""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True) if path.exists() else []
    new_line = _case_line(case)
    for index, line in enumerate(lines):
        if line.strip() and json.loads(line).get("id") == case.id:
            if json.loads(line) == case.model_dump(mode="json"):
                return False
            lines[index] = new_line
            break
    else:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(new_line)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(lines), encoding="utf-8")
    return True


class StaticTarget:
    """Replays a recorded output (the stale answer of step 2) for the grader."""

    def __init__(self, output: dict[str, Any]) -> None:
        self.output = output

    async def run(self, case: EvalCase) -> dict[str, Any]:
        return dict(self.output)


def _verdict(report: EvalReport) -> str:
    [result] = report.cases
    return f"{'PASS' if result.passed else 'FAIL'} (score {result.score:.2f}/100)"


# --- run -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DemoOutcome:
    before: EvalReport
    after: EvalReport
    culprit: str | None
    feedback_status: int


def _chat_llm(provider: str) -> LLMClient:
    return CitationRuleFakeLLM() if provider == "fake" else build_demo_llm(provider)


@dataclass
class _Run:
    """What the steps share; `target` is closed at the end, after the bulletin is deleted."""

    provider: str
    settings: Settings
    exporter: InMemorySpanExporter
    langfuse_api: LangfuseApi | None
    target: ChatTarget | None = None


@dataclass(frozen=True)
class _Answer:
    response: ChatResponse
    output: dict[str, Any]
    customer: Any
    trace_id: str


async def _step_corpus(tmp: Path) -> Path:
    step(1)
    corpus = build_temp_corpus(tmp)
    print(f"    temporary corpus: {corpus} (the versioned corpus/ is not touched)")
    print(f"    {STALE_PATH}: {STALE_TEXT.splitlines()[2]}")
    print(f"    ingested: {await ingest(corpus)}")
    return corpus


async def _step_question(run: _Run) -> _Answer:
    step(2)
    principals = await seed_demo_principals(run.settings)
    customer = principals[STALE_LANGUAGE]
    retriever = RecordingRetriever(get_sessionmaker(), None, run.settings)
    response = await ask(_chat_llm(run.provider), run.settings, retriever, customer)
    output = chat_output(response, [chunk.content for chunk in retriever.last])
    print(f"    {PERSONA}: {QUESTION}")
    print(f"    assistant: {response.content}")
    print(f"    citations: {output['citations']}; trace_id: {response.trace_id}")
    return _Answer(response, output, customer, response.trace_id)


async def _step_feedback(run: _Run, answer: _Answer) -> int:
    step(3)
    message_id = answer.response.message_id
    if message_id is None:
        raise RuntimeError("the answer was not stored; there is nothing to rate")
    recorder = RecordingLangfuse()
    status = await send_feedback(message_id, answer.customer.id, run.langfuse_api or recorder)
    print(f"    POST /messages/{message_id}/feedback {{'rating': 'down'}}")
    print(f"    feedback: {status}; stored: {await stored_rating(message_id)}")
    if run.langfuse_api is None:
        print(f"    score pushed (local recorder, no --langfuse): {recorder.scores}")
        print(f"    score ids: {recorder.score_ids}")
    else:
        print("    score -1 pushed to Langfuse (user_feedback, best effort)")
    return status


async def _read_retrievals(
    run: _Run, trace_id: str, wait: float
) -> tuple[str, list[RetrievalTrace]]:
    """Langfuse first with `--langfuse` (when configured), else the in-memory spans."""
    retrievals: list[RetrievalTrace] = []
    if run.langfuse_api is not None and run.langfuse_api.enabled:
        retrievals = await poll_langfuse(run.settings, trace_id, wait)
        if retrievals:
            return "Langfuse public API", retrievals
        print("    the trace is not in Langfuse yet; using the in-memory spans")
    elif run.langfuse_api is not None:
        print("    --langfuse without LANGFUSE_* settings; using the in-memory spans")
    spans = run.exporter.get_finished_spans()
    return "in-memory span exporter", retrievals_from_spans(spans, trace_id)


def _print_retrievals(retrievals: Sequence[RetrievalTrace]) -> None:
    for retrieval in retrievals:
        ranked = zip(retrieval.doc_keys, retrieval.chunk_ids, retrieval.scores, strict=False)
        for rank, (doc_key, chunk_id, score) in enumerate(ranked, start=1):
            print(f"      {rank}. {doc_key} chunk {chunk_id} score {score:.4f}")


def find_culprit(
    retrievals: Sequence[RetrievalTrace], cited: Sequence[str]
) -> tuple[str, int] | None:
    """The first retrieved `(doc_key, chunk_id)` that the answer cited."""
    return next(
        (
            (doc_key, chunk_id)
            for retrieval in retrievals
            for doc_key, chunk_id in zip(retrieval.doc_keys, retrieval.chunk_ids, strict=False)
            if doc_key in cited
        ),
        None,
    )


async def _step_trace(run: _Run, answer: _Answer, wait: float) -> str | None:
    step(4)
    source, retrievals = await _read_retrievals(run, answer.trace_id, wait)
    print(f"    source: {source}; retrieval spans in trace {answer.trace_id}: ")
    _print_retrievals(retrievals)
    culprit = find_culprit(retrievals, answer.output["citations"])
    if culprit is None:
        print("    culprit: none of the retrieved chunks was cited")
        return None
    print(f"    culprit: {culprit[0]} (chunk {culprit[1]})")
    print(f"    chunk text: {' '.join((await chunk_text(culprit[1])).split())}")
    return culprit[0]


async def _step_fix_and_eval(
    run: _Run, corpus: Path, answer: _Answer, dataset_out: Path
) -> tuple[EvalReport, EvalReport]:
    step(5)
    (corpus / STALE_PATH).write_text(FIXED_TEXT, encoding="utf-8")
    print(f"    fixed {STALE_PATH}: {FIXED_TEXT.splitlines()[2]}")
    print(f"    re-ingested: {await ingest(corpus)}")
    changed = upsert_case(dataset_out, DEMO_CASE)
    print(f"    case {DEMO_CASE.id} {'written to' if changed else 'already in'} {dataset_out}")
    [case] = [c for c in load_cases([dataset_out]) if c.id == DEMO_CASE.id]
    graders = build_graders(build_judge(run.provider, run.settings))
    before = await run_cases([case], StaticTarget(answer.output), graders)
    print(f"    before the fix: {_verdict(before)} - cited {answer.output['citations']}")
    llm = CitationRuleFakeLLM() if run.provider == "fake" else None
    run.target = ChatTarget(run.settings, llm=llm)
    after = await run_cases([case], run.target, graders)
    print(f"    after the fix: {_verdict(after)}")
    return before, after


async def _close(run: _Run) -> None:
    try:
        await delete_stale_document()
    finally:
        if run.target is not None:
            await run.target.aclose()
        if run.langfuse_api is not None:
            await run.langfuse_api.aclose()
        await dispose_engine()


async def run_demo(
    provider: str, settings: Settings, dataset_out: Path, *, langfuse: bool, wait: float
) -> DemoOutcome:
    settings = settings.model_copy(update={"retrieval_mode": RETRIEVAL_MODE})
    exporter = InMemorySpanExporter()
    detach = add_span_exporter(exporter)
    run = _Run(provider, settings, exporter, LangfuseApi(settings) if langfuse else None)
    try:
        with tempfile.TemporaryDirectory(prefix="atlas-demo14-") as tmp:
            corpus = await _step_corpus(Path(tmp))
            answer = await _step_question(run)
            status = await _step_feedback(run, answer)
            culprit = await _step_trace(run, answer, wait)
            before, after = await _step_fix_and_eval(run, corpus, answer, dataset_out)
            return DemoOutcome(before=before, after=after, culprit=culprit, feedback_status=status)
    finally:
        detach()
        await _close(run)


def build_parser() -> argparse.ArgumentParser:
    parser = demo_parser("Phase 14: from a negative-feedback trace to a passing eval case")
    parser.set_defaults(results=None)
    parser.add_argument(
        "--dataset-out",
        dest="dataset_out",
        type=Path,
        default=None,
        help="JSONL file the new case is written to (default: phase_14.jsonl for real, "
        "a temporary copy for fake)",
    )
    parser.add_argument(
        "--langfuse",
        action="store_true",
        help="push the score to Langfuse and read the trace back from its API (LANGFUSE_*)",
    )
    parser.add_argument(
        "--langfuse-wait",
        dest="langfuse_wait",
        type=float,
        default=60.0,
        help="seconds to wait for Langfuse to ingest the trace",
    )
    return parser


def _resolve_paths(args: argparse.Namespace) -> None:
    if args.results is None:
        args.results = DEFAULT_RESULTS_PATH if args.provider == "real" else FAKE_DIR / "results.md"
    if args.dataset_out is None:
        if args.provider == "real":
            args.dataset_out = PHASE_14_DATASET
        else:
            args.dataset_out = FAKE_DIR / PHASE_14_DATASET.name
    if args.dataset_out.resolve() != PHASE_14_DATASET.resolve() and not args.dataset_out.exists():
        # A copy starts from the versioned cases, so it loads like the real file.
        args.dataset_out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PHASE_14_DATASET, args.dataset_out)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _resolve_paths(args)
    settings = get_settings()
    outcome = asyncio.run(
        run_demo(
            args.provider,
            settings,
            args.dataset_out,
            langfuse=args.langfuse,
            wait=args.langfuse_wait,
        )
    )
    [before] = outcome.before.cases
    [after] = outcome.after.cases
    record_result(
        PHASE,
        METRIC,
        f"stale bulletin answer: {before.score:.2f}/100 ({'pass' if before.passed else 'fail'})",
        f"after the fix: {after.score:.2f}/100 ({'pass' if after.passed else 'fail'})",
        notes=(
            f"case {DEMO_CASE.id}, culprit from the retrieval span: {outcome.culprit}; "
            f"feedback route status {outcome.feedback_status}; retrieval={RETRIEVAL_MODE}; "
            f"provider={args.provider}"
        ),
        results_path=args.results,
    )
    print(f"\nRecorded phase {PHASE:02d} in {args.results}")
    ok = not before.passed and after.passed
    return EXIT_OK if ok else EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
