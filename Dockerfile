FROM python:3.12-slim AS base
COPY --from=ghcr.io/astral-sh/uv:0.10.8 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./
RUN uv sync --frozen --no-dev

RUN useradd --uid 10001 app
USER app
ENV PATH="/app/.venv/bin:$PATH"
CMD ["uvicorn", "recovery.api.app:app", "--host", "0.0.0.0", "--port", "8080"]
