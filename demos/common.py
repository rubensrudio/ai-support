"""Shared helpers for the `demo_NN` scripts (CT-12, DOD-02, DA-16).

Every demo takes `--provider {real,fake}`, `--results` and `--out-dir`. `fake` runs offline
with a scripted `FakeLLMClient` (CI and tests, no key); `real` builds the configured client.
`record_result` writes the phase metric into the `| Phase | Metric | Before | After | Notes |`
table of `docs/results.md`, replacing that phase's row, so reruns never duplicate it.
"""

import argparse
import re
from pathlib import Path

from api.config import get_settings
from api.llm.client import LLMClient, LLMError, LLMResponse
from api.llm.factory import build_llm_client
from api.llm.fake import FAKE_MODEL, FAKE_PROVIDER, FakeLLMClient
from api.llm.instrumented import InstrumentedLLMClient

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_RESULTS_PATH = ROOT_DIR / "docs" / "results.md"
DEFAULT_OUT_DIR = ROOT_DIR / "docs" / "img"
PROVIDERS = ("real", "fake")

TABLE_HEADER = "| Phase | Metric | Before | After | Notes |"
TABLE_SEPARATOR = "|---|---|---|---|---|"
_PHASE_ROW = re.compile(r"^\|\s*(\d{1,2})\s*\|")


def demo_parser(description: str) -> argparse.ArgumentParser:
    """Parser with the common demo flags; demos add their own flags before parsing."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--provider", choices=PROVIDERS, default="real")
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS_PATH)
    parser.add_argument("--out-dir", dest="out_dir", type=Path, default=DEFAULT_OUT_DIR)
    return parser


def parse_demo_args(description: str, argv: list[str] | None = None) -> argparse.Namespace:
    return demo_parser(description).parse_args(argv)


def build_demo_llm(
    provider: str, *, script: list[LLMResponse | LLMError] | None = None
) -> LLMClient:
    """`fake`: instrumented scripted fake, no key needed. `real`: the configured client."""
    if provider == "fake":
        return InstrumentedLLMClient(
            FakeLLMClient(script=list(script or [])), model=FAKE_MODEL, provider=FAKE_PROVIDER
        )
    if provider == "real":
        return build_llm_client(get_settings())
    raise ValueError(f"Unknown demo provider {provider!r}; expected one of {PROVIDERS}")


def _cell(value: str) -> str:
    return " ".join(value.replace("|", r"\|").split())


def _format_row(phase: int, metric: str, before: str, after: str, notes: str) -> str:
    cells = [f"{phase:02d}", _cell(metric), _cell(before), _cell(after), _cell(notes)]
    return "| " + " | ".join(cells) + " |"


def _row_phase(line: str) -> int | None:
    match = _PHASE_ROW.match(line)
    return int(match.group(1)) if match else None


def record_result(
    phase: int,
    metric: str,
    before: str,
    after: str,
    notes: str = "",
    results_path: Path = DEFAULT_RESULTS_PATH,
) -> None:
    """Set the row of `phase` in the results table; any duplicate rows of it are removed."""
    if not 1 <= phase <= 99:
        raise ValueError(f"phase must be between 1 and 99, got {phase}")
    row = _format_row(phase, metric, before, after, notes)
    text = results_path.read_text(encoding="utf-8") if results_path.exists() else ""
    lines = text.splitlines()

    try:
        header_index = next(i for i, line in enumerate(lines) if line.strip() == TABLE_HEADER)
    except StopIteration:
        prefix = [*lines, ""] if lines else []
        lines = [*prefix, TABLE_HEADER, TABLE_SEPARATOR, row]
    else:
        start = header_index + 2  # header and separator
        end = start
        while end < len(lines) and lines[end].lstrip().startswith("|"):
            end += 1
        body = [line for line in lines[start:end] if _row_phase(line) != phase]
        position = next(
            (i for i, line in enumerate(body) if (_row_phase(line) or 0) > phase), len(body)
        )
        body.insert(position, row)
        lines = [*lines[:start], *body, *lines[end:]]

    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
