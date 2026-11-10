"""Render blueprint of the public demo (TASK-149, DEMO-04, DEMO-05, DA-27).

The tests read `render.yaml` only; nothing is deployed and no network call is made.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT_DIR = Path(__file__).resolve().parents[3]
BLUEPRINT = ROOT_DIR / "render.yaml"

API_URL = "https://atlas-api.onrender.com"
SECRETS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "LANGFUSE_HOST",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "ATLAS_OPERATOR_API_KEY",
    "ATLAS_PERSONA_PASSWORD",
)
FIXED_VALUES = {
    "CHAOS_RATE": "0",
    "ATLAS_APP_ENV": "demo",
    "ATLAS_MAINTENANCE_ENABLED": "true",
    "ATLAS_COOKIE_SECURE": "true",
    "ATLAS_LLM_LEDGER_ENABLED": "true",
    "ATLAS_RATE_LIMIT_PER_HOUR": "20",
    "ATLAS_DAILY_SPEND_CAP_USD": "2.00",
    "ATLAS_RETENTION_DAYS": "7",
    "ATLAS_LLM_PROVIDER": "anthropic",
    "PORT": "8000",
    "ATLAS_INGEST_ON_BOOT": "true",
    "ATLAS_AGENT_MAX_COST_USD": "0.25",
}
# Prefixes of real credentials: Anthropic/OpenAI (`sk-`), Langfuse (`pk-lf-`, `sk-lf-`),
# GitHub tokens and AWS access keys.
KEY_LIKE = re.compile(r"(sk-|pk-lf-|sk-lf-|ghp_|github_pat_|AKIA)[A-Za-z0-9_-]{4,}")


@pytest.fixture(scope="module")
def blueprint() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(BLUEPRINT.read_text(encoding="utf-8"))
    return data


def _service(blueprint: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [s for s in blueprint["services"] if s["name"] == name]
    assert len(matches) == 1, name
    service: dict[str, Any] = matches[0]
    return service


def _env_vars(blueprint: dict[str, Any]) -> dict[str, dict[str, Any]]:
    env = _service(blueprint, "atlas-api")["envVars"]
    keys = [item["key"] for item in env]
    assert len(keys) == len(set(keys)), "duplicated envVars key"
    return {item["key"]: item for item in env}


def test_api_is_a_docker_web_service_built_from_the_dockerfile(blueprint: dict[str, Any]) -> None:
    api = _service(blueprint, "atlas-api")

    assert api["type"] == "web"
    assert api["runtime"] == "docker"
    assert api["dockerfilePath"] == "./Dockerfile"
    assert (ROOT_DIR / api["dockerfilePath"]).is_file()
    assert api["plan"] in {"free", "starter"}


def test_corpus_is_ingested_on_boot_with_the_openai_embedder(blueprint: dict[str, Any]) -> None:
    # Render free has no shell: docker/api-entrypoint.sh ingests the corpus when this is "true".
    env = _env_vars(blueprint)

    assert env["ATLAS_INGEST_ON_BOOT"] == {"key": "ATLAS_INGEST_ON_BOOT", "value": "true"}
    assert env["ATLAS_EMBEDDING_PROVIDER"]["value"] == "openai"
    assert env["OPENAI_API_KEY"]["sync"] is False


def test_api_health_check_path_is_health(blueprint: dict[str, Any]) -> None:
    assert _service(blueprint, "atlas-api")["healthCheckPath"] == "/health"


def test_chaos_is_off_in_the_public_demo(blueprint: dict[str, Any]) -> None:
    # DEMO-05: a quoted "0", never a number nor a secret the Operator could change.
    assert _env_vars(blueprint)["CHAOS_RATE"] == {"key": "CHAOS_RATE", "value": "0"}


@pytest.mark.parametrize(("key", "value"), sorted(FIXED_VALUES.items()))
def test_demo_settings_have_fixed_string_values(
    blueprint: dict[str, Any], key: str, value: str
) -> None:
    assert _env_vars(blueprint)[key]["value"] == value


@pytest.mark.parametrize("key", SECRETS)
def test_secret_is_prompted_and_has_no_value(blueprint: dict[str, Any], key: str) -> None:
    item = _env_vars(blueprint)[key]

    assert item["sync"] is False
    assert "value" not in item
    assert "generateValue" not in item


def test_database_url_comes_from_the_managed_database(blueprint: dict[str, Any]) -> None:
    item = _env_vars(blueprint)["ATLAS_DATABASE_URL"]
    database_names = {db["name"] for db in blueprint["databases"]}

    assert "value" not in item
    assert item["fromDatabase"] == {"name": "atlas-db", "property": "connectionString"}
    assert "atlas-db" in database_names


def test_no_literal_value_looks_like_a_key() -> None:
    text = BLUEPRINT.read_text(encoding="utf-8")

    assert not KEY_LIKE.search(text)


def test_every_literal_value_is_a_non_secret_string(blueprint: dict[str, Any]) -> None:
    for key, item in _env_vars(blueprint).items():
        if "value" in item:
            assert isinstance(item["value"], str), key
            assert key not in SECRETS, key


def test_web_is_a_static_angular_build(blueprint: dict[str, Any]) -> None:
    web = _service(blueprint, "atlas-web")

    assert web["runtime"] == "static"
    assert web["rootDir"] == "web"
    assert "ng build" in web["buildCommand"]
    # `@angular/build:application` writes the browser bundle to dist/<project>/browser.
    assert web["staticPublishPath"] == "./dist/web/browser"


def test_web_rewrites_api_calls_to_the_api_and_falls_back_to_the_spa(
    blueprint: dict[str, Any],
) -> None:
    routes = _service(blueprint, "atlas-web")["routes"]

    assert routes[0] == {"type": "rewrite", "source": "/api/*", "destination": f"{API_URL}/*"}
    assert routes[-1] == {"type": "rewrite", "source": "/*", "destination": "/index.html"}


def test_database_is_managed_postgres(blueprint: dict[str, Any]) -> None:
    (database,) = blueprint["databases"]

    assert database["name"] == "atlas-db"
    assert database["postgresMajorVersion"] == "16"


def test_header_says_the_operator_deploys() -> None:
    header = BLUEPRINT.read_text(encoding="utf-8").splitlines()[0]

    assert header.startswith("#")
    assert "Operator" in header
