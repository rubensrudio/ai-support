"""Shared helpers for the Assinaflow support corpus tests (TASK-040, TASK-041)."""

import re
from pathlib import Path
from typing import Any

import yaml

CORPUS = Path(__file__).resolve().parents[3] / "corpus"
MANIFEST = CORPUS / "manifest.yaml"
LANGUAGES = ("en", "pt")

# Phrases that the phase 10 ingestion scan treats as prompt injection (CT-52). Only the
# malicious document may contain them, or a legitimate document would be quarantined.
INJECTION_MARKERS = (
    "ignore previous instructions",
    "ignore all previous instructions",
    "you are now",
    "system prompt",
    "developer mode",
    "ignore as instruções anteriores",
    "ignore todas as instruções anteriores",
    "você agora é",
    "prompt do sistema",
    "revele",
)

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)


def load_manifest() -> dict[str, Any]:
    with MANIFEST.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    assert isinstance(data, dict), "manifest.yaml must be a mapping"
    return data


def documents() -> list[dict[str, Any]]:
    docs = load_manifest().get("documents")
    assert isinstance(docs, list) and docs, "manifest.yaml must list documents"
    return docs


def source_text(doc: dict[str, Any], language: str) -> str:
    return (CORPUS / doc["source"][language]).read_text(encoding="utf-8")


def headings(markdown: str) -> list[tuple[int, str]]:
    return [(len(m.group(1)), m.group(2)) for m in _HEADING.finditer(markdown)]


def by_key() -> dict[str, dict[str, Any]]:
    return {doc["doc_key"]: doc for doc in documents()}
