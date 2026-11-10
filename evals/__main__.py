"""Eval CLI (CT-10): `python -m evals run --dataset <glob> --target <name> --out <file>` and
`python -m evals gate --current <report> [--base <report>] [--markdown <out>]` (see
`evals.gate`, exit 0 pass/baseline, 1 regression, 2 error).

Exit codes: 0 when the run completed, 2 on an execution error (unreadable or invalid dataset,
no file matched, unknown target, or a case with an execution error). Scores below 100 are not
an error here; regression gating is a separate command.

Execution errors (EVAL-90): a case has one when its `CaseResult.details.error` is set. That key
comes from the runner (the target or grader raised), from the graders (the target's
`output.error`, e.g. `AtlasError: LLM_UNAVAILABLE (502)`, `TRIAGE_UNAVAILABLE`, a `degraded`
answer, or a judge failure) and from `with_error`, which adds the target's error to graders that
do not report it (`exact_match`) and lifts a part's error of a composite grade (`rag`:
`details.faithfulness.error`) to the top. The targets never raise, so after the run the CLI marks
the report `status="error"` when any case has `details.error`, keeps every case score, writes it
and exits 2: a provider outage in CI is an error for `evals.gate` (exit 2), never a regression
(exit 1).

Targets: `echo` (returns the input, for `smoke.jsonl` only), `chat` (`evals.targets.ChatTarget`:
the chat service in process, against `ATLAS_DATABASE_URL`, which it seeds) and `triage`
(`evals.targets.TriageTarget`). Run the chat and triage datasets with
`--dataset 'evals/datasets/{core_*,phase_*,redteam}.jsonl'` (quoted: the CLI expands the
`{a,b}` alternatives itself), which leaves `smoke.jsonl` to the echo target. The `json_valid`
cases need `--target triage`; on the chat target they score 0.

`--target auto` (`evals.targets.AutoTarget`) runs the same glob in one report: each case goes
to the triage target when it is a triage case (category `json_valid`, or an input with `text`
and no `message`) and to the chat target otherwise. Only with `auto`, the `category: "smoke"`
cases that are not red-team controls (`input.set != "control"`, e.g. the stream-shape cases
of phase 09 and the provider-failure cases of phase 12) are excluded before the run: they
check what the in-process chat output cannot carry (stream events, injected provider
failures), so under `exact_match` they would always score 0 and only dilute the overall
score. Also excluded with `auto`: the cases whose input carries a scripted model reply
(`model_answer`, `fake_reply`, e.g. the output-stage cases of phase 10 and their red-team
controls), since a real model will not give that reply. Each reason's count is printed and
logged (`eval_cases_excluded`); the other controls stay and are graded by `red_team_blocked`.

Grader per case (`grader_name`, contract of `evals.graders`):
1. `input.set == "control"` (red-team controls, including the `category: "smoke"` controls of
   `redteam.jsonl`) -> `red_team_blocked`;
2. `grader == "exact_match"` on a non-smoke case -> the category default of
   `default_graders()`: the phase 1-12 datasets were written with `exact_match` for their
   offline tests, and what they measure in a run is the category score;
3. a category name (the core datasets use `grader == category`) -> that category default
   (`smoke` -> `exact_match`);
4. `exact_match` on a smoke case, `citation`, `faithfulness`, `red_team_blocked` -> that
   grader;
anything else stays unknown and the runner records a case error.
The LLM-as-judge graders use `build_llm_client(settings)`, with `ATLAS_JUDGE_MODEL` as the
primary model when set, built on the first judged case (an echo or triage run needs no judge).
"""

import argparse
import asyncio
import re
import sys
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, get_args

from api.config import get_settings
from api.llm.client import LLMClient, LLMRequest, LLMResponse, LLMStreamEvent
from api.llm.factory import build_llm_client
from api.observability.logging import configure_logging, log_event
from evals.dataset import (
    Category,
    DatasetError,
    EvalCase,
    load_cases,
    resolve_dataset_paths,
)
from evals.graders import citation, default_graders, faithfulness_grader, red_team_blocked
from evals.judge import GRADER_NAME as FAITHFULNESS
from evals.runner import (
    EchoTarget,
    EvalReport,
    EvalTarget,
    Grader,
    GradeResult,
    exact_match,
    run_cases,
)
from evals.targets import AutoTarget, ChatTarget, TriageTarget

EXIT_OK = 0
EXIT_EXECUTION_ERROR = 2

CONTROL_SET = "control"
EXACT_MATCH = "exact_match"
RED_TEAM_BLOCKED = "red_team_blocked"
SMOKE = "smoke"

TARGETS: dict[str, Callable[[], EvalTarget]] = {
    "echo": EchoTarget,
    "chat": lambda: ChatTarget(get_settings()),
    "triage": lambda: TriageTarget(get_settings()),
    "auto": lambda: AutoTarget(get_settings()),
}
AUTO = "auto"
ERROR_KEY = "error"
MAX_LISTED_IDS = 10
SCRIPTED_INPUT_KEYS: tuple[str, ...] = ("model_answer", "fake_reply")
REASON_SMOKE = "smoke cases that are not red-team controls (not measurable in process)"
REASON_SCRIPTED = (
    "cases that depend on a scripted model reply (input.model_answer / input.fake_reply)"
)

CATEGORIES: tuple[str, ...] = get_args(Category)
GRADER_NAMES: frozenset[str] = frozenset(
    {EXACT_MATCH, "citation", FAITHFULNESS, RED_TEAM_BLOCKED, *CATEGORIES}
)

_BRACES = re.compile(r"\{([^{}]*)\}")


class _LazyJudge:
    """The judge client, built on its first call (`ATLAS_JUDGE_MODEL` when set)."""

    def __init__(self) -> None:
        self._client: LLMClient | None = None

    def _get(self) -> LLMClient:
        if self._client is None:
            settings = get_settings()
            if settings.judge_model:
                settings = settings.model_copy(update={"primary_model": settings.judge_model})
            self._client = build_llm_client(settings)
        return self._client

    async def complete(self, request: LLMRequest) -> LLMResponse:
        return await self._get().complete(request)

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        return self._get().stream(request)


def grader_name(case: EvalCase) -> str | None:
    """Name in `GRADER_NAMES` of the grader for `case` (rules in the module docstring), or None
    when the case names an unknown grader."""
    if case.input.get("set") == CONTROL_SET:
        return RED_TEAM_BLOCKED
    if case.grader == EXACT_MATCH and case.category != SMOKE:
        return case.category
    return case.grader if case.grader in GRADER_NAMES else None


def build_graders(judge_llm: LLMClient) -> Mapping[str, Grader]:
    """Runner registry: every name of `GRADER_NAMES` maps to one dispatcher that grades the
    case with `grader_name(case)`; unknown names are left out, so the runner reports them."""
    named: dict[str, Grader] = {
        EXACT_MATCH: exact_match,
        "citation": citation,
        FAITHFULNESS: faithfulness_grader(judge_llm),
        RED_TEAM_BLOCKED: red_team_blocked,
        **default_graders(judge_llm),  # one per category; `smoke` is exact_match
    }

    async def dispatch(case: EvalCase, output: dict[str, Any]) -> GradeResult:
        name = grader_name(case)
        if name is None:  # pragma: no cover - the runner never dispatches an unknown name
            raise LookupError(f"unknown grader {case.grader!r}")
        return with_error(await named[name](case, output), output)

    return {name: dispatch for name in GRADER_NAMES}


def with_error(grade: GradeResult, output: Mapping[str, Any]) -> GradeResult:
    """`grade` with `details.error` set when the case had an execution error: the target's
    `output.error` (graders that do not report it, like `exact_match`, get it here) or a part's
    error inside a composite grade (`rag`: `details.faithfulness.error` from the judge)."""
    if grade.details.get(ERROR_KEY):
        return grade
    error = output.get(ERROR_KEY) or next(
        (
            part[ERROR_KEY]
            for part in grade.details.values()
            if isinstance(part, Mapping) and part.get(ERROR_KEY)
        ),
        None,
    )
    if not error:
        return grade
    return GradeResult(grade.score, grade.passed, {**grade.details, ERROR_KEY: str(error)})


def execution_errors(report: EvalReport) -> list[str]:
    """Ids of the cases with `details.error` (target, judge or runner error)."""
    return [case.id for case in report.cases if case.details.get(ERROR_KEY)]


def dataset_paths(pattern: str) -> list[Path]:
    """`resolve_dataset_paths` with shell-style `{a,b}` alternatives expanded (Python's glob
    has none), sorted and without duplicates."""
    patterns = [pattern]
    while any(_BRACES.search(item) for item in patterns):
        expanded: list[str] = []
        for item in patterns:
            match = _BRACES.search(item)
            if match is None:
                expanded.append(item)
                continue
            head, tail = item[: match.start()], item[match.end() :]
            expanded.extend(f"{head}{option}{tail}" for option in match.group(1).split(","))
        patterns = expanded
    return sorted({path for item in patterns for path in resolve_dataset_paths(item)})


def exclusion_reason(case: EvalCase) -> str | None:
    """Why `--target auto` drops `case` (module docstring), or None when it runs."""
    if any(key in case.input for key in SCRIPTED_INPUT_KEYS):
        return REASON_SCRIPTED
    if case.category == SMOKE and case.input.get("set") != CONTROL_SET:
        return REASON_SMOKE
    return None


def excluded_for_auto(case: EvalCase) -> bool:
    return exclusion_reason(case) is not None


def _exclude_for_auto(cases: list[EvalCase], target_name: str) -> list[EvalCase]:
    """The cases `--target auto` runs; prints and logs the count of each exclusion reason."""
    by_reason: dict[str, list[str]] = {}
    kept: list[EvalCase] = []
    for case in cases:
        reason = exclusion_reason(case)
        if reason is None:
            kept.append(case)
        else:
            by_reason.setdefault(reason, []).append(case.id)
    for reason, ids in by_reason.items():
        print(f"excluded {len(ids)} case(s): {reason}")
        log_event(
            "eval_cases_excluded",
            target=target_name,
            count=len(ids),
            reason=reason,
            case_ids=ids,
        )
    return kept


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m evals", description="Atlas eval runner")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run a dataset against a target")
    run.add_argument("--dataset", required=True, help="JSONL file or glob pattern ({a,b} ok)")
    run.add_argument("--target", required=True, help=f"target name ({', '.join(TARGETS)})")
    run.add_argument("--out", required=True, type=Path, help="report JSON file to write")
    gate = commands.add_parser("gate", help="compare a report with the baseline")
    gate.add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    return parser


def _error(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return EXIT_EXECUTION_ERROR


async def _execute(cases: list[EvalCase], target: EvalTarget) -> EvalReport:
    """Run every case, then let the target release what it holds in this event loop."""
    try:
        return await run_cases(cases, target, build_graders(_LazyJudge()))
    finally:
        aclose = getattr(target, "aclose", None)
        if aclose is not None:
            await aclose()


def _run(dataset: str, target_name: str, out: Path) -> int:
    target_factory = TARGETS.get(target_name)
    if target_factory is None:
        return _error(f"unknown target {target_name!r} (available: {', '.join(TARGETS)})")
    paths = dataset_paths(dataset)
    if not paths:
        return _error(f"no dataset file matches {dataset!r}")
    try:
        cases = load_cases(paths)
    except (DatasetError, OSError) as error:
        return _error(str(error))
    if target_name == AUTO:
        cases = _exclude_for_auto(cases, target_name)

    report = asyncio.run(_execute(cases, target_factory()))
    failed = execution_errors(report)
    if failed:
        # The runner only flags exceptions; a target or judge error reported in the output
        # is an execution error too (EVAL-90): the gate must exit 2, not 1.
        report = report.model_copy(update={"status": "error"})

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    log_event(
        "eval_run_finished",
        run_id=report.run_id,
        status=report.status,
        target=target_name,
        cases=len(report.cases),
        overall=report.scores.overall,
        out=str(out),
    )
    if report.status == "error":
        listed = ", ".join(failed[:MAX_LISTED_IDS]) + (
            ", ..." if len(failed) > MAX_LISTED_IDS else ""
        )
        return _error(
            f"{len(failed)} case(s) had execution errors (target, provider or judge failure; "
            f"see details.error): {listed}; report marked status=error in {out}"
        )
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw[:1] == ["gate"]:
        from evals.gate import main as gate_main

        return gate_main(raw[1:])
    args = _build_parser().parse_args(raw)
    configure_logging(get_settings())
    if args.command == "run":
        return _run(args.dataset, args.target, args.out)
    return _error(f"unknown command {args.command!r}")  # pragma: no cover - argparse guards it


if __name__ == "__main__":
    sys.exit(main())
