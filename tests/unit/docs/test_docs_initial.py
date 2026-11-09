"""Initial English README and results log (TASK-015, FND-01, DOD-04)."""

import re
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parents[3]
PHASES = [f"{phase:02d}" for phase in range(1, 15)]


def _read(relative: str) -> str:
    return (ROOT_DIR / relative).read_text(encoding="utf-8")


def _variables_without_default() -> list[str]:
    """Variables `.env.example` leaves empty: they have no safe default and must be documented."""
    names = []
    for line in _read(".env.example").splitlines():
        match = re.fullmatch(r"([A-Z][A-Z0-9_]*)=\s*", line)
        if match:
            names.append(match.group(1))
    return names


def test_env_example_declares_the_known_secrets() -> None:
    required = _variables_without_default()

    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "ATLAS_PRIMARY_MODEL"):
        assert name in required


def test_readme_has_the_quickstart() -> None:
    readme = _read("README.md")

    assert "cp .env.example .env" in readme
    assert "docker compose up" in readme
    assert "http://localhost:8080" in readme


@pytest.mark.parametrize("name", _variables_without_default())
def test_readme_documents_each_required_variable(name: str) -> None:
    readme = _read("README.md")

    assert re.search(rf"^\|\s*`{name}`\s*\|", readme, flags=re.MULTILINE), name


def test_readme_lists_the_dev_gates() -> None:
    readme = _read("README.md")

    for command in (
        "uv sync",
        "docker compose up -d db",
        "uv run ruff check . --output-format=concise",
        "uv run mypy",
        "uv run pytest -q",
        "npx ng test --watch=false",
        "python -m evals run",
    ):
        assert command in readme, command


def test_results_log_has_one_row_per_phase() -> None:
    lines = _read("docs/results.md").splitlines()

    assert "| Phase | Metric | Before | After | Notes |" in lines
    rows = [line for line in lines if re.match(r"^\|\s*\d{2}\s*\|", line)]
    assert [line.split("|")[1].strip() for line in rows] == PHASES
    for row in rows:
        cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
        assert len(cells) == 5
        # A row is either empty (phase not measured yet) or filled by its demo with a metric.
        assert cells[1:] == ["", "", "", ""] or cells[1] != ""


def test_adr_template_has_the_required_sections() -> None:
    template = _read("docs/adr/0000-template.md")

    for section in ("Context", "Decision", "Alternatives considered", "Consequences"):
        assert re.search(rf"^## {section}$", template, flags=re.MULTILINE), section
