"""The API lifespan never lets a slow telemetry flush hold the shutdown (OBS-93)."""

import time

import pytest

import api.main as main


async def test_lifespan_shutdown_is_bounded_when_tracing_flush_hangs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def noop() -> None:
        return None

    monkeypatch.setattr(main, "shutdown_tracing", lambda: time.sleep(1.0))
    monkeypatch.setattr(main, "SHUTDOWN_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(main, "close_embedder", noop)
    monkeypatch.setattr(main, "close_langfuse_api", noop)
    monkeypatch.setattr(main, "dispose_engine", noop)

    started = time.perf_counter()
    async with main.lifespan(main.app):
        pass
    assert time.perf_counter() - started < 0.5


async def test_lifespan_calls_tracing_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def noop() -> None:
        return None

    monkeypatch.setattr(main, "shutdown_tracing", lambda: calls.append("tracing"))
    monkeypatch.setattr(main, "close_embedder", noop)
    monkeypatch.setattr(main, "close_langfuse_api", noop)
    monkeypatch.setattr(main, "dispose_engine", noop)

    async with main.lifespan(main.app):
        pass
    assert calls == ["tracing"]


def test_shutdown_tracing_returns_after_its_timeout_when_export_hangs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import api.observability.tracing as tracing

    monkeypatch.setattr(tracing, "_shutdown_remote", lambda: time.sleep(2.0))
    started = time.perf_counter()
    tracing.shutdown_tracing(timeout_seconds=0.1)
    assert time.perf_counter() - started < 0.5
