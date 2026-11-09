"""FastAPI application factory (FND-01, FND-03). `app` is the uvicorn entry point."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from api.chat.routes import chat_router
from api.config import get_settings
from api.db.engine import dispose_engine
from api.errors import install_error_handlers
from api.health import router as health_router
from api.observability.logging import configure_logging
from api.observability.middleware import TraceIdMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    try:
        yield
    finally:
        await dispose_engine()


def create_app() -> FastAPI:
    configure_logging(get_settings())
    app = FastAPI(title="Atlas Support API", lifespan=lifespan)
    app.add_middleware(TraceIdMiddleware)
    install_error_handlers(app)
    app.include_router(health_router)
    app.include_router(chat_router)
    return app


app = create_app()
