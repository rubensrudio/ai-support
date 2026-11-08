"""Prompt loader and base system prompt (TASK-018, CT-13)."""

import re
from pathlib import Path

import pytest

from api.prompts import PromptNotFound, load_prompt


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    load_prompt.cache_clear()


def test_system_prompt_has_language_placeholder_and_citation_rule() -> None:
    prompt = load_prompt("system")

    assert "{language}" in prompt
    assert "[n]" in prompt


def test_system_prompt_only_placeholder_is_language() -> None:
    prompt = load_prompt("system")

    assert re.findall(r"\{[^}]*\}", prompt) == ["{language}"] * prompt.count("{language}")
    rendered = prompt.format(language="English")
    assert "{" not in rendered and "}" not in rendered


def test_system_prompt_has_no_dynamic_data() -> None:
    prompt = load_prompt("system")

    assert not re.search(r"\b(19|20)\d{2}\b", prompt), "no dates or years"
    assert not re.search(r"\d{1,2}:\d{2}", prompt), "no times"
    for persona in ("alex", "bia"):
        assert not re.search(rf"\b{persona}\b", prompt, re.IGNORECASE)


def test_system_prompt_says_it_does_not_know_outside_excerpts() -> None:
    prompt = load_prompt("system").lower()

    assert "don't know" in prompt or "do not know" in prompt


def test_unknown_prompt_raises_prompt_not_found() -> None:
    with pytest.raises(PromptNotFound):
        load_prompt("inexistente")


@pytest.mark.parametrize(
    "name", ["../pyproject", "..", "a/b", "/etc/passwd", "System", "system.md", "", "sys tem"]
)
def test_invalid_names_raise_prompt_not_found(name: str) -> None:
    with pytest.raises(PromptNotFound):
        load_prompt(name)


def test_prompt_not_found_is_lookup_error() -> None:
    assert issubclass(PromptNotFound, LookupError)


def test_load_prompt_does_not_depend_on_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    assert "{language}" in load_prompt("system")


def test_load_prompt_is_cached() -> None:
    assert load_prompt("system") is load_prompt("system")
