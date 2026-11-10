"""Composition of the full eval dataset: size, category x language coverage, unique ids and the
shape of the phase 13 core cases (EVAL-01, EVAL-08, TASK-124)."""

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml

from api.tools.catalog import build_default_registry
from evals.dataset import EvalCase, load_cases

ROOT_DIR = Path(__file__).resolve().parents[3]
DATASETS_DIR = ROOT_DIR / "evals" / "datasets"
CORE_FILES = (DATASETS_DIR / "core_en.jsonl", DATASETS_DIR / "core_pt.jsonl")
MANIFEST = ROOT_DIR / "corpus" / "manifest.yaml"

GRADED_CATEGORIES = ("rag", "tool_trajectory", "json_valid", "red_team", "tone")
LANGUAGES = ("pt", "en")
MIN_CASES = 90
MAX_CASES = 130
PERSONA_BY_LANGUAGE = {"en": "alex", "pt": "bia"}


def _all_paths() -> list[Path]:
    return sorted(DATASETS_DIR.glob("*.jsonl"))


def _non_smoke(cases: list[EvalCase]) -> list[EvalCase]:
    return [case for case in cases if case.category != "smoke"]


def _core_cases() -> list[EvalCase]:
    return load_cases(list(CORE_FILES))


def _corpus_doc_keys() -> dict[str, bool]:
    manifest: dict[str, Any] = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    return {doc["doc_key"]: bool(doc["malicious"]) for doc in manifest["documents"]}


def test_core_files_exist() -> None:
    missing = [path.name for path in CORE_FILES if not path.is_file()]

    assert missing == []


def test_every_dataset_file_loads() -> None:
    cases = load_cases(_all_paths())

    assert {path.name for path in CORE_FILES} <= {path.name for path in _all_paths()}
    assert len(cases) > 0


def test_non_smoke_total_is_about_one_hundred() -> None:
    total = len(_non_smoke(load_cases(_all_paths())))

    assert MIN_CASES <= total <= MAX_CASES, total


def _missing_coverage(cases: list[EvalCase]) -> list[tuple[str, str]]:
    counts: Counter[tuple[str, str]] = Counter((case.category, case.language) for case in cases)
    return [
        (category, language)
        for category in GRADED_CATEGORIES
        for language in LANGUAGES
        if counts[(category, language)] == 0
    ]


def test_every_category_has_cases_in_both_languages() -> None:
    assert _missing_coverage(load_cases(_all_paths())) == []


def test_core_covers_every_category_in_both_languages() -> None:
    assert _missing_coverage(_core_cases()) == []


def test_case_ids_are_unique_across_files() -> None:
    owners: dict[str, list[str]] = {}
    for path in _all_paths():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                owners.setdefault(json.loads(line)["id"], []).append(path.name)

    duplicates = {case_id: files for case_id, files in owners.items() if len(files) > 1}
    assert duplicates == {}


def test_core_cases_are_phase_13_with_core_prefix() -> None:
    cases = _core_cases()

    assert {case.phase for case in cases} == {13}
    assert all(case.id.startswith("core-") for case in cases)
    assert all(case.category != "smoke" for case in cases)


@pytest.mark.parametrize("path", CORE_FILES, ids=lambda path: path.name)
def test_core_file_holds_a_single_language(path: Path) -> None:
    language = path.stem.removeprefix("core_")

    assert {case.language for case in load_cases([path])} == {language}


def test_core_grader_is_the_category_default() -> None:
    assert all(case.grader == case.category for case in _core_cases())


def test_core_chat_cases_use_the_persona_of_their_language() -> None:
    chat_cases = [case for case in _core_cases() if case.category != "json_valid"]

    for case in chat_cases:
        assert set(case.input) <= {"message", "persona", "confirm", "set"}, case.id
        assert isinstance(case.input["message"], str), case.id
        assert case.input["message"].strip(), case.id
        assert case.input["persona"] == PERSONA_BY_LANGUAGE[case.language], case.id


def test_core_rag_cases_cite_a_real_safe_corpus_document() -> None:
    doc_keys = _corpus_doc_keys()
    rag = [case for case in _core_cases() if case.category == "rag"]

    for case in rag:
        assert set(case.expected) == {"citation_doc_key"}, case.id
        key = case.expected["citation_doc_key"]
        assert key in doc_keys, case.id
        assert doc_keys[key] is False, case.id


def test_core_tool_trajectory_cases_use_registered_tools() -> None:
    tools = set(build_default_registry().names())
    trajectory = [case for case in _core_cases() if case.category == "tool_trajectory"]

    for case in trajectory:
        assert case.input["confirm"] is True, case.id
        assert set(case.expected) == {"trajectory"}, case.id
        steps = case.expected["trajectory"]
        assert isinstance(steps, list), case.id
        assert steps, case.id
        assert set(steps) <= tools, case.id


def test_core_json_valid_cases_carry_only_the_triage_text() -> None:
    triage = [case for case in _core_cases() if case.category == "json_valid"]

    for case in triage:
        assert set(case.input) == {"text"}, case.id
        assert case.input["text"].strip(), case.id
        assert case.expected == {"json_valid": True}, case.id


def test_core_red_team_cases_mix_attacks_and_controls() -> None:
    red_team = [case for case in _core_cases() if case.category == "red_team"]

    for case in red_team:
        is_control = case.input.get("set") == "control"
        assert case.expected == {"blocked": not is_control}, case.id
    for language in LANGUAGES:
        sets = {
            case.input.get("set") == "control" for case in red_team if case.language == language
        }
        assert sets == {True, False}, language


def test_core_tone_cases_expect_a_polite_answer() -> None:
    tone = [case for case in _core_cases() if case.category == "tone"]

    assert all(case.expected == {"tone": "polite"} for case in tone)
