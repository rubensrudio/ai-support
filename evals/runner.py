"""Run eval cases against a target and build the CT-10 report (FND-05, DA-17).

A target produces an output dict for a case; a grader, picked by the case's `grader` name,
turns that output into a score from 0 to 100. A failing target or grader never aborts the
run: the case is recorded with `passed=False`, `score=0` and `details.error`, and the report
status becomes `error`.
"""

import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from evals.dataset import Category, EvalCase, Language

ReportStatus = Literal["ok", "error"]


class EvalTarget(Protocol):
    """System under evaluation: turns a case into an output dict for the grader."""

    async def run(self, case: EvalCase) -> dict[str, Any]: ...


@dataclass(frozen=True)
class GradeResult:
    score: float
    passed: bool
    details: dict[str, Any] = field(default_factory=dict)


Grader = Callable[[EvalCase, dict[str, Any]], Awaitable[GradeResult]]


class CaseResult(BaseModel):
    id: str
    category: Category
    language: Language
    score: float
    passed: bool
    details: dict[str, Any]


class EvalScores(BaseModel):
    overall: float
    by_category: dict[str, float]


class EvalReport(BaseModel):
    run_id: str
    status: ReportStatus
    cases: list[CaseResult]
    scores: EvalScores


class EchoTarget:
    """Built-in target for the smoke dataset: returns the case input unchanged."""

    async def run(self, case: EvalCase) -> dict[str, Any]:
        return dict(case.input)


async def exact_match(case: EvalCase, output: dict[str, Any]) -> GradeResult:
    """100 when every expected field equals the output field exactly, otherwise 0."""
    mismatches = {
        key: {"expected": expected, "actual": output.get(key)}
        for key, expected in case.expected.items()
        if key not in output or output[key] != expected
    }
    if mismatches:
        return GradeResult(score=0.0, passed=False, details={"mismatches": mismatches})
    return GradeResult(score=100.0, passed=True, details={})


DEFAULT_GRADERS: Mapping[str, Grader] = {"exact_match": exact_match}


def _mean(values: Sequence[float]) -> float:
    return round(sum(values) / len(values), 2) if values else 0.0


async def _run_case(
    case: EvalCase, target: EvalTarget, graders: Mapping[str, Grader]
) -> tuple[CaseResult, bool]:
    """Return the case result and whether an execution error happened."""
    grader = graders.get(case.grader)
    try:
        if grader is None:
            raise LookupError(f"unknown grader {case.grader!r}")
        output = await target.run(case)
        grade = await grader(case, output)
    except Exception as error:  # one broken case must not abort the run
        result = CaseResult(
            id=case.id,
            category=case.category,
            language=case.language,
            score=0.0,
            passed=False,
            details={"error": f"{type(error).__name__}: {error}"},
        )
        return result, True
    score = min(max(float(grade.score), 0.0), 100.0)
    result = CaseResult(
        id=case.id,
        category=case.category,
        language=case.language,
        score=score,
        passed=grade.passed,
        details=dict(grade.details),
    )
    return result, False


async def run_cases(
    cases: Sequence[EvalCase], target: EvalTarget, graders: Mapping[str, Grader]
) -> EvalReport:
    """Run every case sequentially and aggregate scores per category and overall."""
    results: list[CaseResult] = []
    had_error = False
    for case in cases:
        result, errored = await _run_case(case, target, graders)
        results.append(result)
        had_error = had_error or errored

    by_category: dict[str, list[float]] = defaultdict(list)
    for result in results:
        by_category[result.category].append(result.score)

    return EvalReport(
        run_id=uuid.uuid4().hex,
        status="error" if had_error else "ok",
        cases=results,
        scores=EvalScores(
            overall=_mean([result.score for result in results]),
            by_category={category: _mean(scores) for category, scores in by_category.items()},
        ),
    )
