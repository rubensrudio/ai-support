"""Eval scoring gate against the main-branch baseline (EVAL-05, EVAL-07, EVAL-90..92, TASK-127).

`python -m evals gate --current <report.json> [--base <report.json>] [--markdown <out.md>]`
- no base (missing or not given) -> exit 0, verdict `baseline_created` (EVAL-92);
- overall drop > 2.0 points -> exit 1, verdict `regression`; exactly 2.0 -> exit 0 (EVAL-91);
- category drops are reported only, never block;
- a report with `status='error'` or unreadable/invalid -> exit 2, verdict `error` (EVAL-90),
  distinct from a regression.
`evaluate(current, base) -> GateResult` holds the verdict, deltas and the cases that got worse;
`main(argv)` is the CLI entry (wired in `evals/__main__.py`).
"""

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from evals.runner import EvalReport

Verdict = Literal["pass", "regression", "error", "baseline_created"]
EXIT_PASS = 0
EXIT_REGRESSION = 1
EXIT_ERROR = 2
MAX_OVERALL_DROP = 2.0


@dataclass(frozen=True)
class CaseDelta:
    id: str
    category: str
    base: float
    current: float


@dataclass(frozen=True)
class GateResult:
    verdict: Verdict
    exit_code: int
    current_overall: float | None
    base_overall: float | None
    overall_delta: float | None
    category_deltas: dict[str, float] = field(default_factory=dict)
    worsened: list[CaseDelta] = field(default_factory=list)
    message: str = ""


class ReportLoadError(Exception):
    """The report file could not be read or is not a valid CT-10 report."""


def load_report(path: Path) -> EvalReport:
    """Read a CT-10 report JSON file; any read or validation failure raises `ReportLoadError`."""
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ReportLoadError(f"cannot read report {path}: {error}") from error
    try:
        return EvalReport.model_validate_json(raw)
    except ValidationError as error:
        count = error.error_count()
        raise ReportLoadError(f"invalid report {path}: {count} validation error(s)") from error


def _delta(current: float, base: float) -> float:
    """Difference rounded to 6 places so binary float noise never crosses the threshold."""
    return round(current - base, 6)


def _error_result(message: str, current: EvalReport | None) -> GateResult:
    return GateResult(
        verdict="error",
        exit_code=EXIT_ERROR,
        current_overall=current.scores.overall if current is not None else None,
        base_overall=None,
        overall_delta=None,
        message=message,
    )


def _worsened(current: EvalReport, base: EvalReport) -> list[CaseDelta]:
    """Cases present in both runs whose score went down, biggest drop first, then by id."""
    base_scores = {case.id: case.score for case in base.cases}
    worse = [
        CaseDelta(id=case.id, category=case.category, base=base_scores[case.id], current=case.score)
        for case in current.cases
        if case.id in base_scores and _delta(case.score, base_scores[case.id]) < 0
    ]
    return sorted(worse, key=lambda case: (case.current - case.base, case.id))


def evaluate(current: EvalReport, base: EvalReport | None) -> GateResult:
    """Apply the gate rules (EVAL-07, EVAL-90..92) to the current report and the baseline."""
    if current.status == "error":
        return _error_result("the current eval run had execution errors (status='error')", current)
    if base is None:
        return GateResult(
            verdict="baseline_created",
            exit_code=EXIT_PASS,
            current_overall=current.scores.overall,
            base_overall=None,
            overall_delta=None,
            message="no baseline found; this run becomes the baseline",
        )
    if base.status == "error":
        return _error_result("the baseline eval run had execution errors (status='error')", current)

    overall_delta = _delta(current.scores.overall, base.scores.overall)
    category_deltas = {
        category: _delta(score, base.scores.by_category[category])
        for category, score in sorted(current.scores.by_category.items())
        if category in base.scores.by_category
    }
    regressed = -overall_delta > MAX_OVERALL_DROP
    if regressed:
        message = (
            f"overall score dropped {-overall_delta:.2f} points (limit {MAX_OVERALL_DROP:.2f})"
        )
    else:
        message = f"overall score change {overall_delta:+.2f} is within the limit"
    return GateResult(
        verdict="regression" if regressed else "pass",
        exit_code=EXIT_REGRESSION if regressed else EXIT_PASS,
        current_overall=current.scores.overall,
        base_overall=base.scores.overall,
        overall_delta=overall_delta,
        category_deltas=category_deltas,
        worsened=_worsened(current, base),
        message=message,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evals gate", description="Compare an eval report with the baseline"
    )
    parser.add_argument("--current", required=True, type=Path, help="current report JSON")
    parser.add_argument(
        "--base", type=Path, default=None, help="baseline report JSON (missing file = no base)"
    )
    parser.add_argument("--markdown", type=Path, default=None, help="markdown file to write")
    return parser


def _run_gate(current_path: Path, base_path: Path | None) -> tuple[GateResult, EvalReport | None]:
    try:
        current = load_report(current_path)
    except ReportLoadError as error:
        return _error_result(str(error), None), None
    base: EvalReport | None = None
    if base_path is not None and base_path.exists():
        try:
            base = load_report(base_path)
        except ReportLoadError as error:
            return _error_result(str(error), current), current
    return evaluate(current, base), current


def _summary(result: GateResult) -> str:
    def fmt(value: float | None, signed: bool = False) -> str:
        if value is None:
            return "n/a"
        return f"{value:+.2f}" if signed else f"{value:.2f}"

    return (
        f"eval gate: {result.verdict} (exit {result.exit_code}) "
        f"overall={fmt(result.current_overall)} base={fmt(result.base_overall)} "
        f"delta={fmt(result.overall_delta, signed=True)} worsened={len(result.worsened)}"
        + (f" - {result.message}" if result.message else "")
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry: print a one-line summary, optionally write the markdown, return the exit code."""
    args = _build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    result, current = _run_gate(args.current, args.base)
    if args.markdown is not None:
        from evals.report import render_markdown

        try:
            args.markdown.parent.mkdir(parents=True, exist_ok=True)
            args.markdown.write_text(render_markdown(result, current), encoding="utf-8")
        except OSError as error:
            print(f"error: cannot write markdown {args.markdown}: {error}", file=sys.stderr)
            return EXIT_ERROR
    print(_summary(result))
    if result.verdict == "error":
        print(f"error: {result.message}", file=sys.stderr)
    return result.exit_code
