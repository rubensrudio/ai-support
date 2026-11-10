"""Phase 13 demo: the eval gate blocks a degraded `prompts/system.md` (TASK-130, EVAL-06,
DOD-02).

The demo edits the system prompt file in place for the degraded run. These tests point
`api.prompts.PROMPTS_DIR` at a copy of `prompts/` (the checkout is shared, and a degraded
`prompts/system.md` must never be visible to anything else), prove the copy is restored byte
for byte, also when the run fails or is interrupted, and that the real file never changes.
"""

import hashlib
import json
import shutil
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete

import api.prompts
from api.chat.prompt_builder import grounded_system_prompt
from api.config import get_settings
from api.db.engine import dispose_engine, session_scope
from api.llm.client import ChatMessage, LLMRequest, SystemBlock
from api.prompts import load_prompt
from api.rag.models import TS_CONFIGS, Chunk, Document
from demos import demo_13_eval_gate as demo
from demos.common import DEFAULT_RESULTS_PATH
from demos.demo_13_eval_gate import (
    CitationRuleFakeLLM,
    has_citation_rule,
    main,
    prompt_path,
    strip_citation_rule,
)
from evals.dataset import EvalCase
from evals.runner import CaseResult, EvalReport, EvalScores

ROOT_DIR = Path(__file__).resolve().parents[3]
REAL_PROMPT = ROOT_DIR / "prompts" / "system.md"
DOC_PREFIX = "demo13-test-"
KB: dict[str, tuple[str, str, str, str]] = {
    # doc_key suffix: (language, title, content, question)
    "pause-en": (
        "en",
        "Pause your subscription",
        "You can pause your subscription for up to three months from the Plan page. "
        "No box is shipped while the subscription is paused.",
        "Can I pause my subscription for a few months?",
    ),
    "pause-pt": (
        "pt",
        "Pausar a assinatura",
        "Você pode pausar a sua assinatura por até três meses na página Plano. "
        "Nenhuma caixa é enviada enquanto a assinatura estiver pausada.",
        "Posso pausar a minha assinatura por alguns meses?",
    ),
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _row(path: Path) -> list[str]:
    rows = [line for line in path.read_text(encoding="utf-8").splitlines() if line[:6] == "| 13 |"]
    assert len(rows) == 1, rows
    return [cell.strip() for cell in rows[0].strip().strip("|").split("|")]


@pytest.fixture(autouse=True)
def fake_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("ATLAS_LLM_PROVIDER", "fake")
    monkeypatch.setenv("ATLAS_EMBEDDING_PROVIDER", "fake")
    monkeypatch.setenv("ATLAS_RETRIEVAL_MODE", "fulltext")
    for name in ("ATLAS_GUARDRAIL_MODEL", "ATLAS_JUDGE_MODEL", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def fake_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The demo's fake results and reports go to this test's own folder, never to the shared
    `<tmp>/atlas-demo13/` (no collision between pytest sessions)."""
    path = tmp_path / "atlas-demo13"
    monkeypatch.setattr(demo, "FAKE_DIR", path)
    return path


@pytest.fixture(autouse=True)
def prompts_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A private copy of `prompts/`; yields the copy of `system.md`."""
    real_hash = _sha256(REAL_PROMPT)
    copy_dir = tmp_path / "prompts"
    shutil.copytree(REAL_PROMPT.parent, copy_dir)
    monkeypatch.setattr(api.prompts, "PROMPTS_DIR", copy_dir)
    load_prompt.cache_clear()
    yield copy_dir / "system.md"
    load_prompt.cache_clear()
    assert _sha256(REAL_PROMPT) == real_hash, "the real prompts/system.md changed"


# --- prompt surgery ----------------------------------------------------------------------------


def test_the_demo_edits_prompts_system_md_through_the_prompt_loader_dir(
    prompts_copy: Path,
) -> None:
    assert prompt_path() == prompts_copy
    assert prompt_path().read_bytes() == REAL_PROMPT.read_bytes()


def test_strip_citation_rule_removes_only_the_citation_bullet() -> None:
    original = REAL_PROMPT.read_text(encoding="utf-8")
    degraded = strip_citation_rule(original)

    assert has_citation_rule(original)
    assert not has_citation_rule(degraded)
    assert "[n]" not in degraded and "Cite the excerpt" not in degraded
    removed = [line for line in original.splitlines() if line not in degraded.splitlines()]
    removed_text = "\n".join(removed)
    assert "Cite the excerpt" in removed_text and "[n]" in removed_text, removed
    assert len(degraded.splitlines()) == len(original.splitlines()) - len(removed)
    # Every other rule stays: grounding, "I don't know", conduct, orders.
    for kept in ("Answer only with information", "say that you don't know", "#id", "Never reveal"):
        assert kept in degraded, kept


def test_strip_citation_rule_refuses_a_prompt_without_the_rule() -> None:
    degraded = strip_citation_rule(REAL_PROMPT.read_text(encoding="utf-8"))

    with pytest.raises(ValueError, match="citation rule"):
        strip_citation_rule(degraded)


async def test_fake_llm_cites_only_when_the_system_prompt_has_the_citation_rule() -> None:
    llm = CitationRuleFakeLLM()
    documents = (
        '<untrusted_document index="1" source="Pause">You can pause it. More text.'
        "</untrusted_document>"
    )
    original = load_prompt("system")

    def request(system: str) -> LLMRequest:
        return LLMRequest(
            system=[SystemBlock(text=system)],
            messages=[ChatMessage(role="user", content=f"{documents}\nCan I pause?")],
        )

    cited = await llm.complete(request(original))
    uncited = await llm.complete(request(strip_citation_rule(original)))

    assert cited.text == "You can pause it. [1]"
    assert uncited.text == "You can pause it."
    assert llm.provider == "fake"


# --- restore on failure -------------------------------------------------------------------------


def _report(score: float) -> EvalReport:
    case = CaseResult(
        id="demo13-unit-rag-en-001",
        category="rag",
        language="en",
        score=score,
        passed=score == 100.0,
        details={},
    )
    return EvalReport(
        run_id=f"run-{score}",
        status="ok",
        cases=[case],
        scores=EvalScores(overall=score, by_category={"rag": score}),
    )


class _Runs:
    """Stand-in for `run_evals`: scores 100 with the citation rule, 50 without, and records
    the prompt each run saw; `fail_with` is raised by the degraded run."""

    def __init__(self, fail_with: BaseException | None = None) -> None:
        self.fail_with = fail_with
        self.prompts: list[str] = []

    async def __call__(self, *args: Any, **kwargs: Any) -> EvalReport:
        system = grounded_system_prompt()  # what the chat service sends right now
        self.prompts.append(system)
        if has_citation_rule(system):
            return _report(100.0)
        if self.fail_with is not None:
            raise self.fail_with
        return _report(50.0)


def _unit_cases() -> list[EvalCase]:
    return [
        EvalCase(
            id="demo13-unit-rag-en-001",
            phase=13,
            category="rag",
            language="en",
            input={"message": "Can I pause?"},
            expected={"citation_doc_key": "pause"},
            grader="rag",
        )
    ]


def test_without_a_database_the_degraded_run_is_blocked_and_the_prompt_restored(
    prompts_copy: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = _Runs()
    monkeypatch.setattr(demo, "run_evals", runs)
    monkeypatch.setattr(demo, "load_demo_cases", lambda *args: _unit_cases())
    original = prompts_copy.read_bytes()
    results = tmp_path / "results.md"

    exit_code = main(["--provider", "fake", "--results", str(results)])

    assert exit_code == 0
    assert [has_citation_rule(system) for system in runs.prompts] == [True, False]
    assert prompts_copy.read_bytes() == original
    assert load_prompt("system") == original.decode("utf-8")
    metric, before, after, notes = _row(results)[1:]
    assert "100.00" in before and "50.00" in after
    assert "regression" in after and "exit 1" in after
    assert "PR: <link>" in notes


@pytest.mark.parametrize("failure", [RuntimeError("provider down"), KeyboardInterrupt()])
def test_the_prompt_is_restored_when_the_degraded_run_fails(
    failure: BaseException,
    prompts_copy: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs = _Runs(fail_with=failure)
    monkeypatch.setattr(demo, "run_evals", runs)
    monkeypatch.setattr(demo, "load_demo_cases", lambda *args: _unit_cases())
    original = prompts_copy.read_bytes()
    results = tmp_path / "results.md"

    with pytest.raises(type(failure)):
        main(["--provider", "fake", "--results", str(results)])

    assert len(runs.prompts) == 2 and not has_citation_rule(runs.prompts[1])
    assert prompts_copy.read_bytes() == original
    assert load_prompt("system") == original.decode("utf-8")
    assert not results.exists()


def test_a_fake_run_without_results_never_touches_docs_results_md(
    fake_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(demo, "run_evals", _Runs())
    monkeypatch.setattr(demo, "load_demo_cases", lambda *args: _unit_cases())
    before = DEFAULT_RESULTS_PATH.read_bytes()

    assert main(["--provider", "fake"]) == 0

    assert DEFAULT_RESULTS_PATH.read_bytes() == before
    assert _row(fake_dir / "results.md")[0] == "13"
    for name in ("base-report.json", "degraded-report.json", "eval-gate.md"):
        assert (fake_dir / name).is_file(), name


def test_default_dataset_is_the_rag_cases_of_core_and_phase_03() -> None:
    cases = demo.load_demo_cases(demo.DEFAULT_DATASET, demo.DEFAULT_CATEGORIES)

    assert cases
    assert {case.category for case in cases} == {"rag"}
    assert {case.language for case in cases} == {"pt", "en"}
    assert {case.id.split("-")[0] for case in cases} == {"core", "phase03"}


# --- end to end with the chat service --------------------------------------------------------


@pytest.fixture
async def knowledge_base(migrated_database: str) -> AsyncIterator[None]:
    """Two indexed documents in the test database, committed so the demo sessions see them."""
    async with session_scope() as session:
        for key, (language, title, content, _) in KB.items():
            document = Document(
                doc_key=f"{DOC_PREFIX}{key}",
                title=title,
                language=language,
                doc_type="policy",
                format="md",
                source_path=f"corpus/{language}/{key}.md",
                content_hash=f"{DOC_PREFIX}{key}",
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


@pytest.fixture
def rag_dataset(tmp_path: Path) -> Path:
    path = tmp_path / "demo13_rag.jsonl"
    lines = []
    for key, (language, _, _, question) in KB.items():
        case = {
            "id": f"demo13-rag-{key}",
            "phase": 13,
            "category": "rag",
            "language": language,
            "input": {"message": question, "persona": "alex" if language == "en" else "bia"},
            "expected": {"citation_doc_key": f"{DOC_PREFIX}{key}"},
            "grader": "rag",
        }
        lines.append(json.dumps(case, ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.mark.integration
@pytest.mark.usefixtures("knowledge_base")
def test_fake_chat_run_gate_exits_1_for_the_degraded_prompt_and_records_row_13(
    prompts_copy: Path,
    rag_dataset: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original = prompts_copy.read_bytes()
    results = tmp_path / "results.md"

    exit_code = main(
        ["--provider", "fake", "--dataset", str(rag_dataset), "--results", str(results)]
    )

    output = capsys.readouterr().out
    assert exit_code == 0, output
    assert "gate exit code: 1 (regression)" in output
    assert prompts_copy.read_bytes() == original
    metric, before, after, notes = _row(results)[1:]
    assert "Eval gate" in metric
    # Citation 100 + faithfulness 100 with the rule; citation 0 + faithfulness 100 without.
    assert "overall 100.00/100" in before
    assert "overall 50.00/100" in after
    assert "regression (exit 1" in after
    assert "provider=fake" in notes and "PR: <link>" in notes
