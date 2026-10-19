FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

RUN useradd --create-home signer && mkdir -p /data && chown signer /data
USER signer
ENV SIGNER_DB_PATH=/data/signer.sqlite3 SIGNER_HOST=0.0.0.0
EXPOSE 8300
CMD ["python", "-m", "signer"]
