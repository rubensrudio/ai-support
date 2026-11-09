"""`POST /triage` input validation contract (CT-48, CT-49, TRI-05, TRI-90).

The body is `TriageRequest`: only `text`, 1..`ATLAS_MAX_MESSAGE_CHARS` characters, not blank.
Anything else is 422 `VALIDATION_ERROR` with the standard envelope, and the LLM is never called.
The requests are authenticated, so the 422 comes from validation and not from access control.
"""

from typing import Any

import httpx
import pytest

from api.llm.fake import FakeLLMClient

pytestmark = pytest.mark.integration

MARKER = "zq-ticket-marker-58c1"


def _assert_validation_error(response: httpx.Response, fake_llm: FakeLLMClient) -> None:
    assert response.status_code == 422, response.text
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "trace_id"}
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["trace_id"] == response.headers["x-trace-id"]
    assert len(fake_llm.requests) == 0


@pytest.mark.parametrize("text", ["", " ", "     ", "\n", "\t \n \r"])
async def test_empty_or_blank_text_is_a_validation_error(
    client: httpx.AsyncClient,
    fake_llm: FakeLLMClient,
    operator_headers: dict[str, str],
    answer: Any,
    valid_triage: dict[str, Any],
    text: str,
) -> None:
    fake_llm.enqueue(answer(valid_triage))

    response = await client.post("/triage", json={"text": text}, headers=operator_headers)

    _assert_validation_error(response, fake_llm)


async def test_text_above_the_limit_is_a_validation_error(
    client: httpx.AsyncClient,
    fake_llm: FakeLLMClient,
    persona_headers: dict[str, str],
    answer: Any,
    valid_triage: dict[str, Any],
    max_chars: int,
) -> None:
    text = MARKER + "x" * (max_chars + 1 - len(MARKER))
    assert len(text) == max_chars + 1
    fake_llm.enqueue(answer(valid_triage))

    response = await client.post("/triage", json={"text": text}, headers=persona_headers)

    _assert_validation_error(response, fake_llm)
    assert MARKER not in response.text


async def test_text_exactly_at_the_limit_is_accepted(
    client: httpx.AsyncClient,
    fake_llm: FakeLLMClient,
    persona_headers: dict[str, str],
    answer: Any,
    valid_triage: dict[str, Any],
    max_chars: int,
) -> None:
    fake_llm.enqueue(answer(valid_triage))

    response = await client.post("/triage", json={"text": "y" * max_chars}, headers=persona_headers)

    assert response.status_code == 200, response.text
    assert len(fake_llm.requests) == 1


@pytest.mark.parametrize(
    "body",
    [
        {"text": "Where is my order?", "conversation_id": "7d4b6c1e-0f53-4c63-9d6a-3c1f2b0e9a11"},
        {"conversation_id": "7d4b6c1e-0f53-4c63-9d6a-3c1f2b0e9a11"},
        {"text": "Where is my order?", "categoria": "pedido"},
        {},
        {"message": "Where is my order?"},
        {"text": None},
        {"text": 123},
        {"text": ["Where is my order?"]},
    ],
    ids=[
        "extra-conversation_id",
        "only-conversation_id",
        "extra-field",
        "empty-object",
        "no-text",
        "null-text",
        "number-text",
        "list-text",
    ],
)
async def test_malformed_body_is_a_validation_error(
    client: httpx.AsyncClient,
    fake_llm: FakeLLMClient,
    operator_headers: dict[str, str],
    answer: Any,
    valid_triage: dict[str, Any],
    body: dict[str, Any],
) -> None:
    fake_llm.enqueue(answer(valid_triage))

    response = await client.post("/triage", json=body, headers=operator_headers)

    _assert_validation_error(response, fake_llm)


@pytest.mark.parametrize("raw", [b"", b"not json", b'"just a string"', b"[]"])
async def test_non_object_body_is_a_validation_error(
    client: httpx.AsyncClient,
    fake_llm: FakeLLMClient,
    operator_headers: dict[str, str],
    answer: Any,
    valid_triage: dict[str, Any],
    raw: bytes,
) -> None:
    fake_llm.enqueue(answer(valid_triage))
    headers = {**operator_headers, "Content-Type": "application/json"}

    response = await client.post("/triage", content=raw, headers=headers)

    _assert_validation_error(response, fake_llm)
