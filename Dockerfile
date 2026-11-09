# Atlas API image. Secrets are never baked in: they come from `.env` at runtime (compose
# `env_file`). Build: `docker compose build api`.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.15 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

# Dependencies first, so source edits do not invalidate this layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev \
    && chmod 0755 docker/api-entrypoint.sh \
    && groupadd --system --gid 10001 atlas \
    && useradd --system --uid 10001 --gid atlas --home-dir /app --no-create-home atlas

USER atlas

EXPOSE 8000

ENTRYPOINT ["/app/docker/api-entrypoint.sh"]
