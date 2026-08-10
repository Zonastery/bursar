# syntax=docker/dockerfile:1

ARG POSTGRES_IMAGE=postgres:17.10-bookworm@sha256:9b18b78397054fce88a9552e9d5a3ad5bb7fd258c5b3cc1c5028e46373d6ea8f
FROM ${POSTGRES_IMAGE} AS pg-jsonschema

ARG PG_JSONSCHEMA_VERSION=0.3.4

SHELL ["/bin/bash", "-euxo", "pipefail", "-c"]

RUN architecture="$(dpkg --print-architecture)"; \
    case "${PG_MAJOR}-${architecture}" in \
        16-amd64) pg_jsonschema_sha256="0135e1df71032880ff52a5e51a787a08cf724e5c3a4b455bba9a22ecd368fb68" ;; \
        16-arm64) pg_jsonschema_sha256="28496d630b5c99415e77680d6d5a522f67be43170b30fbef376caeab9966dd74" ;; \
        17-amd64) pg_jsonschema_sha256="4c339c178a7396b4076fb1eb5ffe3430bafd3d5f92fb6c917ad49860237db6c9" ;; \
        17-arm64) pg_jsonschema_sha256="312f3b6b7b0e9f4d55c1ecd7b16c774da62482869e64745594b8c0f8af753414" ;; \
        *) echo "unsupported PostgreSQL/architecture pair: ${PG_MAJOR}-${architecture}" >&2; exit 1 ;; \
    esac; \
    apt-get update; \
    apt-get install --yes --no-install-recommends \
        ca-certificates \
        curl; \
    package="pg_jsonschema-v${PG_JSONSCHEMA_VERSION}-pg${PG_MAJOR}-${architecture}-linux-gnu.deb"; \
    curl --fail --location --proto '=https' --tlsv1.2 \
        --connect-timeout 15 --max-time 300 \
        --retry 2 --retry-all-errors --retry-max-time 600 \
        --output /tmp/pg_jsonschema.deb \
        "https://github.com/supabase/pg_jsonschema/releases/download/v${PG_JSONSCHEMA_VERSION}/${package}"; \
    echo "${pg_jsonschema_sha256}  /tmp/pg_jsonschema.deb" | sha256sum --check --strict; \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/*

FROM ${POSTGRES_IMAGE}

LABEL org.opencontainers.image.title="Bursar PostgreSQL test database" \
    org.opencontainers.image.description="Upstream PostgreSQL with Bursar's required extensions" \
    org.opencontainers.image.source="https://github.com/Zonastery/bursar"

SHELL ["/bin/bash", "-euxo", "pipefail", "-c"]

# Copy only the checksum-verified package into the runtime stage. Standard
# Dockerfile instructions keep this image buildable through Testcontainers'
# Docker API builder, which does not enable BuildKit-only RUN mounts.
COPY --from=pg-jsonschema /tmp/pg_jsonschema.deb /tmp/pg_jsonschema.deb

RUN apt-get update; \
    apt-get install --yes --no-install-recommends \
        "postgresql-${PG_MAJOR}-partman" \
        /tmp/pg_jsonschema.deb; \
    rm -f /tmp/pg_jsonschema.deb; \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/*
