"""Health check endpoint (FND-01): `SELECT 1` on the database with a short timeout."""

import asyncio

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from api.db.engine import get_engine
from api.observability.logging import log_event

DB_TIMEOUT_SECONDS = 3.0

router = APIRouter(tags=["health"])


async def _ping_database() -> None:
    async with get_engine().connect() as connection:
        await connection.execute(text("SELECT 1"))


@router.get("/health")
async def health() -> JSONResponse:
    """200 when the database answers; 503 (no exception detail in the body) otherwise."""
    try:
        await asyncio.wait_for(_ping_database(), timeout=DB_TIMEOUT_SECONDS)
    except Exception as exc:
        log_event("health_db_unavailable", exc_type=type(exc).__name__)
        return JSONResponse({"status": "degraded", "db": "unavailable"}, status_code=503)
    return JSONResponse({"status": "ok", "db": "ok"})
