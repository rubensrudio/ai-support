"""Eval cases stored as JSONL, one case per line (CT-10)."""

import glob
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

Category = Literal["rag", "tool_trajectory", "json_valid", "red_team", "tone", "smoke"]
Language = Literal["pt", "en"]


class EvalCase(BaseModel):
    """One eval case. Unknown fields are rejected so typos never pass silently."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: str = Field(min_length=1)
    phase: int = Field(ge=0)
    category: Category
    language: Language
    input: dict[str, Any]
    expected: dict[str, Any]
    grader: str = Field(min_length=1)


class DatasetError(ValueError):
    """A dataset line could not be loaded. The message cites `<file>:<line>`."""

    def __init__(self, path: Path, line_number: int, reason: str) -> None:
        super().__init__(f"{path}:{line_number}: {reason}")
        self.path = path
        self.line_number = line_number
        self.reason = reason


def _validation_reason(error: ValidationError) -> str:
    parts = []
    for item in error.errors():
        location = ".".join(str(part) for part in item["loc"]) or "case"
        parts.append(f"{location}: {item['msg']}")
    return "; ".join(parts)


def load_cases(paths: list[Path]) -> list[EvalCase]:
    """Load and validate every case of the given JSONL files, in order.

    Blank lines are skipped. Raises `DatasetError` on invalid JSON, a schema violation or a
    case id already used earlier in the same load.
    """
    cases: list[EvalCase] = []
    seen: dict[str, str] = {}
    for path in paths:
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError as error:
                    raise DatasetError(path, line_number, f"invalid JSON ({error.msg})") from error
                try:
                    case = EvalCase.model_validate(raw)
                except ValidationError as error:
                    raise DatasetError(path, line_number, _validation_reason(error)) from error
                location = f"{path}:{line_number}"
                if case.id in seen:
                    raise DatasetError(
                        path,
                        line_number,
                        f"duplicate case id {case.id!r} (first at {seen[case.id]})",
                    )
                seen[case.id] = location
                cases.append(case)
    return cases


def resolve_dataset_paths(pattern: str) -> list[Path]:
    """Expand a file path or glob pattern into a sorted list of existing files."""
    return sorted(
        Path(match) for match in glob.glob(pattern, recursive=True) if Path(match).is_file()
    )
