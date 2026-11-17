# syntax=docker/dockerfile:1

FROM python:3.15.2-slim-bookworm@sha256:96599df822d0e1c6bb700014a05c7f3c8cabc6e0f7ff3c4f9f69cee033b4b5d9 AS python-base

# Pin uv by version and multi-platform manifest digest. It is build-only.
FROM python-base AS build

WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.13.8@sha256:5cc1805e2426484abca4ad46ea783bab281fdb225fd82d8ce6d66a81254bac85 /uv /usr/local/bin/uv
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
