"""Error catalog and the standard error envelope (CT-3, plan 8.3, CHAT-92).

Every error response has the body `{"error": {"code", "message", "trace_id"}}`, where
`trace_id` is the one returned in `X-Trace-Id`. No stack trace and no exception text ever
reach the body: an unhandled exception becomes `INTERNAL_ERROR` and only its type is logged.
"""

from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware import Middleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from api.messages import Lang, MessageKey, message_template, user_message
from api.observability.logging import get_trace_id, log_event

DEFAULT_LANG: Lang = "en"


class ErrorCode(StrEnum):
    VALIDATION_ERROR = "VALIDATION_ERROR"
    MESSAGE_TOO_LONG = "MESSAGE_TOO_LONG"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    CONVERSATION_NOT_FOUND = "CONVERSATION_NOT_FOUND"
    CONFIRMATION_NOT_FOUND = "CONFIRMATION_NOT_FOUND"
    CONFIRMATION_NOT_PENDING = "CONFIRMATION_NOT_PENDING"
    MESSAGE_NOT_FOUND = "MESSAGE_NOT_FOUND"
    RATE_LIMITED = "RATE_LIMITED"
    DAILY_CAP_REACHED = "DAILY_CAP_REACHED"
    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
    TRIAGE_UNAVAILABLE = "TRIAGE_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    STREAM_ERROR = "STREAM_ERROR"
    # Framework-level errors (unknown route / method), so they also use the envelope.
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"

    @property
    def http_status(self) -> int | None:
        """Catalog HTTP status; `None` for `STREAM_ERROR`, which is an SSE event only."""
        return _HTTP_STATUS.get(self)


_HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.VALIDATION_ERROR: 422,
    ErrorCode.MESSAGE_TOO_LONG: 422,
    ErrorCode.AUTH_REQUIRED: 401,
    ErrorCode.INVALID_CREDENTIALS: 401,
    ErrorCode.CONVERSATION_NOT_FOUND: 404,
    ErrorCode.CONFIRMATION_NOT_FOUND: 404,
    ErrorCode.CONFIRMATION_NOT_PENDING: 409,
    ErrorCode.MESSAGE_NOT_FOUND: 404,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.DAILY_CAP_REACHED: 429,
    ErrorCode.LLM_UNAVAILABLE: 502,
    ErrorCode.TRIAGE_UNAVAILABLE: 503,
    ErrorCode.INTERNAL_ERROR: 500,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.METHOD_NOT_ALLOWED: 405,
}

_FRAMEWORK_STATUS_CODES: dict[int, ErrorCode] = {
    401: ErrorCode.AUTH_REQUIRED,
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.METHOD_NOT_ALLOWED,
    422: ErrorCode.VALIDATION_ERROR,
    429: ErrorCode.RATE_LIMITED,
}


class AtlasError(Exception):
    """Typed error returned to the client with the standard envelope."""

    def __init__(
        self, code: ErrorCode, http_status: int, params: dict[str, object] | None = None
    ) -> None:
        super().__init__(code.value)
        self.code = code
        self.http_status = http_status
        self.params: dict[str, object] = dict(params or {})


def error_message(code: ErrorCode, lang: Lang = DEFAULT_LANG, **params: object) -> str:
    """User message for `code`; falls back to the raw text if a parameter is missing."""
    key = MessageKey(code.value)
    try:
        return user_message(key, lang, **params)
    except (KeyError, IndexError, ValueError):
        log_event("error_message_params_missing", code=code.value)
        return message_template(key, lang)


def error_body(code: ErrorCode, message: str) -> dict[str, dict[str, str]]:
    return {"error": {"code": code.value, "message": message, "trace_id": get_trace_id()}}


def error_response(
    code: ErrorCode,
    http_status: int,
    params: dict[str, object] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    message = error_message(code, DEFAULT_LANG, **(params or {}))
    return JSONResponse(error_body(code, message), status_code=http_status, headers=headers)


async def _atlas_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AtlasError)  # noqa: S101 - registered only for AtlasError
    return error_response(exc.code, exc.http_status, exc.params)


async def _validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    return error_response(ErrorCode.VALIDATION_ERROR, 422)


async def _http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)  # noqa: S101 - registered only for it
    status = exc.status_code
    default = ErrorCode.INTERNAL_ERROR if status >= 500 else ErrorCode.VALIDATION_ERROR
    code = _FRAMEWORK_STATUS_CODES.get(status, default)
    return error_response(code, status, headers=exc.headers)


def _log_unhandled(exc: BaseException) -> None:
    log_event("unhandled_error", exc_type=type(exc).__name__)


async def _unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Last-resort handler (ServerErrorMiddleware) for errors raised outside the app."""
    _log_unhandled(exc)
    return error_response(ErrorCode.INTERNAL_ERROR, 500)


class UnhandledErrorMiddleware:
    """Turns an unhandled exception into a 500 envelope inside the trace id scope.

    It runs inside every user middleware (so `X-Trace-Id` is still added and matches the
    body). If the response already started (e.g. a stream), the error is re-raised.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = False

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception as exc:
            _log_unhandled(exc)
            if started:
                raise
            response = error_response(ErrorCode.INTERNAL_ERROR, 500)
            await response(scope, receive, send)


def install_error_handlers(app: FastAPI) -> None:
    """Register the envelope handlers. Call before the app serves its first request."""
    handlers: dict[Any, Any] = {
        AtlasError: _atlas_error_handler,
        RequestValidationError: _validation_error_handler,
        StarletteHTTPException: _http_exception_handler,
        Exception: _unhandled_error_handler,
    }
    for exc_class, handler in handlers.items():
        app.add_exception_handler(exc_class, handler)
    # Appended (not `add_middleware`, which prepends) so it is always the innermost user
    # middleware, inside `TraceIdMiddleware` whatever the registration order.
    installed = {getattr(m.cls, "__name__", None) for m in app.user_middleware}
    if UnhandledErrorMiddleware.__name__ not in installed:
        app.user_middleware.append(Middleware(UnhandledErrorMiddleware))
