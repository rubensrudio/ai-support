"""Versioned prompt files loader (CT-13).

Prompts live in `prompts/<name>.md` at the repository root (a change there triggers the
evals, EVAL-03). The path is resolved from this file, never from the current directory,
and only names matching `[a-z0-9_-]+` are accepted, so no path traversal is possible.
"""

import re
from functools import lru_cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"
_NAME_PATTERN = re.compile(r"[a-z0-9_-]+")


class PromptNotFound(LookupError):
    """No prompt file with that name (or the name is not a valid prompt name)."""

    def __init__(self, name: str) -> None:
        super().__init__(f"Prompt not found: {name!r}")
        self.name = name


@lru_cache(maxsize=64)
def load_prompt(name: str) -> str:
    """Text of `prompts/<name>.md`, read once per process."""
    if not _NAME_PATTERN.fullmatch(name):
        raise PromptNotFound(name)
    path = PROMPTS_DIR / f"{name}.md"
    if not path.is_file():
        raise PromptNotFound(name)
    return path.read_text(encoding="utf-8")
