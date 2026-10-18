# One image for both services: the agent service (default) and the mock platform
# (override the command with `python -m mock_platform.server`).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first, so code changes don't reinstall them.
COPY pyproject.toml ./
COPY src ./src
RUN pip install .

# mock_platform is not part of the installed package; it runs from /app.
COPY mock_platform ./mock_platform

RUN useradd --system --no-create-home agent
USER agent

ENV AGENT_HOST=0.0.0.0 \
    AGENT_PORT=8100
EXPOSE 8100

CMD ["python", "-m", "agent_layer"]
