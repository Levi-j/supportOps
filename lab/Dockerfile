FROM ghcr.io/astral-sh/uv:0.12.23 AS uv

FROM python:3.13-slim AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY lab/pyproject.toml lab/pyproject.toml
RUN uv sync --locked --no-dev --package supportops-lab --no-install-workspace

COPY lab/src lab/src
RUN uv sync --locked --no-dev --package supportops-lab --no-editable

FROM python:3.13-slim
RUN groupadd --gid 10001 billing \
    && useradd --uid 10001 --gid billing --no-create-home --shell /usr/sbin/nologin billing
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1
USER billing
EXPOSE 8000
ENTRYPOINT ["uvicorn", "--factory", "billing_api.main:create_app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
