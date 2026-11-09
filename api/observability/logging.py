"""Structured JSON logging with a per-request trace id (CT-2, FND-03, DA-11).

`trace_id` lives in a `contextvars.ContextVar`, so every log line emitted while handling a
request carries the same id. A filter replaces any configured secret value with `***`
before the record is formatted (FND-90).
"""

import contextvars
import json
import logging
import sys
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from api.config import Settings

LOGGER_NAME = "atlas"
REDACTED = "***"

_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "atlas_trace_id", default=None
)
_secret_values: tuple[str, ...] = ()

_RESERVED = frozenset(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}


def new_trace_id() -> str:
    return uuid.uuid4().hex


def get_trace_id() -> str:
    """Current trace id; a fresh one is bound when called outside any request."""
    current = _trace_id.get()
    if current is None:
        current = new_trace_id()
        _trace_id.set(current)
    return current


def bind_trace_id(trace_id: str) -> contextvars.Token[str | None]:
    return _trace_id.set(trace_id)


def reset_trace_id(token: contextvars.Token[str | None]) -> None:
    _trace_id.reset(token)


def _redact(value: Any, secrets: Iterable[str]) -> Any:
    if isinstance(value, str):
        for secret in secrets:
            if secret and secret in value:
                value = value.replace(secret, REDACTED)
        return value
    if isinstance(value, dict):
        return {k: _redact(v, secrets) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_redact(v, secrets) for v in value]
    return value


class SecretRedactionFilter(logging.Filter):
    """Replaces configured secret values in the message, args and extra fields."""

    def filter(self, record: logging.LogRecord) -> bool:
        secrets = _secret_values
        if not secrets:
            return True
        record.msg = _redact(record.getMessage(), secrets)
        record.args = None
        for key, value in list(vars(record).items()):
            if key not in _RESERVED:
                setattr(record, key, _redact(value, secrets))
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", None) or record.getMessage(),
            "trace_id": getattr(record, "trace_id", None) or _trace_id.get(),
        }
        for key, value in vars(record).items():
            if key not in _RESERVED and key not in payload:
                payload[key] = value
        if record.exc_info:
            payload["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(settings: Settings) -> None:
    """Install the JSON handler on the `atlas` logger and register secrets to redact."""
    global _secret_values
    _secret_values = tuple(settings.secret_values())
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(settings.log_level.upper())
    logger.propagate = False
    for handler in list(logger.handlers):
        if getattr(handler, "_atlas_json", False):
            logger.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(SecretRedactionFilter())
    handler._atlas_json = True  # type: ignore[attr-defined]
    logger.addHandler(handler)


def register_secrets(values: Iterable[str]) -> None:
    """Add secret values to redact (used by tests and by late-loaded credentials)."""
    global _secret_values
    _secret_values = tuple(dict.fromkeys([*_secret_values, *(v for v in values if v)]))


def log_event(event: str, **fields: object) -> None:
    """Emit one structured event on the `atlas` logger with the current trace id."""
    extra: dict[str, object] = {"event": event, "trace_id": get_trace_id()}
    for key, value in fields.items():
        if key in _RESERVED or key in extra:
            extra[f"field_{key}"] = value
        else:
            extra[key] = value
    logging.getLogger(LOGGER_NAME).info(event, extra=extra)
