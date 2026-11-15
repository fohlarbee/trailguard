# The TrailGuard service: ingest API, tracer, scorer and agent.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY simulator simulator
COPY connector connector
COPY scorer scorer
COPY core core
COPY agent agent
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
CMD ["uvicorn", "core.api:app", "--host", "0.0.0.0", "--port", "8000"]
