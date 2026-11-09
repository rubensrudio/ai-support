#!/bin/sh
# Apply database migrations, then serve the API (DA-18).
set -eu

alembic upgrade head
python -m api.seed
exec uvicorn api.main:app --host 0.0.0.0 --port 8000
