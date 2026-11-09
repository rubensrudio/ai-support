"""`POST /auth/login` contract (CT-17, DA-10, AS-7, TASK-026)."""

from uuid import UUID

import httpx
import pytest
from auth_contract_helpers import PERSONA_PASSWORD, session_cookie_from, without_trace_id

from api.auth.deps import SESSION_COOKIE
from api.config import get_settings

pytestmark = pytest.mark.integration


async def test_login_sets_http_only_strict_session_cookie(
    client: httpx.AsyncClient, personas: dict[str, UUID]
) -> None:
    response = await client.post(
        "/auth/login", json={"username": "alex", "password": PERSONA_PASSWORD}
    )

    assert response.status_code == 200
    cookie = session_cookie_from(response)
    assert cookie.name == SESSION_COOKIE
    assert cookie.value
    assert "httponly" in cookie.attributes
    assert cookie.attributes.get("samesite", "").lower() == "strict"
    assert cookie.attributes.get("path") == "/"
    assert "secure" not in cookie.attributes


async def test_login_cookie_is_secure_when_configured(
    client: httpx.AsyncClient, personas: dict[str, UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ATLAS_COOKIE_SECURE", "true")
    get_settings.cache_clear()

    response = await client.post(
        "/auth/login", json={"username": "alex", "password": PERSONA_PASSWORD}
    )

    assert response.status_code == 200
    cookie = session_cookie_from(response)
    assert "secure" in cookie.attributes
    assert "httponly" in cookie.attributes
    assert cookie.attributes.get("samesite", "").lower() == "strict"


async def test_login_body_exposes_only_public_customer_fields(
    client: httpx.AsyncClient, personas: dict[str, UUID]
) -> None:
    response = await client.post(
        "/auth/login", json={"username": "bia", "password": PERSONA_PASSWORD}
    )

    assert response.status_code == 200
    assert response.json() == {"customer": {"display_name": "Bia Souza", "language": "pt"}}
    token = session_cookie_from(response).value
    assert token not in response.text
    assert "password_hash" not in response.text
    assert "$argon2" not in response.text
    assert PERSONA_PASSWORD not in response.text
    leaked_headers = {
        name: value
        for name, value in response.headers.items()
        if name.lower() != "set-cookie" and token in value
    }
    assert leaked_headers == {}


async def test_unknown_user_and_wrong_password_get_identical_401(
    client: httpx.AsyncClient, personas: dict[str, UUID]
) -> None:
    unknown = await client.post(
        "/auth/login", json={"username": "nobody", "password": PERSONA_PASSWORD}
    )
    wrong = await client.post(
        "/auth/login", json={"username": "alex", "password": PERSONA_PASSWORD + "-wrong"}
    )

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["error"]["code"] == "INVALID_CREDENTIALS"
    assert without_trace_id(unknown.json()) == without_trace_id(wrong.json())
    assert unknown.json()["error"]["trace_id"] == unknown.headers["x-trace-id"]
    for response in (unknown, wrong):
        assert SESSION_COOKIE not in response.headers.get("set-cookie", "")
        assert "password_hash" not in response.text


@pytest.mark.parametrize(
    "payload",
    [{}, {"username": "alex"}, {"password": PERSONA_PASSWORD}, {"username": 1, "password": 2}],
)
async def test_malformed_login_body_is_validation_error(
    client: httpx.AsyncClient, personas: dict[str, UUID], payload: dict[str, object]
) -> None:
    response = await client.post("/auth/login", json=payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert SESSION_COOKIE not in response.headers.get("set-cookie", "")
