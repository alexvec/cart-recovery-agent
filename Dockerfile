# Build stage: uv resolves and installs; it never ships in the runtime image.
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.10.8 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

# Runtime stage: Python, the virtualenv and migrations only.
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH"
COPY --from=build /app/.venv /app/.venv
COPY migrations ./migrations
COPY alembic.ini ./

RUN useradd --uid 10001 app
USER app
CMD ["uvicorn", "recovery.api.app:app", "--host", "0.0.0.0", "--port", "8080"]
