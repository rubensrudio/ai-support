"""ASGI middleware that binds one trace id per request and returns it as `X-Trace-Id` (CT-2).

It also opens the root `request` span (CT-62, OBS-01) around the whole app call, so every span
of the request (including the ones emitted while a streaming body is produced) is its child and
shares the request trace id. The span records `http.status_code` from `http.response.start`.
"""

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from api.observability.logging import bind_trace_id, new_trace_id, reset_trace_id
from api.observability.tracing import start_span

TRACE_HEADER = "X-Trace-Id"


class TraceIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        trace_id = new_trace_id()
        token = bind_trace_id(trace_id)

        try:
            with start_span(
                "request",
                **{"http.method": scope.get("method"), "url.path": scope.get("path")},
            ) as span:

                async def send_with_header(message: Message) -> None:
                    if message["type"] == "http.response.start":
                        headers = list(message.get("headers", []))
                        headers.append((TRACE_HEADER.lower().encode(), trace_id.encode()))
                        message["headers"] = headers
                        span.set_attribute("http.status_code", int(message["status"]))
                    await send(message)

                try:
                    await self.app(scope, receive, send_with_header)
                finally:
                    route = getattr(scope.get("route"), "path", None)
                    if isinstance(route, str):
                        span.set_attribute("http.route", route)
        finally:
            reset_trace_id(token)
