"""Phase definition-of-done checker (CT-11, DOD-01, DOD-04).

Usage: `python scripts/check_phase.py <NN> [--require-tag] [--root <path>]`

A phase NN is ready when:
- `evals/datasets/*.jsonl` has at least one case with `phase == NN`;
- `demos/demo_NN_*.py` exists;
- the `| NN |` row of `docs/results.md` has Before and After filled in;
- `docs/adr/00NN-*.md` exists, has Context, Decision and Alternatives sections and is written
  in English (detected by `langdetect`);
- with `--require-tag`, the git tag `phase-NN` exists.

Exit 0 when ready; exit 1 printing one line per missing artifact.
"""

import argparse
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from langdetect import DetectorFactory, detect
from langdetect.lang_detect_exception import LangDetectException

from evals.dataset import DatasetError, load_cases

DetectorFactory.seed = 0  # deterministic language detection

DEFAULT_ROOT = Path(__file__).resolve().parents[1]
FIRST_PHASE = 1
LAST_PHASE = 14
EXIT_READY = 0
EXIT_NOT_READY = 1
ADR_SECTIONS = ("Context", "Decision", "Alternatives")
RESULTS_COLUMNS = ("phase", "metric", "before", "after", "notes")
_CELL_SEPARATOR = re.compile(r"(?<!\\)\|")


def _check_eval_cases(phase: int, root: Path) -> str | None:
    paths = sorted((root / "evals" / "datasets").glob("*.jsonl"))
    try:
        cases = load_cases(paths)
    except (DatasetError, OSError) as error:
        return f"eval cases: invalid dataset ({error})"
    if not any(case.phase == phase for case in cases):
        return f"eval cases: no case with phase == {phase} in evals/datasets/*.jsonl"
    return None


def _check_demo(nn: str, root: Path) -> str | None:
    if not list((root / "demos").glob(f"demo_{nn}_*.py")):
        return f"demo: demos/demo_{nn}_*.py not found"
    return None


def _results_row(nn: str, text: str) -> dict[str, str] | None:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        # `record_result` escapes a literal pipe as `\|`; split on unescaped pipes only.
        cells = [cell.strip() for cell in _CELL_SEPARATOR.split(stripped.strip("|"))]
        if cells and cells[0] == nn:
            padded = cells + [""] * (len(RESULTS_COLUMNS) - len(cells))
            return dict(zip(RESULTS_COLUMNS, padded, strict=False))
    return None


def _check_results(nn: str, root: Path) -> str | None:
    path = root / "docs" / "results.md"
    if not path.is_file():
        return "results: docs/results.md not found"
    row = _results_row(nn, path.read_text(encoding="utf-8"))
    if row is None:
        return f"results: docs/results.md has no `| {nn} |` row"
    empty = [name.capitalize() for name in ("before", "after") if not row[name]]
    if empty:
        return f"results: docs/results.md row {nn} is missing {' and '.join(empty)}"
    return None


def _check_adr(nn: str, root: Path) -> str | None:
    pattern = f"docs/adr/00{nn}-*.md"
    matches = sorted((root / "docs" / "adr").glob(f"00{nn}-*.md"))
    if not matches:
        return f"adr: {pattern} not found"
    adr = matches[0]
    relative = adr.relative_to(root).as_posix()
    text = adr.read_text(encoding="utf-8")
    missing_sections = [
        section
        for section in ADR_SECTIONS
        if not re.search(rf"^#{{1,6}}\s+{section}\b", text, flags=re.MULTILINE | re.IGNORECASE)
    ]
    if missing_sections:
        return f"adr: {relative} is missing sections: {', '.join(missing_sections)}"
    try:
        language = detect(text)
    except LangDetectException:
        language = "unknown"
    if language != "en":
        return f"adr: {relative} must be written in English (detected {language!r})"
    return None


def _check_tag(nn: str, root: Path) -> str | None:
    tag = f"phase-{nn}"
    git = shutil.which("git")
    if git is None:
        return f"tag: git not found, cannot verify tag {tag}"
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [git, "-C", str(root), "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return f"tag: git tag {tag} not found"
    return None


def check_phase(phase: int, root: Path, require_tag: bool = False) -> list[str]:
    """Return one human-readable line per missing artifact; empty means the phase is ready."""
    nn = f"{phase:02d}"
    checks = [
        _check_eval_cases(phase, root),
        _check_demo(nn, root),
        _check_results(nn, root),
        _check_adr(nn, root),
    ]
    if require_tag:
        checks.append(_check_tag(nn, root))
    return [item for item in checks if item is not None]


def _phase_number(value: str) -> int:
    try:
        phase = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"invalid phase {value!r}") from error
    if not FIRST_PHASE <= phase <= LAST_PHASE:
        raise argparse.ArgumentTypeError(
            f"phase must be between {FIRST_PHASE:02d} and {LAST_PHASE:02d}"
        )
    return phase


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check a phase's definition of done.")
    parser.add_argument("phase", type=_phase_number, help="phase number, e.g. 01")
    parser.add_argument("--require-tag", action="store_true", help="also require tag phase-NN")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="repository root")
    args = parser.parse_args(argv)

    missing = check_phase(args.phase, args.root, require_tag=args.require_tag)
    nn = f"{args.phase:02d}"
    if missing:
        print(f"Phase {nn} is not ready. Missing:")
        for item in missing:
            print(f"- {item}")
        return EXIT_NOT_READY
    print(f"Phase {nn} is ready.")
    return EXIT_READY


if __name__ == "__main__":
    sys.exit(main())
