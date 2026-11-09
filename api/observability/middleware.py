"""ASGI middleware that binds one trace id per request and returns it as `X-Trace-Id` (CT-2)."""

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from api.observability.logging import bind_trace_id, new_trace_id, reset_trace_id

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

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((TRACE_HEADER.lower().encode(), trace_id.encode()))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        finally:
            reset_trace_id(token)
