"""FastAPI application factory (FND-01, FND-03). `app` is the uvicorn entry point."""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from api.auth.routes import auth_router
from api.chat.routes import chat_router
from api.chat.service import close_embedder
from api.chat.stream_routes import stream_router
from api.config import get_settings
from api.db.engine import dispose_engine
from api.errors import install_error_handlers
from api.feedback.routes import close_langfuse_api, feedback_router
from api.health import router as health_router
from api.maintenance.scheduler import start_scheduler
from api.observability.logging import configure_logging
from api.observability.middleware import TraceIdMiddleware
from api.observability.tracing import init_tracing, shutdown_tracing
from api.triage.routes import triage_router

SHUTDOWN_TIMEOUT_SECONDS = 5.0


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    maintenance = start_scheduler(app) if get_settings().maintenance_enabled else None
    try:
        yield
    finally:
        if maintenance is not None:
            maintenance.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await maintenance
        await close_embedder()
        await close_langfuse_api()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.to_thread(shutdown_tracing), SHUTDOWN_TIMEOUT_SECONDS)
        await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings)
    init_tracing(settings)
    app = FastAPI(title="Atlas Support API", lifespan=lifespan)
    app.add_middleware(TraceIdMiddleware)
    install_error_handlers(app)
    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(chat_router)
    app.include_router(stream_router)
    app.include_router(triage_router)
    app.include_router(feedback_router)
    return app


app = create_app()
