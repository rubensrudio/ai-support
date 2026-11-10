"""Evals workflow with path filter, baseline artifact and merge gate (TASK-129, EVAL-03..05/90)."""

import glob
import re
import shlex
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT_DIR = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT_DIR / ".github" / "workflows" / "evals.yml"
DEFAULT_BRANCH = "develop"


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    loaded: dict[Any, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return loaded


@pytest.fixture(scope="module")
def job(workflow: dict[str, Any]) -> dict[str, Any]:
    jobs: dict[str, Any] = workflow["jobs"]
    assert list(jobs) == ["evals"]
    evals_job: dict[str, Any] = jobs["evals"]
    return evals_job


def _triggers(workflow: dict[Any, Any]) -> Any:
    # PyYAML (YAML 1.1) reads the bare key `on` as the boolean True.
    return workflow.get("on", workflow.get(True))


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = job["steps"]
    return steps


def _step_running(job: dict[str, Any], command: str) -> dict[str, Any]:
    matches = [step for step in _steps(job) if command in step.get("run", "")]
    assert len(matches) == 1, command
    return matches[0]


def _step_using(job: dict[str, Any], action: str) -> list[dict[str, Any]]:
    return [step for step in _steps(job) if step.get("uses", "").startswith(action)]


def test_pull_request_runs_only_for_prompts_and_api(workflow: dict[str, Any]) -> None:
    triggers = _triggers(workflow)

    assert triggers["pull_request"]["paths"] == ["prompts/**", "api/**"]


def test_push_runs_on_the_default_branch_only(workflow: dict[str, Any]) -> None:
    push = _triggers(workflow)["push"]

    assert push["branches"] == [DEFAULT_BRANCH]


def test_permissions_are_minimal(workflow: dict[str, Any]) -> None:
    assert workflow["permissions"] == {
        "contents": "read",
        "pull-requests": "write",
        "actions": "read",
    }


def test_job_has_postgres_service_and_gh_token(job: dict[str, Any]) -> None:
    postgres = next(iter(job["services"].values()))

    assert postgres["image"] == "pgvector/pgvector:pg16"
    assert "5432:5432" in postgres["ports"]
    assert job["env"]["ATLAS_DATABASE_URL"] == (
        "postgresql+psycopg://atlas:atlas@localhost:5432/atlas"
    )
    assert job["env"]["GH_TOKEN"] == "${{ github.token }}"


def test_api_key_is_scoped_to_the_steps_that_need_it(job: dict[str, Any]) -> None:
    secret = "${{ secrets.ANTHROPIC_API_KEY }}"
    with_key = {
        step.get("id") or step.get("name")
        for step in _steps(job)
        if step.get("env", {}).get("ANTHROPIC_API_KEY") == secret
    }

    assert "ANTHROPIC_API_KEY" not in job["env"]
    assert with_key == {"key", "Run evals"}
    assert "ANTHROPIC_API_KEY" not in _step_running(job, "python -m evals gate").get("env", {})


def test_setup_installs_migrates_and_ingests_without_embeddings(job: dict[str, Any]) -> None:
    runs = [step.get("run", "") for step in _steps(job)]

    assert any(run.startswith("uv sync --frozen") for run in runs)
    assert any("alembic upgrade head" in run for run in runs)
    ingest = _step_running(job, "python -m ingest run")
    assert "--skip-embeddings" in ingest["run"]
    assert job["env"]["ATLAS_RETRIEVAL_MODE"] == "fulltext"


def test_auto_target_runs_every_dataset_except_smoke(job: dict[str, Any]) -> None:
    step = _step_running(job, "python -m evals run")
    args = shlex.split(step["run"])
    pattern = args[args.index("--dataset") + 1]
    matched = {Path(path).name for path in glob.glob(str(ROOT_DIR / pattern))}

    assert args[args.index("--target") + 1] == "auto"
    assert args[args.index("--out") + 1] == "reports/eval-report.json"
    assert "smoke.jsonl" not in matched
    assert {"core_en.jsonl", "core_pt.jsonl", "phase_01.jsonl", "redteam.jsonl"} <= matched


def test_gate_step_runs_without_continue_on_error(job: dict[str, Any]) -> None:
    step = _step_running(job, "python -m evals gate")
    args = shlex.split(step["run"])

    assert step.get("continue-on-error") is not True
    assert args[args.index("--current") + 1] == "reports/eval-report.json"
    assert "--base" in args
    assert args[args.index("--markdown") + 1] == "reports/eval-report.md"
    for name, job_step in [("job", job)] + [(s.get("name", ""), s) for s in _steps(job)]:
        assert job_step.get("continue-on-error") is not True, name


def test_pull_request_downloads_base_from_last_successful_default_run(
    job: dict[str, Any],
) -> None:
    step = _step_running(job, "gh run download")

    assert "github.event_name == 'pull_request'" in step["if"]
    assert re.search(r"gh run list --workflow evals\.yml --branch develop", step["run"])
    assert "--status success" in step["run"]
    assert "-n eval-report" in step["run"]


def test_pull_request_comments_the_report_even_when_the_gate_fails(job: dict[str, Any]) -> None:
    comments = [step for step in _steps(job) if "gh pr comment" in step.get("run", "")]
    report_comment = next(c for c in comments if "reports/eval-report.md" in c["run"])

    assert report_comment["if"].startswith("always()")
    assert "github.event_name == 'pull_request'" in report_comment["if"]
    assert "--edit-last" in report_comment["run"]


def test_push_uploads_the_report_as_eval_report_artifact(job: dict[str, Any]) -> None:
    uploads = [s for s in _step_using(job, "actions/upload-artifact") if s["with"]]
    base = next(s for s in uploads if s["with"]["name"] == "eval-report")

    assert "github.event_name == 'push'" in base["if"]
    assert "reports/eval-report.json" in base["with"]["path"]


def test_missing_api_key_skips_without_blocking(job: dict[str, Any]) -> None:
    check = next(s for s in _steps(job) if s.get("id") == "key")
    skipped = [s for s in _steps(job) if "skipped: no ANTHROPIC_API_KEY" in s.get("run", "")]

    assert "ANTHROPIC_API_KEY" in check["run"]
    assert "available=" in check["run"]
    assert skipped, "a step must report the skip"
    for step in skipped:
        assert "steps.key.outputs.available != 'true'" in step["if"]
        assert "exit 1" not in step["run"]
    pr_skip = [s for s in skipped if "gh pr comment" in s["run"]]
    assert pr_skip and "|| echo" in pr_skip[0]["run"], "commenting must not block the job"
    for command in ("python -m evals run", "python -m evals gate", "gh run download"):
        assert "steps.key.outputs.available == 'true'" in _step_running(job, command)["if"]
