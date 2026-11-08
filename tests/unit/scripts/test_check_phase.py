"""Phase definition-of-done checker (TASK-016, CT-11, DOD-01)."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.check_phase import check_phase, main

GIT = shutil.which("git")

ENGLISH_ADR = """# ADR 0001: Call the LLM through our own client

## Context

The support agent must talk to a large language model. We want to understand every request
and response, so the application owns the client instead of hiding it behind a framework.

## Decision

We define a small client protocol with one adapter per provider. The API key is read from an
environment variable and never written to logs or source control.

## Alternatives considered

Using an agent framework would hide the request format, which defeats the learning goal of
the project. Calling the provider SDK everywhere would couple the code to one vendor.

## Consequences

Each provider needs an adapter, but tests can use a scripted fake client.
"""

PORTUGUESE_ADR = """# ADR 0001: Chamar o modelo pelo nosso próprio cliente

## Context

O agente de suporte precisa conversar com um modelo de linguagem. Queremos entender cada
requisição e cada resposta, então a aplicação é dona do cliente em vez de escondê-lo.

## Decision

Definimos um protocolo pequeno de cliente com um adaptador por provedor. A chave da API é lida
de uma variável de ambiente e nunca é gravada em logs nem no controle de versão.

## Alternatives considered

Usar um framework de agentes esconderia o formato das requisições, o que vai contra o objetivo
de aprendizado do projeto. Chamar o SDK do provedor em todo lugar acoplaria o código.
"""

RESULTS_HEADER = "| Phase | Metric | Before | After | Notes |\n|---|---|---|---|---|\n"


def _results(rows: dict[int, tuple[str, str, str]]) -> str:
    lines = []
    for phase in range(1, 15):
        metric, before, after = rows.get(phase, ("", "", ""))
        lines.append(f"| {phase:02d} | {metric} | {before} | {after} |  |")
    return "# Results\n\n" + RESULTS_HEADER + "\n".join(lines) + "\n"


def _make_ready_repo(root: Path, phase: int = 1) -> Path:
    nn = f"{phase:02d}"
    datasets = root / "evals" / "datasets"
    datasets.mkdir(parents=True)
    case = {
        "id": f"phase-{nn}-en-001",
        "phase": phase,
        "category": "tone",
        "language": "en",
        "input": {"message": "Hello"},
        "expected": {"non_empty": True},
        "grader": "exact_match",
    }
    (datasets / f"phase_{nn}.jsonl").write_text(json.dumps(case) + "\n", encoding="utf-8")
    (root / "demos").mkdir()
    (root / "demos" / f"demo_{nn}_stateless.py").write_text("print('demo')\n", encoding="utf-8")
    (root / "docs" / "adr").mkdir(parents=True)
    (root / "docs" / "adr" / f"00{nn}-llm-api-call.md").write_text(ENGLISH_ADR, encoding="utf-8")
    (root / "docs" / "results.md").write_text(
        _results({phase: ("Turns remembered", "0", "0")}), encoding="utf-8"
    )
    return root


def test_ready_phase_exits_0(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _make_ready_repo(tmp_path)

    code = main(["01", "--root", str(root)])

    assert code == 0
    assert "ready" in capsys.readouterr().out.lower()


def test_missing_artifacts_exit_1_listing_each(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "results.md").write_text(_results({}), encoding="utf-8")

    code = main(["01", "--root", str(tmp_path)])

    out = capsys.readouterr().out
    assert code == 1
    assert "eval cases" in out
    assert "demos/demo_01_*.py" in out
    assert "docs/results.md" in out
    assert "docs/adr/0001-*.md" in out


@pytest.mark.parametrize(
    "remove",
    [
        "evals/datasets/phase_01.jsonl",
        "demos/demo_01_stateless.py",
        "docs/adr/0001-llm-api-call.md",
    ],
)
def test_each_missing_artifact_is_reported_alone(tmp_path: Path, remove: str) -> None:
    root = _make_ready_repo(tmp_path)
    (root / remove).unlink()

    missing = check_phase(1, root)

    assert len(missing) == 1


def test_cases_of_another_phase_do_not_count(tmp_path: Path) -> None:
    root = _make_ready_repo(tmp_path, phase=2)
    (root / "demos" / "demo_01_x.py").write_text("", encoding="utf-8")
    (root / "docs" / "adr" / "0001-x.md").write_text(ENGLISH_ADR, encoding="utf-8")
    (root / "docs" / "results.md").write_text(
        _results({1: ("m", "1", "2"), 2: ("m", "1", "2")}), encoding="utf-8"
    )

    missing = check_phase(1, root)

    assert len(missing) == 1
    assert "phase == 1" in missing[0]


@pytest.mark.parametrize(
    ("before", "after"), [("", "5"), ("5", ""), ("", "")], ids=["no-before", "no-after", "empty"]
)
def test_results_row_needs_before_and_after(tmp_path: Path, before: str, after: str) -> None:
    root = _make_ready_repo(tmp_path)
    (root / "docs" / "results.md").write_text(_results({1: ("m", before, after)}), encoding="utf-8")

    missing = check_phase(1, root)

    assert len(missing) == 1
    assert "docs/results.md" in missing[0]


def test_portuguese_adr_fails_language_check(tmp_path: Path) -> None:
    root = _make_ready_repo(tmp_path)
    (root / "docs" / "adr" / "0001-llm-api-call.md").write_text(PORTUGUESE_ADR, encoding="utf-8")

    missing = check_phase(1, root)

    assert len(missing) == 1
    assert "English" in missing[0]
    assert "'pt'" in missing[0]


def test_adr_without_required_sections_fails(tmp_path: Path) -> None:
    root = _make_ready_repo(tmp_path)
    adr = ENGLISH_ADR.replace("## Alternatives considered", "## Options")
    (root / "docs" / "adr" / "0001-llm-api-call.md").write_text(adr, encoding="utf-8")

    missing = check_phase(1, root)

    assert len(missing) == 1
    assert "Alternatives" in missing[0]


def test_invalid_dataset_is_reported(tmp_path: Path) -> None:
    root = _make_ready_repo(tmp_path)
    (root / "evals" / "datasets" / "broken.jsonl").write_text("{bad\n", encoding="utf-8")

    missing = check_phase(1, root)

    assert len(missing) == 1
    assert "broken.jsonl:1" in missing[0]


@pytest.mark.skipif(GIT is None, reason="git is not installed")
def test_require_tag_checks_the_phase_tag(tmp_path: Path) -> None:
    assert GIT is not None
    root = _make_ready_repo(tmp_path)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }

    def git(*args: str) -> None:
        subprocess.run([GIT, "-C", str(root), *args], check=True, capture_output=True, env=env)  # noqa: S603

    git("init", "-q")
    git("commit", "-q", "--allow-empty", "-m", "init")

    assert check_phase(1, root, require_tag=False) == []
    without_tag = check_phase(1, root, require_tag=True)
    assert len(without_tag) == 1
    assert "phase-01" in without_tag[0]

    git("tag", "phase-01")

    assert check_phase(1, root, require_tag=True) == []
    assert main(["01", "--root", str(root), "--require-tag"]) == 0


def test_invalid_phase_number_is_a_usage_error(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as error:
        main(["15", "--root", str(tmp_path)])

    assert error.value.code == 2


def test_escaped_pipe_in_a_cell_does_not_shift_columns(tmp_path: Path) -> None:
    root = _make_ready_repo(tmp_path)
    (root / "docs" / "results.md").write_text(
        _results({1: (r"turns \| tokens", "", "5")}), encoding="utf-8"
    )

    missing = check_phase(1, root)

    assert len(missing) == 1
    assert "missing Before" in missing[0]
