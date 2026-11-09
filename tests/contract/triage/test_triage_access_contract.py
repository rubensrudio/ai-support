"""`POST /triage` access contract (CT-16, CT-49, LAC-35, AS-1).

Allowed: a persona session cookie or the configured `X-Operator-Key`. Everything else is
401 `AUTH_REQUIRED` with the standard error envelope, and the LLM is never called.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.deps import OPERATOR_HEADER, SESSION_COOKIE
from api.auth.models import CustomerSession
from api.auth.service import hash_token
from api.llm.fake import FakeLLMClient

pytestmark = pytest.mark.integration

TICKET = {"text": "My package arrived broken and I want my money back."}


def _cookie(token: str) -> dict[str, str]:
    return {"Cookie": f"{SESSION_COOKIE}={token}"}


def _assert_auth_required(response: httpx.Response, fake_llm: FakeLLMClient) -> None:
    assert response.status_code == 401, response.text
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "trace_id"}
    assert body["error"]["code"] == "AUTH_REQUIRED"
    assert body["error"]["trace_id"] == response.headers["x-trace-id"]
    assert len(fake_llm.requests) == 0


async def test_persona_session_gets_a_triage(
    client: httpx.AsyncClient,
    fake_llm: FakeLLMClient,
    persona_headers: dict[str, str],
    answer: Any,
    valid_triage: dict[str, Any],
) -> None:
    fake_llm.enqueue(answer(valid_triage))

    response = await client.post("/triage", json=TICKET, headers=persona_headers)

    assert response.status_code == 200, response.text
    assert response.json() == valid_triage
    assert len(fake_llm.requests) == 1


async def test_operator_key_gets_a_triage(
    client: httpx.AsyncClient,
    fake_llm: FakeLLMClient,
    operator_headers: dict[str, str],
    answer: Any,
    valid_triage: dict[str, Any],
) -> None:
    fake_llm.enqueue(answer(valid_triage))

    response = await client.post("/triage", json=TICKET, headers=operator_headers)

    assert response.status_code == 200, response.text
    assert response.json() == valid_triage
    assert len(fake_llm.requests) == 1


async def test_no_credentials_is_auth_required(
    client: httpx.AsyncClient, fake_llm: FakeLLMClient, answer: Any, valid_triage: dict[str, Any]
) -> None:
    fake_llm.enqueue(answer(valid_triage))

    _assert_auth_required(await client.post("/triage", json=TICKET), fake_llm)


@pytest.mark.parametrize("variant", ["", "wrong-key", "suffix", "prefix", "upper"])
async def test_wrong_operator_key_is_auth_required(
    client: httpx.AsyncClient,
    fake_llm: FakeLLMClient,
    operator_key: str,
    answer: Any,
    valid_triage: dict[str, Any],
    variant: str,
) -> None:
    key = {
        "": "",
        "wrong-key": "wrong-key",
        "suffix": operator_key + "x",
        "prefix": operator_key[:-1],
        "upper": operator_key.upper(),
    }[variant]
    fake_llm.enqueue(answer(valid_triage))

    response = await client.post("/triage", json=TICKET, headers={OPERATOR_HEADER: key})

    _assert_auth_required(response, fake_llm)


@pytest.mark.parametrize("key", ["", "None", "null", "triage-contract-operator-key-4e7b"])
async def test_any_operator_key_is_auth_required_when_operator_is_disabled(
    operator_disabled: None,
    client: httpx.AsyncClient,
    fake_llm: FakeLLMClient,
    answer: Any,
    valid_triage: dict[str, Any],
    key: str,
) -> None:
    fake_llm.enqueue(answer(valid_triage))

    response = await client.post("/triage", json=TICKET, headers={OPERATOR_HEADER: key})

    _assert_auth_required(response, fake_llm)


async def test_tampered_session_cookie_is_auth_required(
    client: httpx.AsyncClient,
    fake_llm: FakeLLMClient,
    persona_token: str,
    answer: Any,
    valid_triage: dict[str, Any],
) -> None:
    flipped = persona_token[:-1] + ("A" if persona_token[-1] != "A" else "B")
    fake_llm.enqueue(answer(valid_triage))

    for forged in (flipped, persona_token + "x", persona_token[:-4], "not-a-real-token"):
        response = await client.post("/triage", json=TICKET, headers=_cookie(forged))
        _assert_auth_required(response, fake_llm)


async def test_expired_session_cookie_is_auth_required(
    client: httpx.AsyncClient,
    fake_llm: FakeLLMClient,
    persona_token: str,
    db_session: AsyncSession,
    answer: Any,
    valid_triage: dict[str, Any],
) -> None:
    result = await db_session.execute(
        update(CustomerSession)
        .where(CustomerSession.token_hash == hash_token(persona_token))
        .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
    )
    await db_session.flush()
    assert result.rowcount == 1  # type: ignore[attr-defined]
    fake_llm.enqueue(answer(valid_triage))

    response = await client.post("/triage", json=TICKET, headers=_cookie(persona_token))

    _assert_auth_required(response, fake_llm)
