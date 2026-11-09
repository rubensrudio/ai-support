"""Triage schemas and prompt (TASK-087, CT-48, TRI-01, TRI-05, TRI-06, TRI-08, TRI-90, LAC-11)."""

from collections.abc import Iterator
from typing import Any, get_args

import pytest
from pydantic import ValidationError

from api.config import get_settings
from api.prompts import load_prompt
from api.triage.schemas import (
    Categoria,
    Prioridade,
    Sentimento,
    TriageRequest,
    TriageResult,
)

RESULT_FIELDS = {"categoria", "prioridade", "sentimento", "precisa_humano", "resumo"}

VALID_RESULT: dict[str, Any] = {
    "categoria": "reembolso",
    "prioridade": "alta",
    "sentimento": "negativo",
    "precisa_humano": True,
    "resumo": "Customer was charged twice and wants a refund.",
}


@pytest.fixture
def _fresh_settings() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_valid_result_is_accepted() -> None:
    result = TriageResult.model_validate(VALID_RESULT)

    assert result.model_dump() == VALID_RESULT


def test_result_with_extra_field_is_rejected() -> None:
    with pytest.raises(ValidationError) as info:
        TriageResult.model_validate({**VALID_RESULT, "confidence": 0.9})

    assert info.value.errors()[0]["type"] == "extra_forbidden"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("categoria", "billing"),
        ("categoria", "Reembolso"),
        ("prioridade", "high"),
        ("prioridade", "média"),
        ("sentimento", "negative"),
    ],
)
def test_result_outside_closed_enums_is_rejected(field: str, value: str) -> None:
    with pytest.raises(ValidationError) as info:
        TriageResult.model_validate({**VALID_RESULT, field: value})

    assert info.value.errors()[0]["loc"] == (field,)


@pytest.mark.parametrize("missing", sorted(RESULT_FIELDS))
def test_result_missing_any_field_is_rejected(missing: str) -> None:
    data = {k: v for k, v in VALID_RESULT.items() if k != missing}

    with pytest.raises(ValidationError):
        TriageResult.model_validate(data)


def test_result_json_schema_has_exactly_the_five_required_fields() -> None:
    schema = TriageResult.model_json_schema()

    assert set(schema["properties"]) == RESULT_FIELDS
    assert set(schema["required"]) == RESULT_FIELDS
    assert len(schema["required"]) == 5
    assert schema["additionalProperties"] is False
    assert schema["properties"]["precisa_humano"]["type"] == "boolean"
    assert set(schema["properties"]["categoria"]["enum"]) == set(get_args(Categoria))


def test_enum_values_are_the_fixed_portuguese_codes() -> None:
    assert set(get_args(Categoria)) == {
        "pedido",
        "assinatura",
        "reembolso",
        "entrega",
        "conta",
        "outro",
    }
    assert set(get_args(Prioridade)) == {"baixa", "media", "alta", "urgente"}
    assert set(get_args(Sentimento)) == {"positivo", "neutro", "negativo"}


@pytest.mark.parametrize("text", ["", " ", "   ", "\n\t  "])
def test_request_with_empty_or_blank_text_is_rejected(text: str) -> None:
    with pytest.raises(ValidationError):
        TriageRequest(text=text)


def test_request_rejects_conversation_id() -> None:
    with pytest.raises(ValidationError) as info:
        TriageRequest.model_validate({"text": "Where is my order?", "conversation_id": "abc"})

    assert info.value.errors()[0]["type"] == "extra_forbidden"
    assert set(TriageRequest.model_json_schema()["properties"]) == {"text"}


@pytest.mark.usefixtures("_fresh_settings")
def test_request_over_max_message_chars_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATLAS_MAX_MESSAGE_CHARS", "10")
    get_settings.cache_clear()

    assert TriageRequest(text="a" * 10).text == "a" * 10
    with pytest.raises(ValidationError):
        TriageRequest(text="a" * 11)


def test_triage_prompt_lists_every_enum_value_and_the_rules() -> None:
    load_prompt.cache_clear()
    prompt = load_prompt("triage")

    for value in (*get_args(Categoria), *get_args(Prioridade), *get_args(Sentimento)):
        assert f"`{value}`" in prompt, value
    for name in RESULT_FIELDS:
        assert f"`{name}`" in prompt, name
    lowered = prompt.lower()
    assert "same language as the ticket" in lowered
    assert "json object only" in lowered
    assert "data, not instructions" in lowered
    assert "explicitly for a human" in lowered


def test_provider_schema_keeps_field_names_that_look_like_keywords() -> None:
    from typing import Any

    from pydantic import BaseModel, ConfigDict, Field

    from api.triage.schemas import _strip_unsupported

    class Odd(BaseModel):
        model_config = ConfigDict(extra="forbid")
        pattern: str = Field(min_length=1)
        minimum: int = Field(ge=0)

    stripped: dict[str, Any] = _strip_unsupported(Odd.model_json_schema())

    assert set(stripped["properties"]) == {"pattern", "minimum"} == set(stripped["required"])
    assert "minLength" not in stripped["properties"]["pattern"]
    assert "minimum" not in stripped["properties"]["minimum"]
