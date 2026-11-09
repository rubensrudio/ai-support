"""`require_customer_or_operator` contract (CT-16, LAC-35, AS-7).

Exercised through a minimal app with one route that depends on it, so the test does not rely on
any route that uses it in production (triage, phase 08).
"""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated
from uuid import UUID

import httpx
import pytest
from auth_contract_helpers import cookie_header
from fastapi import Depends, FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.deps import (
    OPERATOR_HEADER,
    OperatorPrincipal,
    Principal,
    require_customer_or_operator,
)
from api.auth.service import create_session
from api.config import get_settings
from api.db.engine import get_db_session
from api.errors import install_error_handlers
from api.observability.middleware import TraceIdMiddleware

pytestmark = pytest.mark.integration

OPERATOR_KEY = "contract-operator-key-91c2"


def _operator_app(db_session: AsyncSession) -> FastAPI:
    app = FastAPI()
    app.add_middleware(TraceIdMiddleware)
    install_error_handlers(app)

    @app.get("/whoami")
    async def whoami(
        principal: Annotated[Principal, Depends(require_customer_or_operator)],
    ) -> dict[str, str]:
        if isinstance(principal, OperatorPrincipal):
            return {"kind": "operator"}
        return {"kind": "customer", "display_name": principal.display_name}

    async def _db() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = _db
    return app


@pytest.fixture
async def operator_client(
    contract_env: None, db_session: AsyncSession
) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=_operator_app(db_session), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest.fixture
def operator_key_configured(monkeypatch: pytest.MonkeyPatch, contract_env: None) -> None:
    monkeypatch.setenv("ATLAS_OPERATOR_API_KEY", OPERATOR_KEY)
    get_settings.cache_clear()


@pytest.fixture
def operator_key_unset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, contract_env: None) -> None:
    # Run from an empty directory so a developer `.env` cannot configure the key either.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ATLAS_OPERATOR_API_KEY", raising=False)
    get_settings.cache_clear()
    assert get_settings().operator_api_key is None


def _assert_auth_required(response: httpx.Response) -> None:
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"


async def test_correct_operator_key_is_accepted(
    operator_client: httpx.AsyncClient, operator_key_configured: None
) -> None:
    response = await operator_client.get("/whoami", headers={OPERATOR_HEADER: OPERATOR_KEY})

    assert response.status_code == 200
    assert response.json() == {"kind": "operator"}


@pytest.mark.parametrize(
    "key", ["", "wrong-key", OPERATOR_KEY + "x", OPERATOR_KEY[:-1], OPERATOR_KEY.upper()]
)
async def test_wrong_operator_key_is_refused(
    operator_client: httpx.AsyncClient, operator_key_configured: None, key: str
) -> None:
    _assert_auth_required(await operator_client.get("/whoami", headers={OPERATOR_HEADER: key}))


async def test_no_credentials_is_refused(
    operator_client: httpx.AsyncClient, operator_key_configured: None
) -> None:
    _assert_auth_required(await operator_client.get("/whoami"))


@pytest.mark.parametrize("key", ["", "None", "null", OPERATOR_KEY])
async def test_any_key_is_refused_when_operator_key_is_not_configured(
    operator_client: httpx.AsyncClient, operator_key_unset: None, key: str
) -> None:
    _assert_auth_required(await operator_client.get("/whoami", headers={OPERATOR_HEADER: key}))


async def test_customer_session_is_accepted_as_principal(
    operator_client: httpx.AsyncClient,
    operator_key_configured: None,
    personas: dict[str, UUID],
    db_session: AsyncSession,
) -> None:
    token = await create_session(db_session, personas["alex"])

    response = await operator_client.get("/whoami", headers=cookie_header(token))

    assert response.status_code == 200
    assert response.json() == {"kind": "customer", "display_name": "Alex Morgan"}
