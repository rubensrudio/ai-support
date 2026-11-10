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

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

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


def load_report(path: Path) -> EvalReport:
    raise NotImplementedError("TASK-127")


def evaluate(current: EvalReport, base: EvalReport | None) -> GateResult:
    raise NotImplementedError("TASK-127")


def main(argv: Sequence[str] | None = None) -> int:
    raise NotImplementedError("TASK-127")
