"""CI workflow for lint, tests and minimal evals (TASK-014, FND-02)."""

from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT_DIR = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT_DIR / ".github" / "workflows" / "ci.yml"


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    loaded: dict[Any, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return loaded


def _triggers(workflow: dict[Any, Any]) -> Any:
    # PyYAML (YAML 1.1) reads the bare key `on` as the boolean True.
    return workflow.get("on", workflow.get(True))


def _runs(job: dict[str, Any]) -> list[str]:
    return [step["run"] for step in job["steps"] if "run" in step]


def _uses(job: dict[str, Any]) -> list[str]:
    return [step["uses"] for step in job["steps"] if "uses" in step]


def test_jobs_exist(workflow: dict[str, Any]) -> None:
    assert {"python", "web", "evals-smoke"} <= set(workflow["jobs"])


def test_triggers_include_pull_request_and_push(workflow: dict[str, Any]) -> None:
    triggers = _triggers(workflow)

    assert "pull_request" in triggers
    assert "push" in triggers


def test_no_step_or_job_continues_on_error(workflow: dict[str, Any]) -> None:
    for name, job in workflow["jobs"].items():
        assert job.get("continue-on-error") is not True, name
        for step in job["steps"]:
            assert step.get("continue-on-error") is not True, (name, step)


def test_python_job_runs_lint_typecheck_and_tests(workflow: dict[str, Any]) -> None:
    job = workflow["jobs"]["python"]
    runs = _runs(job)

    assert "uv sync" in " ".join(runs)
    for command in (
        "uv run ruff check",
        "uv run ruff format --check",
        "uv run mypy",
        "uv run pytest",
    ):
        assert any(run.startswith(command) for run in runs), command
    assert any(uses.startswith("astral-sh/setup-uv") for uses in _uses(job))


def test_python_job_has_postgres_service_and_fake_llm(workflow: dict[str, Any]) -> None:
    job = workflow["jobs"]["python"]
    postgres = next(iter(job["services"].values()))

    assert postgres["image"] == "pgvector/pgvector:pg16"
    assert postgres["env"]["POSTGRES_USER"] == "atlas"
    assert "5432:5432" in postgres["ports"]
    assert job["env"]["ATLAS_DATABASE_URL"] == (
        "postgresql+psycopg://atlas:atlas@localhost:5432/atlas"
    )
    assert job["env"]["ATLAS_LLM_PROVIDER"] == "fake"


def test_web_job_lints_tests_and_builds(workflow: dict[str, Any]) -> None:
    job = workflow["jobs"]["web"]
    default_dir = job.get("defaults", {}).get("run", {}).get("working-directory")
    steps_in_web = [
        step["run"]
        for step in job["steps"]
        if "run" in step and step.get("working-directory", default_dir) == "web"
    ]

    assert any(uses.startswith("actions/setup-node") for uses in _uses(job))
    for command in ("npm ci", "npx ng lint", "npx ng test --watch=false", "npx ng build"):
        assert command in steps_in_web, command


def test_evals_smoke_runs_the_smoke_dataset_with_fake_llm(workflow: dict[str, Any]) -> None:
    job = workflow["jobs"]["evals-smoke"]
    runs = " ".join(_runs(job))

    assert "python -m evals run" in runs
    assert "evals/datasets/smoke.jsonl" in runs
    assert job["env"]["ATLAS_LLM_PROVIDER"] == "fake"


def test_workflow_uses_no_secrets() -> None:
    assert "secrets." not in WORKFLOW.read_text(encoding="utf-8")
