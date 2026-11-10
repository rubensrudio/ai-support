"""Eval scoring gate against the main-branch baseline (TASK-127, EVAL-05/07/90/91/92)."""

import subprocess
import sys
from pathlib import Path

import pytest

from evals.__main__ import main as evals_main
from evals.gate import (
    EXIT_ERROR,
    EXIT_PASS,
    EXIT_REGRESSION,
    MAX_OVERALL_DROP,
    ReportLoadError,
    evaluate,
    load_report,
    main,
)
from evals.runner import CaseResult, EvalReport, EvalScores, ReportStatus

ROOT_DIR = Path(__file__).resolve().parents[3]


def _report(
    overall: float,
    by_category: dict[str, float] | None = None,
    cases: list[tuple[str, str, float]] | None = None,
    status: ReportStatus = "ok",
) -> EvalReport:
    rows = cases if cases is not None else [("case-001", "rag", overall)]
    return EvalReport(
        run_id="run",
        status=status,
        cases=[
            CaseResult(
                id=case_id,
                category=category,  # type: ignore[arg-type]
                language="en",
                score=score,
                passed=score == 100.0,
                details={},
            )
            for case_id, category, score in rows
        ],
        scores=EvalScores(
            overall=overall,
            by_category=by_category if by_category is not None else {"rag": overall},
        ),
    )


def _write(path: Path, report: EvalReport) -> Path:
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return path


def test_drop_above_limit_is_a_regression() -> None:
    result = evaluate(_report(87.99), _report(90.0))

    assert result.verdict == "regression"
    assert result.exit_code == EXIT_REGRESSION == 1
    assert result.overall_delta == pytest.approx(-2.01)


def test_drop_of_exactly_the_limit_passes() -> None:
    assert MAX_OVERALL_DROP == 2.0
    result = evaluate(_report(88.0), _report(90.0))

    assert result.verdict == "pass"
    assert result.exit_code == EXIT_PASS == 0
    assert result.overall_delta == pytest.approx(-2.0)


def test_float_noise_at_the_limit_does_not_block() -> None:
    # 72.3 - 70.3 is 2.0000000000000004 in binary floating point.
    result = evaluate(_report(70.3), _report(72.3))

    assert result.verdict == "pass"
    assert result.exit_code == EXIT_PASS


def test_improvement_passes() -> None:
    result = evaluate(_report(95.0), _report(90.0))

    assert result.verdict == "pass"
    assert result.overall_delta == pytest.approx(5.0)
    assert result.current_overall == 95.0
    assert result.base_overall == 90.0


def test_no_base_creates_the_baseline() -> None:
    result = evaluate(_report(50.0), None)

    assert result.verdict == "baseline_created"
    assert result.exit_code == EXIT_PASS
    assert result.base_overall is None
    assert result.overall_delta is None
    assert result.current_overall == 50.0


def test_current_report_with_error_status_is_an_execution_error() -> None:
    result = evaluate(_report(100.0, status="error"), _report(90.0))

    assert result.verdict == "error"
    assert result.exit_code == EXIT_ERROR == 2


def test_error_status_without_base_is_still_an_error() -> None:
    result = evaluate(_report(100.0, status="error"), None)

    assert result.verdict == "error"
    assert result.exit_code == EXIT_ERROR


def test_base_report_with_error_status_is_an_execution_error() -> None:
    result = evaluate(_report(90.0), _report(90.0, status="error"))

    assert result.verdict == "error"
    assert result.exit_code == EXIT_ERROR


def test_category_only_drop_is_reported_without_blocking() -> None:
    base = _report(
        80.0,
        {"rag": 100.0, "tone": 60.0},
        [("rag-1", "rag", 100.0), ("tone-1", "tone", 60.0)],
    )
    current = _report(
        80.0,
        {"rag": 90.0, "tone": 70.0},
        [("rag-1", "rag", 90.0), ("tone-1", "tone", 70.0)],
    )

    result = evaluate(current, base)

    assert result.verdict == "pass"
    assert result.exit_code == EXIT_PASS
    assert result.category_deltas == {"rag": pytest.approx(-10.0), "tone": pytest.approx(10.0)}


def test_worsened_lists_cases_with_a_lower_score_by_id() -> None:
    base = _report(
        50.0,
        cases=[("a", "rag", 100.0), ("b", "tone", 50.0), ("c", "rag", 0.0), ("gone", "rag", 0.0)],
    )
    current = _report(
        25.0,
        cases=[("c", "rag", 0.0), ("b", "tone", 0.0), ("a", "rag", 0.0), ("new", "rag", 0.0)],
    )

    result = evaluate(current, base)

    assert [(case.id, case.category, case.base, case.current) for case in result.worsened] == [
        ("a", "rag", 100.0, 0.0),
        ("b", "tone", 50.0, 0.0),
    ]


def test_load_report_round_trips_a_written_report(tmp_path: Path) -> None:
    report = _report(91.5)

    assert load_report(_write(tmp_path / "r.json", report)) == report


@pytest.mark.parametrize(
    "content",
    ["{not json", '{"run_id": "x"}', "[]", ""],
    ids=["broken-json", "missing-fields", "wrong-shape", "empty"],
)
def test_load_report_rejects_invalid_content(tmp_path: Path, content: str) -> None:
    path = tmp_path / "r.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ReportLoadError):
        load_report(path)


def test_load_report_rejects_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ReportLoadError):
        load_report(tmp_path / "missing.json")


def test_main_exits_1_on_regression(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    current = _write(tmp_path / "current.json", _report(87.99))
    base = _write(tmp_path / "base.json", _report(90.0))

    assert main(["--current", str(current), "--base", str(base)]) == EXIT_REGRESSION
    assert "regression" in capsys.readouterr().out


def test_main_exits_0_at_exactly_the_limit(tmp_path: Path) -> None:
    current = _write(tmp_path / "current.json", _report(88.0))
    base = _write(tmp_path / "base.json", _report(90.0))

    assert main(["--current", str(current), "--base", str(base)]) == EXIT_PASS


def test_main_without_base_creates_the_baseline(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    current = _write(tmp_path / "current.json", _report(70.0))

    assert main(["--current", str(current)]) == EXIT_PASS
    assert "baseline_created" in capsys.readouterr().out


def test_main_treats_a_missing_base_file_as_no_base(tmp_path: Path) -> None:
    current = _write(tmp_path / "current.json", _report(70.0))

    assert main(["--current", str(current), "--base", str(tmp_path / "nope.json")]) == EXIT_PASS


def test_main_exits_2_for_invalid_current_json(tmp_path: Path) -> None:
    current = tmp_path / "current.json"
    current.write_text("{oops", encoding="utf-8")
    base = _write(tmp_path / "base.json", _report(90.0))

    assert main(["--current", str(current), "--base", str(base)]) == EXIT_ERROR


def test_main_exits_2_for_missing_current_file(tmp_path: Path) -> None:
    assert main(["--current", str(tmp_path / "missing.json")]) == EXIT_ERROR


def test_main_exits_2_for_invalid_base_json(tmp_path: Path) -> None:
    current = _write(tmp_path / "current.json", _report(90.0))
    base = tmp_path / "base.json"
    base.write_text("[]", encoding="utf-8")

    assert main(["--current", str(current), "--base", str(base)]) == EXIT_ERROR


def test_main_exits_2_for_error_status(tmp_path: Path) -> None:
    current = _write(tmp_path / "current.json", _report(100.0, status="error"))
    base = _write(tmp_path / "base.json", _report(90.0))

    assert main(["--current", str(current), "--base", str(base)]) == EXIT_ERROR


def test_main_writes_the_markdown_report(tmp_path: Path) -> None:
    current = _write(tmp_path / "current.json", _report(87.0))
    base = _write(tmp_path / "base.json", _report(90.0))
    out = tmp_path / "nested" / "report.md"

    code = main(["--current", str(current), "--base", str(base), "--markdown", str(out)])

    assert code == EXIT_REGRESSION
    text = out.read_text(encoding="utf-8")
    assert "Overall" in text
    assert "regression" in text.lower()


def test_main_writes_markdown_even_on_error(tmp_path: Path) -> None:
    current = tmp_path / "current.json"
    current.write_text("{oops", encoding="utf-8")
    out = tmp_path / "report.md"

    assert main(["--current", str(current), "--markdown", str(out)]) == EXIT_ERROR
    assert "error" in out.read_text(encoding="utf-8").lower()


def test_evals_cli_delegates_the_gate_subcommand(tmp_path: Path) -> None:
    current = _write(tmp_path / "current.json", _report(87.99))
    base = _write(tmp_path / "base.json", _report(90.0))

    code = evals_main(["gate", "--current", str(current), "--base", str(base)])

    assert code == EXIT_REGRESSION


@pytest.mark.parametrize(
    ("current_overall", "base_overall", "expected"),
    [(87.99, 90.0, EXIT_REGRESSION), (88.0, 90.0, EXIT_PASS), (None, 90.0, EXIT_PASS)],
    ids=["drop-2.01", "drop-2.00", "no-base"],
)
def test_python_m_evals_gate_exit_codes(
    tmp_path: Path, current_overall: float | None, base_overall: float, expected: int
) -> None:
    base = _write(tmp_path / "base.json", _report(base_overall))
    if current_overall is None:
        current = _write(tmp_path / "current.json", _report(10.0))
        args = ["--current", str(current), "--base", str(tmp_path / "absent.json")]
    else:
        current = _write(tmp_path / "current.json", _report(current_overall))
        args = ["--current", str(current), "--base", str(base)]

    completed = subprocess.run(  # noqa: S603 - fixed interpreter and arguments
        [sys.executable, "-m", "evals", "gate", *args],
        cwd=ROOT_DIR,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert completed.returncode == expected, completed.stderr


def test_python_m_evals_gate_exits_2_for_invalid_json(tmp_path: Path) -> None:
    current = tmp_path / "current.json"
    current.write_text("not json", encoding="utf-8")

    completed = subprocess.run(  # noqa: S603 - fixed interpreter and arguments
        [sys.executable, "-m", "evals", "gate", "--current", str(current)],
        cwd=ROOT_DIR,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert completed.returncode == EXIT_ERROR
