ARG POSTGRES_VERSION=17.10
FROM postgres:${POSTGRES_VERSION}-bookworm

ARG PG_JSONSCHEMA_VERSION=0.3.4

LABEL org.opencontainers.image.title="Bursar PostgreSQL test database"
LABEL org.opencontainers.image.description="Upstream PostgreSQL with Bursar's required extensions"
LABEL org.opencontainers.image.source="https://github.com/Zonastery/bursar"

SHELL ["/bin/bash", "-euxo", "pipefail", "-c"]

RUN architecture="$(dpkg --print-architecture)"; \
    case "${architecture}" in \
        amd64) pg_jsonschema_sha256="4c339c178a7396b4076fb1eb5ffe3430bafd3d5f92fb6c917ad49860237db6c9" ;; \
        arm64) pg_jsonschema_sha256="312f3b6b7b0e9f4d55c1ecd7b16c774da62482869e64745594b8c0f8af753414" ;; \
        *) echo "unsupported architecture: ${architecture}" >&2; exit 1 ;; \
    esac; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        "postgresql-${PG_MAJOR}-partman"; \
    package="pg_jsonschema-v${PG_JSONSCHEMA_VERSION}-pg${PG_MAJOR}-${architecture}-linux-gnu.deb"; \
    curl --fail --location --proto '=https' --tlsv1.2 \
        --connect-timeout 15 --max-time 300 \
        --retry 2 --retry-all-errors --retry-max-time 600 \
        --output "/tmp/${package}" \
        "https://github.com/supabase/pg_jsonschema/releases/download/v${PG_JSONSCHEMA_VERSION}/${package}"; \
    echo "${pg_jsonschema_sha256}  /tmp/${package}" | sha256sum --check --strict; \
    apt-get install -y --no-install-recommends "/tmp/${package}"; \
    rm -f "/tmp/${package}"; \
    rm -rf /var/lib/apt/lists/*
