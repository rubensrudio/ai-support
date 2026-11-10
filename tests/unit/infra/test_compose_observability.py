"""Local Langfuse stack behind the `observability` compose profile (TASK-138, OBS-03, OBS-07).

The tests read the files only; no container is built or started.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT_DIR = Path(__file__).resolve().parents[3]

LANGFUSE_SERVICES = {
    "langfuse-web",
    "langfuse-worker",
    "langfuse-clickhouse",
    "langfuse-redis",
    "langfuse-minio",
    "langfuse-postgres",
}
CORE_SERVICES = {"db", "api", "web"}
# A dotted version (`3.227.0`, `7.4.12-alpine`, `17.11`), never a bare major such as `3`,
# or a dated MinIO release.
PINNED_TAG = r"\d+\.\d+(\.\d+)*(-[0-9A-Za-z.]+)?|RELEASE\.\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z"
DASHBOARD_SECTIONS = ("Cost per conversation", "p95 latency", "Fallback rate")


def _read(relative: str) -> str:
    return (ROOT_DIR / relative).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    result: dict[str, Any] = yaml.safe_load(_read("docker-compose.yml"))
    return result


@pytest.fixture(scope="module")
def services(compose: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = compose["services"]
    return result


@pytest.fixture(scope="module")
def doc() -> str:
    return _read("docs/observability.md")


def _published_host_ips(port: Any) -> list[str]:
    """Host IP of each published port; an empty string means all interfaces."""
    if isinstance(port, dict):
        if "published" not in port:
            return []
        return [str(port.get("host_ip", ""))]
    # `${VAR:-default}` interpolation also contains colons; collapse it before splitting.
    text = re.sub(r"\$\{[^}]*\}", "VAR", str(port))
    parts = text.split(":")
    if len(parts) == 1:
        # Only the container port: Docker publishes it on a random port of every interface.
        return [""]
    if len(parts) == 2:
        return [""]
    return [":".join(parts[:-2])]


def test_compose_has_the_full_langfuse_stack(services: dict[str, Any]) -> None:
    assert LANGFUSE_SERVICES <= set(services)


def test_langfuse_services_only_run_with_the_observability_profile(
    services: dict[str, Any],
) -> None:
    for name in services:
        if "langfuse" in name:
            assert services[name].get("profiles") == ["observability"], name


def test_core_services_have_no_profile(services: dict[str, Any]) -> None:
    for name in CORE_SERVICES:
        assert "profiles" not in services[name], name


def test_core_services_do_not_depend_on_langfuse(services: dict[str, Any]) -> None:
    for name in CORE_SERVICES:
        depends_on = services[name].get("depends_on", {})
        assert not any("langfuse" in dependency for dependency in depends_on), name


def test_langfuse_ports_are_bound_to_loopback_only(services: dict[str, Any]) -> None:
    for name in LANGFUSE_SERVICES:
        for port in services[name].get("ports", []):
            for host_ip in _published_host_ips(port):
                assert host_ip == "127.0.0.1", f"{name} publishes {port!r} beyond loopback"


def test_langfuse_web_is_published_on_local_port_3000(services: dict[str, Any]) -> None:
    assert services["langfuse-web"]["ports"] == ["127.0.0.1:${LANGFUSE_PORT:-3000}:3000"]


def test_langfuse_images_use_fixed_version_tags(services: dict[str, Any]) -> None:
    for name in LANGFUSE_SERVICES:
        image = services[name]["image"]
        tag = image.rsplit("/", 1)[-1].partition(":")[2]
        assert tag, f"{name} image {image!r} has no tag"
        assert tag != "latest" and "$" not in tag, f"{name} image {image!r} is not pinned"
        assert re.fullmatch(PINNED_TAG, tag), (
            f"{name} image {image!r} must pin a full version, not a major tag"
        )


def test_langfuse_stack_uses_its_own_named_volumes(compose: dict[str, Any]) -> None:
    volumes = set(compose["volumes"])
    for name in ("langfuse-postgres", "langfuse-clickhouse", "langfuse-minio", "langfuse-redis"):
        mounts = compose["services"][name].get("volumes", [])
        assert mounts, f"{name} must persist its data"
        for mount in mounts:
            volume = str(mount).split(":", 1)[0]
            assert volume in volumes and volume.startswith("langfuse-"), (name, mount)


def test_langfuse_web_bootstraps_the_local_project_headlessly(services: dict[str, Any]) -> None:
    environment = services["langfuse-web"]["environment"]

    for key in (
        "LANGFUSE_INIT_ORG_ID",
        "LANGFUSE_INIT_PROJECT_ID",
        "LANGFUSE_INIT_USER_EMAIL",
        "LANGFUSE_INIT_USER_PASSWORD",
    ):
        assert environment.get(key), key
    assert "pk-lf-local-dev" in environment["LANGFUSE_INIT_PROJECT_PUBLIC_KEY"]
    assert "sk-lf-local-dev" in environment["LANGFUSE_INIT_PROJECT_SECRET_KEY"]


def test_langfuse_services_wait_for_healthy_backends(services: dict[str, Any]) -> None:
    backends = {"langfuse-postgres", "langfuse-clickhouse", "langfuse-redis", "langfuse-minio"}
    for name in backends:
        assert services[name].get("healthcheck"), name
    for name in ("langfuse-web", "langfuse-worker"):
        depends_on = services[name]["depends_on"]
        assert set(depends_on) == backends, name
        assert all(spec["condition"] == "service_healthy" for spec in depends_on.values())


def test_env_example_documents_the_local_langfuse_keys() -> None:
    env_example = _read(".env.example")

    assert "--profile observability" in env_example
    assert "pk-lf-local-dev" in env_example
    assert "sk-lf-local-dev" in env_example
    assert re.search(r"^LANGFUSE_HOST=", env_example, re.MULTILINE)


def test_doc_has_the_dashboard_sections(doc: str) -> None:
    headings = {line.lstrip("#").strip() for line in doc.splitlines() if line.startswith("#")}

    for section in DASHBOARD_SECTIONS:
        assert section in headings, section


def test_doc_explains_how_to_run_and_restrict_the_stack(doc: str) -> None:
    for fragment in (
        "docker compose --profile observability up -d",
        "langfuse.session.id",
        "atlas.cost_usd",
        "fallback",
        "Langfuse Cloud",
        "Operator",
        "trace_id",
        "LANGFUSE_HOST=http://localhost:3000",
    ):
        assert fragment in doc, fragment
