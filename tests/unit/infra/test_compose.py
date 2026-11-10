"""Docker images and the one-command compose stack (TASK-013, FND-01, DA-18).

The tests read the files only; no container is built or started.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT_DIR = Path(__file__).resolve().parents[3]

EXPECTED_DB_SERVICE: dict[str, Any] = {
    "image": "pgvector/pgvector:pg16",
    "environment": {
        "POSTGRES_USER": "atlas",
        "POSTGRES_PASSWORD": "atlas",
        "POSTGRES_DB": "atlas",
    },
    "ports": ["5432:5432"],
    "volumes": ["atlas-db:/var/lib/postgresql/data"],
    "healthcheck": {
        "test": ["CMD-SHELL", "pg_isready -U atlas -d atlas"],
        "interval": "5s",
        "timeout": "5s",
        "retries": 10,
    },
}


def _read(relative: str) -> str:
    return (ROOT_DIR / relative).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def services() -> dict[str, Any]:
    compose = yaml.safe_load(_read("docker-compose.yml"))
    result: dict[str, Any] = compose["services"]
    return result


def _instructions(dockerfile: str) -> list[str]:
    """Dockerfile instructions with line continuations joined and comments dropped."""
    joined = dockerfile.replace("\\\n", " ")
    return [
        " ".join(line.split())
        for line in joined.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_compose_has_db_api_and_web(services: dict[str, Any]) -> None:
    assert {"db", "api", "web"} <= set(services)


def test_db_service_is_unchanged(services: dict[str, Any]) -> None:
    assert services["db"] == EXPECTED_DB_SERVICE


def test_api_waits_for_healthy_db(services: dict[str, Any]) -> None:
    api = services["api"]

    assert api["depends_on"]["db"]["condition"] == "service_healthy"
    assert api["build"]["dockerfile"] == "Dockerfile"
    assert api["env_file"] == [".env"] or api["env_file"] == ".env"
    assert "8000:8000" in api["ports"]
    database_url = api["environment"]["ATLAS_DATABASE_URL"]
    assert "@db:5432/" in database_url


def test_web_publishes_port_8080_and_depends_on_api(services: dict[str, Any]) -> None:
    web = services["web"]

    assert "8080:8080" in web["ports"]
    assert "api" in web["depends_on"]
    assert web["build"]["context"] == "web"


def test_compose_has_no_langfuse(services: dict[str, Any]) -> None:
    """Langfuse only exists behind the `observability` profile (DA-18, LAC-36)."""
    outside_profile = [
        name
        for name, service in services.items()
        if "observability" not in service.get("profiles", [])
    ]

    assert outside_profile
    assert not any("langfuse" in name for name in outside_profile)
    for name in outside_profile:
        assert not any("langfuse" in dep for dep in services[name].get("depends_on", {})), name


def test_api_dockerfile_uses_uv_and_a_non_root_user() -> None:
    instructions = _instructions(_read("Dockerfile"))

    assert instructions[0].startswith("FROM python:3.12-slim")
    assert any(line.startswith("COPY --from=ghcr.io/astral-sh/uv") for line in instructions), (
        "uv must be copied from the official image"
    )
    assert any("uv sync --frozen --no-dev" in line for line in instructions)
    users = [line for line in instructions if line.startswith("USER ")]
    assert users, "the image must switch to a non-root user"
    assert users[-1].split()[1] not in {"root", "0"}
    assert not any(line.startswith(("ENV ANTHROPIC", "ENV OPENAI")) for line in instructions)
    assert not any("COPY .env" in line for line in instructions)


def test_dockerignore_excludes_local_state_and_secrets() -> None:
    entries = {line.strip().rstrip("/") for line in _read(".dockerignore").splitlines()}

    for entry in (".venv", "web/node_modules", ".git", ".env", ".orchestrate", "reports"):
        assert entry in entries


def test_api_entrypoint_migrates_then_serves() -> None:
    script = _read("docker/api-entrypoint.sh")

    assert script.startswith("#!/")
    assert "set -e" in script
    migrate = script.index("alembic upgrade head")
    serve = script.index("uvicorn api.main:app --host 0.0.0.0 --port 8000")
    assert migrate < serve


def test_web_dockerfile_builds_with_node_and_serves_with_nginx() -> None:
    instructions = _instructions(_read("web/Dockerfile"))

    froms = [line for line in instructions if line.startswith("FROM ")]
    assert froms[0].startswith("FROM node:24")
    assert froms[-1].startswith("FROM nginx")
    assert any("npm ci" in line for line in instructions)
    assert any("ng build" in line for line in instructions)
    assert any("dist/web/browser" in line for line in instructions)
    assert "EXPOSE 8080" in instructions


def test_nginx_proxies_api_and_falls_back_to_the_spa() -> None:
    conf = " ".join(_read("web/nginx.conf").split())

    assert "listen 8080;" in conf
    assert "location /api/ {" in conf
    # Trailing slash on proxy_pass strips the /api prefix.
    assert "proxy_pass http://api:8000/;" in conf
    assert "try_files $uri $uri/ /index.html;" in conf
