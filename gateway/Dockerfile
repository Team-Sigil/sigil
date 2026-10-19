# Gateway image. The same image runs the gateway, its migrations and the mock crypto layer.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.1 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first, so code changes don't reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY alembic.ini ./
COPY src ./src
COPY mock_crypto ./mock_crypto
RUN uv sync --frozen --no-dev

RUN useradd --uid 10001 --no-create-home gateway
USER gateway

EXPOSE 8000
CMD ["python", "-m", "gateway"]
