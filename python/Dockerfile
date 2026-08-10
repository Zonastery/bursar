# syntax=docker/dockerfile:1

FROM python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2 AS python-base

# Pin uv by version and multi-platform manifest digest. It is build-only.
FROM python-base AS build

WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.11.16@sha256:440fd6477af86a2f1b38080c539f1672cd22acb1b1a47e321dba5158ab08864d /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

# Install exactly the hashed production dependency set from uv.lock. Installing
# the project non-editably builds a wheel without retaining build tooling.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,id=zonastery-bursar-uv,target=/root/.cache/uv,sharing=locked \
    uv sync --frozen --no-dev --extra postgres --no-editable

FROM python-base AS runtime

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

LABEL org.opencontainers.image.title="Bursar CLI" \
    org.opencontainers.image.description="Bursar database migration and catalog management CLI" \
    org.opencontainers.image.source="https://github.com/Zonastery/bursar" \
    org.opencontainers.image.licenses="AGPL-3.0-only"

RUN useradd --create-home --uid 10001 bursar
COPY --from=build /app/.venv /app/.venv

USER bursar
WORKDIR /home/bursar

# `migrate` reads BURSAR_MIGRATION_DATABASE_URL; tenant commands read
# BURSAR_OPERATOR_DATABASE_URL; database-backed config commands read
# DATABASE_URL and BURSAR_TENANT_ID. Never bake any of them into the image.
ENTRYPOINT ["bursar"]
CMD ["--help"]
