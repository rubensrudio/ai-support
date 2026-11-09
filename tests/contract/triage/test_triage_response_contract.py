"""`POST /triage` response contract (CT-48, CT-49, CT-74, TRI-01, TRI-03, TRI-06, TRI-07).

200 is exactly the five `TriageResult` fields with values from the closed enums. When the model
keeps answering invalid output until the repairs run out, the answer is 503
`TRIAGE_UNAVAILABLE` with only the error envelope: no triage object and no ticket text.
"""

from typing import Any, get_args

import httpx
import pytest

from api.config import get_settings
from api.llm.fake import FakeLLMClient
from api.triage.schemas import (
    UNSUPPORTED_SCHEMA_KEYWORDS,
    Categoria,
    Prioridade,
    Sentimento,
    TriageResult,
    triage_output_schema,
)

pytestmark = pytest.mark.integration

FIELDS = {"categoria", "prioridade", "sentimento", "precisa_humano", "resumo"}
MARKER = "zq-ticket-marker-9b07"
TICKET_TEXT = f"Order {MARKER} never arrived, I am furious and want to talk to a person."


def _invalid_answers(valid: dict[str, Any]) -> list[dict[str, Any] | str]:
    """Answers that `TriageResult` must reject, one kind of defect each."""
    missing = {k: v for k, v in valid.items() if k != "resumo"}
    return [
        "this is not json",
        {**valid, "categoria": "billing"},
        {**valid, "prioridade": "high"},
        {**valid, "sentimento": "angry"},
        {**valid, "conversation_id": "abc"},
        missing,
        {**valid, "resumo": ""},
        '```json\n{"categoria": "pedido"}\n```',
    ]


@pytest.mark.parametrize(
    ("categoria", "prioridade", "sentimento", "precisa_humano"),
    [
        ("pedido", "baixa", "positivo", False),
        ("assinatura", "media", "neutro", False),
        ("reembolso", "alta", "negativo", True),
        ("entrega", "urgente", "negativo", True),
        ("conta", "media", "neutro", False),
        ("outro", "baixa", "positivo", False),
    ],
)
async def test_success_has_exactly_the_five_fields_with_enum_values(
    client: httpx.AsyncClient,
    fake_llm: FakeLLMClient,
    persona_headers: dict[str, str],
    answer: Any,
    categoria: str,
    prioridade: str,
    sentimento: str,
    precisa_humano: bool,
) -> None:
    triage = {
        "categoria": categoria,
        "prioridade": prioridade,
        "sentimento": sentimento,
        "precisa_humano": precisa_humano,
        "resumo": "Short summary of the ticket.",
    }
    fake_llm.enqueue(answer(triage))

    response = await client.post("/triage", json={"text": TICKET_TEXT}, headers=persona_headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == FIELDS
    assert body["categoria"] in get_args(Categoria)
    assert body["prioridade"] in get_args(Prioridade)
    assert body["sentimento"] in get_args(Sentimento)
    assert isinstance(body["precisa_humano"], bool)
    assert isinstance(body["resumo"], str)
    assert body["resumo"]
    assert body == triage
    TriageResult.model_validate(body)


async def test_schema_mode_requests_native_structured_output(
    client: httpx.AsyncClient,
    fake_llm: FakeLLMClient,
    operator_headers: dict[str, str],
    answer: Any,
    valid_triage: dict[str, Any],
) -> None:
    fake_llm.enqueue(answer(valid_triage))

    response = await client.post("/triage", json={"text": TICKET_TEXT}, headers=operator_headers)

    assert response.status_code == 200, response.text
    assert len(fake_llm.requests) == 1
    schema = fake_llm.requests[0].response_schema
    assert schema is not None
    assert schema == triage_output_schema()
    _assert_provider_safe_triage_schema(schema)


async def test_one_invalid_answer_is_repaired(
    client: httpx.AsyncClient,
    fake_llm: FakeLLMClient,
    persona_headers: dict[str, str],
    answer: Any,
    valid_triage: dict[str, Any],
) -> None:
    fake_llm.enqueue(answer({**valid_triage, "categoria": "billing"}), answer(valid_triage))

    response = await client.post("/triage", json={"text": TICKET_TEXT}, headers=persona_headers)

    assert response.status_code == 200, response.text
    assert response.json() == valid_triage
    assert len(fake_llm.requests) == 2


@pytest.mark.parametrize("defect", range(8))
async def test_invalid_answers_until_repairs_run_out_is_triage_unavailable(
    client: httpx.AsyncClient,
    fake_llm: FakeLLMClient,
    persona_headers: dict[str, str],
    answer: Any,
    valid_triage: dict[str, Any],
    max_repairs: int,
    defect: int,
) -> None:
    bad = _invalid_answers(valid_triage)[defect]
    # More invalid answers than the service may ask for, then a valid one it must never reach.
    fake_llm.enqueue(*(answer(bad) for _ in range(max_repairs + 3)), answer(valid_triage))

    response = await client.post("/triage", json={"text": TICKET_TEXT}, headers=persona_headers)

    _assert_triage_unavailable(response)
    assert len(fake_llm.requests) == 1 + max_repairs


@pytest.mark.parametrize("repairs", [0, 1])
async def test_repair_budget_follows_the_setting(
    monkeypatch: pytest.MonkeyPatch,
    client: httpx.AsyncClient,
    fake_llm: FakeLLMClient,
    operator_headers: dict[str, str],
    answer: Any,
    valid_triage: dict[str, Any],
    repairs: int,
) -> None:
    monkeypatch.setenv("ATLAS_TRIAGE_MAX_REPAIRS", str(repairs))
    get_settings.cache_clear()
    fake_llm.enqueue(*(answer("not json") for _ in range(5)))

    response = await client.post("/triage", json={"text": TICKET_TEXT}, headers=operator_headers)

    _assert_triage_unavailable(response)
    assert len(fake_llm.requests) == 1 + repairs


async def test_mixed_invalid_answers_is_triage_unavailable(
    client: httpx.AsyncClient,
    fake_llm: FakeLLMClient,
    operator_headers: dict[str, str],
    answer: Any,
    valid_triage: dict[str, Any],
    max_repairs: int,
) -> None:
    bad = _invalid_answers(valid_triage)
    fake_llm.enqueue(*(answer(item) for item in bad))
    assert len(bad) > max_repairs + 1

    response = await client.post("/triage", json={"text": TICKET_TEXT}, headers=operator_headers)

    _assert_triage_unavailable(response)
    assert len(fake_llm.requests) == 1 + max_repairs


def _schema_keys(node: Any) -> set[str]:
    """Every key used anywhere in a JSON schema (nested objects, lists and `$defs`)."""
    if isinstance(node, dict):
        keys = set(node)
        for value in node.values():
            keys |= _schema_keys(value)
        return keys
    if isinstance(node, list):
        return set().union(*(_schema_keys(item) for item in node)) if node else set()
    return set()


def _enum_values(schema: dict[str, Any], prop: dict[str, Any]) -> set[Any]:
    """Allowed values of a property, following a local `$ref` and `enum`/`const`/`anyOf`."""
    if "$ref" in prop:
        name = prop["$ref"].rsplit("/", 1)[-1]
        prop = schema.get("$defs", {}).get(name, {})
    if "enum" in prop:
        return set(prop["enum"])
    if "const" in prop:
        return {prop["const"]}
    options = prop.get("anyOf") or prop.get("oneOf") or []
    return set().union(*(_enum_values(schema, option) for option in options)) if options else set()


def _assert_provider_safe_triage_schema(schema: dict[str, Any]) -> None:
    """CT-74: provider-safe schema: 5 required fields, closed enums, no extra properties."""
    assert schema.get("type") == "object"
    assert set(schema["properties"]) == FIELDS
    assert set(schema["required"]) == FIELDS
    assert schema.get("additionalProperties") is False
    props = schema["properties"]
    assert _enum_values(schema, props["categoria"]) == set(get_args(Categoria))
    assert _enum_values(schema, props["prioridade"]) == set(get_args(Prioridade))
    assert _enum_values(schema, props["sentimento"]) == set(get_args(Sentimento))
    assert props["precisa_humano"].get("type") == "boolean"
    assert props["resumo"].get("type") == "string"
    assert not _schema_keys(schema) & UNSUPPORTED_SCHEMA_KEYWORDS


def _assert_triage_unavailable(response: httpx.Response) -> None:
    assert response.status_code == 503, response.text
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "trace_id"}
    assert body["error"]["code"] == "TRIAGE_UNAVAILABLE"
    assert body["error"]["trace_id"] == response.headers["x-trace-id"]
    for field in FIELDS:
        assert field not in response.text
    assert MARKER not in response.text
