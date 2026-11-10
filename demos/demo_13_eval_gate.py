"""Phase 13 demo: the eval gate blocks a degraded system prompt (EVAL-05, EVAL-06, EVAL-07,
DOD-02).

1. **Cases.** `--dataset` (CT-10 JSONL file or glob, `{a,b}` alternatives allowed) filtered by
   `--categories` (comma separated, `all` keeps every category). Default: the `rag` cases of
   `core_en.jsonl`, `core_pt.jsonl` and `phase_03.jsonl`, graded by citation + faithfulness.
   As with `python -m evals run --target auto`, smoke cases that are not red-team controls
   are left out.
2. **Base run.** The cases run in process (`evals.targets.AutoTarget`: chat cases through
   `ChatService`, triage cases through `TriageService`) with the current `prompts/system.md`.
3. **Degraded run.** `prompts/system.md` is rewritten in place WITHOUT the citation rule (the
   bullet that asks for `[n]` markers, `strip_citation_rule`), the prompt cache of
   `api.prompts.load_prompt` is cleared and the same cases run again. The original bytes are
   written back in a `finally` (also on an error, Ctrl-C or SIGTERM) and the cache is cleared
   again; the restored file is checked byte for byte. The degraded prompt only ever exists
   during that run: never commit it.
4. **Gate.** `evals.gate.evaluate(current=degraded, base=base)` applies the merge rule of the
   CI (overall drop > 2.0 points -> `regression`, exit 1). The markdown report of the PR
   comment (`evals.report.render_markdown`) is printed.
5. **Results.** The `| 13 |` row of `docs/results.md`: Before = overall score with the current
   prompt, After = overall score of the degraded prompt and the gate verdict. The notes keep a
   `PR: <link>` slot that the Operator fills with the real regression PR (EVAL-06, plan
   section 16).

`--provider fake` runs offline: `CitationRuleFakeLLM` answers with the first sentence of the
first excerpt it was given and appends `[1]` only when the system prompt carries the citation
rule, and the faithfulness judge is the offline rubric judge of demo 03 (`RubricFakeJudge`). The
fake run therefore proves the mechanism (rule removed -> no citations -> gate blocks), not the
behaviour of a model. `real` uses the configured provider (`claude_cli` works locally) for the
chat and `ATLAS_JUDGE_MODEL` (or the chat model) for the judges. A fake run writes its results
under `<tmp>/atlas-demo13/` unless `--results` is given, so it never replaces the real row; the
two reports and the markdown also go to that folder.

Warning: the chat cases reset the personas' orders and subscription in the configured database
(`ATLAS_DATABASE_URL`), exactly like `python -m api.seed`, and the RAG cases need the corpus
ingested there. Do not start an API process while the demo runs: it would read the degraded
prompt from disk.

Exit code: 0 when the gate blocked the degraded prompt (the expected outcome), 1 when it did
not, 2 when a run had execution errors (gate verdict `error`).

Usage:
    uv run python -m demos.demo_13_eval_gate --provider fake --results /tmp/results.md
    uv run python -m demos.demo_13_eval_gate --provider real   # writes docs/results.md
"""

import argparse
import asyncio
import html
import re
import signal
import sys
import tempfile
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import FrameType

import api.prompts
from api.config import Settings, get_settings
from api.llm.client import LLMClient, LLMRequest, LLMResponse, LLMStreamEvent, Usage
from api.llm.factory import build_llm_client
from api.llm.fake import FAKE_MODEL, FAKE_PROVIDER
from api.prompts import load_prompt
from demos.common import DEFAULT_RESULTS_PATH, ROOT_DIR, demo_parser, record_result
from demos.demo_03_rag import RubricFakeJudge
from evals.__main__ import build_graders, dataset_paths, excluded_for_auto
from evals.dataset import EvalCase, load_cases
from evals.gate import MAX_OVERALL_DROP, GateResult, evaluate
from evals.report import render_markdown
from evals.runner import EvalReport, run_cases
from evals.targets import AutoTarget, ChatTarget, TriageTarget

PHASE = 13
METRIC = (
    "Eval gate: overall eval score (0-100), current prompts/system.md vs the same prompt "
    "without the citation rule"
)
SYSTEM_PROMPT = "system"
DEFAULT_DATASET = str(ROOT_DIR / "evals" / "datasets" / "{core_en,core_pt,phase_03}.jsonl")
DEFAULT_CATEGORIES: tuple[str, ...] = ("rag",)
ALL_CATEGORIES = "all"
PR_SLOT = "PR: <link>"
# Fake results and the run reports; read at call time, so tests can monkeypatch it.
FAKE_DIR = Path(tempfile.gettempdir()) / "atlas-demo13"

EXIT_BLOCKED = 0
EXIT_NOT_BLOCKED = 1
EXIT_ERROR = 2

# The citation rule: a bullet of the system prompt that asks for the `[n]` markers.
CITATION_MARKER = "[n]"
_BULLET = re.compile(r"^\s*- ")
_DOCUMENT = re.compile(
    r'<untrusted_document index="(\d+)"[^>]*>(.*?)</untrusted_document>', re.DOTALL
)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_REFUSAL: dict[str, str] = {
    "en": "I don't know: I could not find this in the Assinaflow knowledge base.",
    "pt": "Não sei: não encontrei essa informação na base de conhecimento da Assinaflow.",
}


# --- prompt surgery ----------------------------------------------------------------------------


def prompt_path() -> Path:
    """`prompts/system.md`, in the directory `api.prompts.load_prompt` reads right now."""
    return api.prompts.PROMPTS_DIR / f"{SYSTEM_PROMPT}.md"


def _bullets(lines: list[str]) -> Iterator[tuple[int, int]]:
    """`(start, end)` line ranges of every bullet: its `- ` line plus indented continuations."""
    index = 0
    while index < len(lines):
        if not _BULLET.match(lines[index]):
            index += 1
            continue
        end = index + 1
        while (
            end < len(lines)
            and lines[end].strip()
            and lines[end][:1].isspace()
            and not _BULLET.match(lines[end])
        ):
            end += 1
        yield index, end
        index = end


def _citation_bullets(lines: list[str]) -> list[tuple[int, int]]:
    return [
        (start, end)
        for start, end in _bullets(lines)
        if CITATION_MARKER in "".join(lines[start:end])
    ]


def has_citation_rule(prompt: str) -> bool:
    """Whether the prompt has a bullet asking for `[n]` citation markers."""
    return bool(_citation_bullets(prompt.splitlines(keepends=True)))


def strip_citation_rule(prompt: str) -> str:
    """The prompt without its citation bullet(s); every other line is kept as is.

    Raises `ValueError` when there is no citation rule (the base prompt would already be the
    degraded one, and the comparison would mean nothing)."""
    lines = prompt.splitlines(keepends=True)
    ranges = _citation_bullets(lines)
    if not ranges:
        raise ValueError(f"no citation rule (a bullet with {CITATION_MARKER!r}) in the prompt")
    removed = {index for start, end in ranges for index in range(start, end)}
    return "".join(line for index, line in enumerate(lines) if index not in removed)


def _raise_interrupt(signum: int, frame: FrameType | None) -> None:
    raise KeyboardInterrupt(f"signal {signum}")


@contextmanager
def degraded_prompt(path: Path) -> Iterator[str]:
    """Rewrite `path` without the citation rule for the duration of the block, then write the
    original bytes back, whatever happens (error, Ctrl-C, SIGTERM). Yields the degraded text."""
    original = path.read_bytes()
    degraded = strip_citation_rule(original.decode("utf-8"))
    previous_handler = signal.signal(signal.SIGTERM, _raise_interrupt)
    try:
        path.write_bytes(degraded.encode("utf-8"))
        load_prompt.cache_clear()
        print(f"Temporarily removed the citation rule from {path} (never commit this state)")
        yield degraded
    finally:
        path.write_bytes(original)
        load_prompt.cache_clear()
        signal.signal(signal.SIGTERM, previous_handler)
        if path.read_bytes() != original:  # pragma: no cover - only on a broken filesystem
            raise RuntimeError(f"{path} could not be restored; run `git checkout -- {path}`")
        print(f"Restored {path}")


# --- fake provider ---------------------------------------------------------------------------


def _response(text: str) -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=[],
        usage=Usage(input_tokens=0, output_tokens=0),
        model=FAKE_MODEL,
        provider=FAKE_PROVIDER,
        stop_reason="end_turn",
    )


class CitationRuleFakeLLM:
    """Offline chat model that cites only when told to.

    It answers with the first sentence of the first `<untrusted_document>` of the last user
    message and appends `[<index>]` only when a system block carries the citation rule
    (`has_citation_rule`). With no document it declines, in the reply language. Requests that
    are not chat turns (summaries) get a fixed text. `provider` is `fake`, so the chat service
    uses its offline topic classifier for the guardrail."""

    provider = FAKE_PROVIDER

    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if request.purpose != "chat":
            return _response("Summary of the earlier conversation.")
        system = "\n".join(block.text for block in request.system)
        last = request.messages[-1].content if request.messages else ""
        documents = [
            (index, html.unescape(content).strip())
            for index, content in _DOCUMENT.findall(last)
            if content.strip()
        ]
        if not documents:
            language = "pt" if "Brazilian Portuguese" in system else "en"
            return _response(_REFUSAL[language])
        index, content = documents[0]
        sentence = _SENTENCE_END.split(content, maxsplit=1)[0]
        if has_citation_rule(system):
            return _response(f"{sentence} [{index}]")
        return _response(sentence)

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        return self._stream(request)

    async def _stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        response = await self.complete(request)
        yield LLMStreamEvent(kind="text_delta", text=response.text)
        yield LLMStreamEvent(kind="usage", usage=response.usage)
        yield LLMStreamEvent(kind="stop", stop_reason=response.stop_reason)


# --- runs --------------------------------------------------------------------------------------


def load_demo_cases(dataset: str, categories: Sequence[str]) -> list[EvalCase]:
    """The cases of `dataset` in `categories` (`all` keeps every one), without the smoke
    cases the in-process target cannot measure (`excluded_for_auto`)."""
    paths = dataset_paths(dataset)
    if not paths:
        raise ValueError(f"no dataset file matches {dataset!r}")
    cases = [case for case in load_cases(paths) if not excluded_for_auto(case)]
    if ALL_CATEGORIES not in categories:
        cases = [case for case in cases if case.category in categories]
    if not cases:
        raise ValueError(f"no case of {dataset!r} in categories {list(categories)}")
    return cases


def build_target(provider: str, settings: Settings) -> AutoTarget:
    if provider == "fake":
        llm = CitationRuleFakeLLM()
        return AutoTarget(
            settings,
            chat=ChatTarget(settings, llm=llm),
            triage=TriageTarget(settings, llm=llm),
        )
    return AutoTarget(settings)


def build_judge(provider: str, settings: Settings) -> LLMClient:
    if provider == "fake":
        return RubricFakeJudge()
    if settings.judge_model:
        settings = settings.model_copy(update={"primary_model": settings.judge_model})
    return build_llm_client(settings)


async def run_evals(cases: list[EvalCase], provider: str, settings: Settings) -> EvalReport:
    """One eval run with the prompt currently on disk; a fresh target and judge every time."""
    target = build_target(provider, settings)
    try:
        return await run_cases(cases, target, build_graders(build_judge(provider, settings)))
    finally:
        await target.aclose()  # the engine's pooled connections belong to this event loop


# --- report ----------------------------------------------------------------------------------


def _score(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def _categories(report: EvalReport) -> str:
    return ", ".join(
        f"{name} {score:.2f}" for name, score in sorted(report.scores.by_category.items())
    )


def before_cell(base: EvalReport) -> str:
    return (
        f"current prompt: overall {base.scores.overall:.2f}/100 over {len(base.cases)} cases "
        f"({_categories(base)})"
    )


def after_cell(degraded: EvalReport, result: GateResult) -> str:
    delta = "n/a" if result.overall_delta is None else f"{result.overall_delta:+.2f}"
    return (
        f"without the citation rule: overall {degraded.scores.overall:.2f}/100 "
        f"(delta {delta}); gate {result.verdict} (exit {result.exit_code}, "
        f"limit {MAX_OVERALL_DROP:.2f} points)"
    )


def _provider_note(provider: str, settings: Settings) -> str:
    if provider == "fake":
        return (
            "provider=fake (scripted model that cites [n] only when the system prompt asks for "
            "it; offline rubric judge)"
        )
    judge = settings.judge_model or "the chat model"
    return f"provider={settings.llm_provider}, judge on {judge}"


def build_parser() -> argparse.ArgumentParser:
    parser = demo_parser("Phase 13: the eval gate blocks a prompt without the citation rule")
    parser.set_defaults(results=None)
    parser.add_argument(
        "--dataset", default=DEFAULT_DATASET, help="CT-10 JSONL file or glob ({a,b} ok)"
    )
    parser.add_argument(
        "--categories",
        default=",".join(DEFAULT_CATEGORIES),
        help=f"comma-separated case categories to run, or {ALL_CATEGORIES!r}",
    )
    return parser


def _write_reports(base: EvalReport, degraded: EvalReport, markdown: str) -> None:
    FAKE_DIR.mkdir(parents=True, exist_ok=True)
    (FAKE_DIR / "base-report.json").write_text(base.model_dump_json(indent=2), encoding="utf-8")
    (FAKE_DIR / "degraded-report.json").write_text(
        degraded.model_dump_json(indent=2), encoding="utf-8"
    )
    (FAKE_DIR / "eval-gate.md").write_text(markdown, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.results is None:
        # A fake run never replaces the real `| 13 |` row (same rule as demos 03 to 12).
        args.results = DEFAULT_RESULTS_PATH if args.provider == "real" else FAKE_DIR / "results.md"
    categories = [name.strip() for name in args.categories.split(",") if name.strip()]

    settings = get_settings()
    cases = load_demo_cases(args.dataset, categories)
    print(f"Running {len(cases)} eval case(s) twice ({', '.join(categories)})")

    load_prompt.cache_clear()
    base = asyncio.run(run_evals(cases, args.provider, settings))
    print(f"[base] current prompt: overall {base.scores.overall:.2f}/100 ({base.status})")
    with degraded_prompt(prompt_path()):
        degraded = asyncio.run(run_evals(cases, args.provider, settings))
    print(
        f"[degraded] without the citation rule: overall {degraded.scores.overall:.2f}/100 "
        f"({degraded.status})"
    )

    result = evaluate(degraded, base)
    markdown = render_markdown(result, degraded)
    _write_reports(base, degraded, markdown)
    print(markdown)
    print(f"gate exit code: {result.exit_code} ({result.verdict}) - {result.message}")

    record_result(
        PHASE,
        METRIC,
        before_cell(base),
        after_cell(degraded, result),
        notes=(
            f"Base = run with the current prompts/system.md; current = the same run with the "
            f"citation bullet removed from the file for that run only (restored afterwards, "
            f"never committed); gate rule of evals.yml: block when the overall score drops more "
            f"than {MAX_OVERALL_DROP:.2f} points; categories={','.join(categories)}; "
            f"{_provider_note(args.provider, settings)}; reports in {FAKE_DIR}; {PR_SLOT}"
        ),
        results_path=args.results,
    )
    print(f"Recorded phase {PHASE:02d} in {args.results}")
    if result.verdict == "error":
        return EXIT_ERROR
    return EXIT_BLOCKED if result.verdict == "regression" else EXIT_NOT_BLOCKED


if __name__ == "__main__":
    sys.exit(main())
