"""Eval CLI: `python -m evals run --dataset <glob> --target <name> --out <file>` (CT-10).

Exit codes: 0 when the run completed, 2 on an execution error (unreadable or invalid dataset,
no file matched, unknown target, or a case whose target or grader raised). Scores below
100 are not an error here; regression gating is a separate command.
"""

import argparse
import asyncio
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from api.config import get_settings
from api.observability.logging import configure_logging, log_event
from evals.dataset import DatasetError, load_cases, resolve_dataset_paths
from evals.runner import DEFAULT_GRADERS, EchoTarget, EvalTarget, Grader, run_cases

EXIT_OK = 0
EXIT_EXECUTION_ERROR = 2

TARGETS: Mapping[str, Callable[[], EvalTarget]] = {"echo": EchoTarget}
GRADERS: Mapping[str, Grader] = DEFAULT_GRADERS


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m evals", description="Atlas eval runner")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run a dataset against a target")
    run.add_argument("--dataset", required=True, help="JSONL file or glob pattern")
    run.add_argument("--target", required=True, help=f"target name ({', '.join(TARGETS)})")
    run.add_argument("--out", required=True, type=Path, help="report JSON file to write")
    return parser


def _error(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return EXIT_EXECUTION_ERROR


def _run(dataset: str, target_name: str, out: Path) -> int:
    target_factory = TARGETS.get(target_name)
    if target_factory is None:
        return _error(f"unknown target {target_name!r} (available: {', '.join(TARGETS)})")
    paths = resolve_dataset_paths(dataset)
    if not paths:
        return _error(f"no dataset file matches {dataset!r}")
    try:
        cases = load_cases(paths)
    except (DatasetError, OSError) as error:
        return _error(str(error))

    report = asyncio.run(run_cases(cases, target_factory(), GRADERS))

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
        return _error(f"one or more cases failed to execute; see {out}")
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    configure_logging(get_settings())
    if args.command == "run":
        return _run(args.dataset, args.target, args.out)
    return _error(f"unknown command {args.command!r}")  # pragma: no cover - argparse guards it


if __name__ == "__main__":
    sys.exit(main())
