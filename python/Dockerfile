# syntax=docker/dockerfile:1
# ── Build stage ──────────────────────────────────────────────────────────────
# Pin the patch release and manifest digest for reproducible multi-platform
# builds. Build tooling stays in this disposable stage.
FROM python:3.12.13-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b AS build

WORKDIR /app

# Only the files needed to build the wheel. A .dockerignore keeps .env, .venv,
# tests, dist and .git out of the build context entirely, so no secret or build
# artifact is ever baked into a layer.
COPY pyproject.toml README.md ./
COPY src ./src

# Build a wheel and install it (plus the Postgres extra) into an isolated prefix
# that the runtime stage copies in. Keeps build tooling out of the final image.
RUN --mount=type=cache,id=zonastery-bursar-pip,target=/root/.cache/pip,sharing=locked \
    python -m pip install --disable-pip-version-check build \
    && python -m build --wheel --outdir /tmp/dist \
    && WHEEL="$(ls /tmp/dist/*.whl)" \
    && python -m pip install --disable-pip-version-check --prefix=/install "${WHEEL}[postgres]"

# ── Runtime stage ────────────────────────────────────────────────────────────
FROM python:3.12.13-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Run as a non-root user (H8).
RUN useradd --create-home --uid 10001 bursar
COPY --from=build /install /usr/local

USER bursar
WORKDIR /home/bursar

# `bursar migrate` / `bursar pricing …` read secrets from the environment
# (DATABASE_URL, SUPABASE_URL, SUPABASE_SECRET_KEY) — never bake them in.
ENTRYPOINT ["bursar"]
CMD ["--help"]
