# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv:0.13.9 AS uv

FROM python:3.13-slim-bookworm AS builder
COPY --from=uv /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
WORKDIR /app
COPY uv.lock pyproject.toml ./
COPY packages ./packages
COPY sim ./sim
COPY README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

FROM python:3.13-slim-bookworm AS runtime
RUN useradd --create-home --uid 10001 eye
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY config ./config
RUN mkdir -p /app/data/lake /app/packages/selleros/data \
    && chown -R eye:eye /app/data /app/packages/selleros/data
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER eye
CMD ["eye-core"]
