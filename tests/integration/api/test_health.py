"""App factory and health endpoint (TASK-010, FND-01, FND-03)."""

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy import make_url

from api import health
from api.config import get_settings
from api.db.engine import dispose_engine
from api.main import app as module_app
from api.main import create_app

pytestmark = pytest.mark.integration


@pytest.fixture
async def client(migrated_database: str) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


def test_module_exposes_app() -> None:
    assert module_app is not None
    assert "/health" in module_app.openapi()["paths"]


async def test_health_ok_with_trace_header(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": "ok"}
    assert response.headers["X-Trace-Id"]


async def test_health_degraded_when_database_does_not_exist(
    client: httpx.AsyncClient, migrated_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(health, "log_event", lambda event, **f: events.append((event, f)))
    missing = f"atlas_missing_{uuid.uuid4().hex}"
    missing_url = make_url(migrated_database).set(database=missing)

    with pytest.MonkeyPatch.context() as env:
        env.setenv("ATLAS_DATABASE_URL", missing_url.render_as_string(hide_password=False))
        await dispose_engine()
        get_settings.cache_clear()
        try:
            response = await client.get("/health")
        finally:
            await dispose_engine()
            get_settings.cache_clear()

    assert response.status_code == 503
    assert response.json() == {"status": "degraded", "db": "unavailable"}
    assert missing not in response.text
    assert response.headers["X-Trace-Id"]
    assert [name for name, _ in events] == ["health_db_unavailable"]
    assert "exc_type" in events[0][1]


async def test_health_degraded_on_timeout(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def hang() -> None:
        raise TimeoutError

    monkeypatch.setattr(health, "_ping_database", hang)

    response = await client.get("/health")

    assert response.status_code == 503
    assert response.json() == {"status": "degraded", "db": "unavailable"}


def test_health_timeout_is_short() -> None:
    assert 0 < health.DB_TIMEOUT_SECONDS <= 3


async def test_unknown_route_uses_error_envelope(client: httpx.AsyncClient) -> None:
    response = await client.get("/nope")

    assert response.status_code == 404
    assert response.json()["error"]["trace_id"] == response.headers["X-Trace-Id"]


async def test_lifespan_disposes_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    import api.main as main_module

    calls: list[str] = []

    async def fake_dispose() -> None:
        calls.append("dispose")

    monkeypatch.setattr(main_module, "dispose_engine", fake_dispose)
    app = create_app()
    async with app.router.lifespan_context(app):
        assert calls == []
    assert calls == ["dispose"]
