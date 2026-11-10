#!/bin/sh
# Apply database migrations, seed the personas, optionally ingest the corpus, then serve the API
# (DA-18, DA-27).
set -eu

alembic upgrade head
python -m api.seed
# Opt-in corpus ingestion for hosts without a shell (Render, DA-27). It is idempotent (unchanged
# documents keep their chunks); a failed document never blocks the API from starting.
if [ "${ATLAS_INGEST_ON_BOOT:-false}" = "true" ]; then
    python -m ingest run || echo "ingest: some documents failed, see the logs" >&2
fi
exec uvicorn api.main:app --host 0.0.0.0 --port 8000
