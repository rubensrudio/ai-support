"""Markdown eval report for PR comments (TASK-128, EVAL-03, DOD-04)."""

from evals.gate import CaseDelta, evaluate
from evals.report import render_markdown
from evals.runner import CaseResult, EvalReport, EvalScores, ReportStatus


def _report(
    overall: float,
    by_category: dict[str, float],
    cases: list[tuple[str, str, float]],
    status: ReportStatus = "ok",
) -> EvalReport:
    return EvalReport(
        run_id="run-123",
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
            for case_id, category, score in cases
        ],
        scores=EvalScores(overall=overall, by_category=by_category),
    )


BASE = _report(
    90.0,
    {"rag": 100.0, "tone": 80.0},
    [("rag-001", "rag", 100.0), ("tone-001", "tone", 80.0)],
)
CURRENT = _report(
    85.0,
    {"rag": 90.0, "tone": 80.0, "red_team": 85.0},
    [("rag-001", "rag", 90.0), ("tone-001", "tone", 80.0), ("rt-001", "red_team", 85.0)],
)


def test_markdown_has_overall_every_category_and_the_verdict() -> None:
    result = evaluate(CURRENT, BASE)
    text = render_markdown(result, CURRENT)

    assert "Overall" in text
    for category in ("rag", "tone", "red_team"):
        assert category in text
    assert "Regression" in text
    assert "85.00" in text
    assert "90.00" in text
    assert "-5.00" in text


def test_markdown_lists_the_worsened_cases() -> None:
    result = evaluate(CURRENT, BASE)
    text = render_markdown(result, CURRENT)

    assert "rag-001" in text
    assert "tone-001" not in text.split("worse", 1)[-1]


def test_pass_verdict() -> None:
    current = _report(89.0, {"rag": 100.0, "tone": 78.0}, [("rag-001", "rag", 100.0)])
    text = render_markdown(evaluate(current, BASE), current)

    assert "Pass" in text
    assert "Overall" in text
    assert "rag" in text and "tone" in text


def test_baseline_created_verdict() -> None:
    text = render_markdown(evaluate(CURRENT, None), CURRENT)

    assert "Baseline created" in text
    assert "Overall" in text
    for category in ("rag", "tone", "red_team"):
        assert category in text


def test_error_verdict_without_reports() -> None:
    result = evaluate(_report(0.0, {}, [], status="error"), None)
    text = render_markdown(result)

    assert "Error" in text
    assert "Overall" in text


def test_worsened_list_is_capped() -> None:
    base = _report(100.0, {"rag": 100.0}, [(f"c-{i:03d}", "rag", 100.0) for i in range(40)])
    current = _report(0.0, {"rag": 0.0}, [(f"c-{i:03d}", "rag", 0.0) for i in range(40)])
    text = render_markdown(evaluate(current, base), current)

    assert "c-000" in text
    assert "c-039" not in text
    assert "more" in text


def test_case_delta_is_rendered_with_scores() -> None:
    result = evaluate(CURRENT, BASE)
    assert result.worsened == [CaseDelta("rag-001", "rag", 100.0, 90.0)]
    text = render_markdown(result, CURRENT)

    assert "| rag-001 | rag | 100.00 | 90.00 | -10.00 |" in text
