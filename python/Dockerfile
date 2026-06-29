# syntax=docker/dockerfile:1
# ── Build stage ──────────────────────────────────────────────────────────────
# Pinned base image (H8): a specific patch tag rather than the floating
# `3.12-slim`, so a moved tag can't silently change the base. For an even
# stronger guarantee, append a digest, e.g.:
#   FROM python:3.12.7-slim-bookworm@sha256:<digest> AS build
# (run `docker buildx imagetools inspect python:3.12.7-slim-bookworm` to get it).
FROM python:3.12.7-slim-bookworm AS build

WORKDIR /app

# Only the files needed to build the wheel. A .dockerignore keeps .env, .venv,
# tests, dist and .git out of the build context entirely, so no secret or build
# artifact is ever baked into a layer.
COPY pyproject.toml README.md ./
COPY src ./src

# Build a wheel and install it (plus the supabase extra) into an isolated prefix
# that the runtime stage copies in. Keeps build tooling out of the final image.
RUN pip install --no-cache-dir build \
    && python -m build --wheel --outdir /tmp/dist \
    && WHEEL="$(ls /tmp/dist/*.whl)" \
    && pip install --no-cache-dir --prefix=/install "${WHEEL}[supabase]"

# ── Runtime stage ────────────────────────────────────────────────────────────
FROM python:3.12.7-slim-bookworm AS runtime

# Run as a non-root user (H8).
RUN useradd --create-home --uid 10001 ducto
COPY --from=build /install /usr/local

USER ducto
WORKDIR /home/ducto

# `ducto migrate` / `ducto pricing …` read secrets from the environment
# (DATABASE_URL, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY) — never bake them in.
ENTRYPOINT ["ducto"]
CMD ["--help"]
