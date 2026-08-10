"""Fixtures for integration tests — one canonical Postgres source.

The ``pg_database_url`` fixture resolves a connection string to a **real
PostgreSQL 17 with pg_partman 5 and pg_jsonschema 0.3** from a single,
consistent mechanism
(resolution order):

1. ``DATABASE_URL`` — an explicitly supplied database shared by the Python
   and JavaScript harnesses. Because test isolation truncates every Bursar
   table, this path also requires ``BURSAR_ALLOW_DATABASE_RESET=1``::

       BURSAR_ALLOW_DATABASE_RESET=1 \
       DATABASE_URL=postgres://bursar:bursar@localhost:5432/bursar_test uv run pytest

2. **testcontainers** — a disposable, provider-neutral PostgreSQL 17 image
   with pg_partman 5 and pg_jsonschema 0.3,
   started once per test session (requires only a reachable Docker daemon; no
   manual setup, no ``ephemeralpg``/``pg_tmp`` install). This is the default
   local path: a bare ``pytest`` run with Docker available exercises the real
   SQL RPCs instead of silently skipping them, so a green run without a DB is
   no longer possible when Docker is present.

Only if Docker itself is unreachable do local PostgreSQL integration tests
**skip** with a visible reason. CI sets ``BURSAR_REQUIRE_POSTGRES_TESTS=1`` so
the same condition fails the suite instead. Migrations run once per session;
every test gets a clean slate from the shared reset SQL before it starts and
again after it finishes.
"""

from __future__ import annotations

import os
import time
import warnings
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path
from typing import Any, NoReturn

import psycopg2
import pytest

from bursar.credits.postgres.store import PostgresStore, run_migrations

TEST_TENANT_ID = "00000000-0000-0000-0000-000000000001"
TEST_TENANT_SLUG = "bursar-tests"
DEFAULT_POSTGRES_IMAGE = "bursar/postgres-test:17.10-pg-jsonschema-0.3.4"
POSTGRES_BUILD_CONTEXT = Path(__file__).resolve().parents[2] / "tests" / "postgres"
RESET_SQL = (POSTGRES_BUILD_CONTEXT / "reset_bursar.sql").read_text(encoding="utf-8")


def _reset_bursar_database(dsn: str) -> None:
    """Reset every Bursar data table without touching the migration ledger."""
    conn = psycopg2.connect(dsn)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(RESET_SQL)
    finally:
        conn.close()


def _wait_until_ready(dsn: str, timeout: float = 30.0) -> None:
    """Block until Postgres at ``dsn`` accepts connections (or raise)."""
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            conn = psycopg2.connect(dsn)
            conn.close()
            return
        except Exception as e:
            last_err = e
            time.sleep(0.3)
    raise RuntimeError(f"pg_database_url not ready after {timeout:.0f}s: {last_err}")


def _resolve_persistent_dsn() -> str | None:
    """Return the already-running-Postgres DSN from DATABASE_URL."""
    dsn = os.environ.get("DATABASE_URL", "").strip()
    return dsn or None


def _require_external_database_reset_opt_in() -> None:
    """Refuse to truncate an externally supplied database without consent."""
    if os.environ.get("BURSAR_ALLOW_DATABASE_RESET") != "1":
        pytest.fail(
            "Refusing to reset externally supplied DATABASE_URL. Set "
            "BURSAR_ALLOW_DATABASE_RESET=1 only for a disposable test database.",
            pytrace=False,
        )


def _start_testcontainer() -> tuple[str, Any]:
    """Start one PostgreSQL testcontainer and return its DSN and handle."""
    try:
        from testcontainers.community.postgres import PostgresContainer
    except ModuleNotFoundError as exc:
        raise RuntimeError("testcontainers is not installed") from exc

    image = os.environ.get("BURSAR_TEST_PG_IMAGE")
    container: Any | None = None
    try:
        if image is None:
            from testcontainers.core.image import DockerImage

            built_image = DockerImage(
                path=POSTGRES_BUILD_CONTEXT,
                tag=DEFAULT_POSTGRES_IMAGE,
                clean_up=False,
            ).build()
            built_image.get_docker_client().client.close()
            image = DEFAULT_POSTGRES_IMAGE
        container = PostgresContainer(image, driver=None)
        container.start()
        return container.get_connection_url(), container
    except Exception as exc:  # Docker daemon unreachable, image pull failed, etc.
        if container is not None:
            with suppress(Exception):
                container.stop()
        raise RuntimeError(f"testcontainers could not start {image or DEFAULT_POSTGRES_IMAGE}: {exc}") from exc


def _handle_unavailable_postgres(message: str) -> NoReturn:
    """Fail required runs and visibly skip optional local integration tests."""
    if os.environ.get("BURSAR_REQUIRE_POSTGRES_TESTS") == "1":
        pytest.fail(message, pytrace=False)
    warnings.warn(message, stacklevel=2)
    pytest.skip(message)


@pytest.fixture(scope="session")
def migrated_pg_database_url() -> Iterator[str]:
    """Yield one migrated database for the integration-test session."""
    dsn = _resolve_persistent_dsn()
    container: Any | None = None
    if dsn is not None:
        _require_external_database_reset_opt_in()
        _wait_until_ready(dsn)
    else:
        try:
            dsn, container = _start_testcontainer()
        except RuntimeError as exc:
            message = (
                f"No real Postgres available: {exc}. Set DATABASE_URL to a "
                "pg_partman 5 and pg_jsonschema-enabled database, or make "
                "Docker available for testcontainers."
            )
            _handle_unavailable_postgres(message)

    try:
        # Installation is a migration-runner responsibility, never a store method.
        run_migrations(dsn)
        yield dsn
    finally:
        if container is not None:
            container.stop()


@pytest.fixture(scope="function")
def pg_database_url(migrated_pg_database_url: str) -> Iterator[str]:
    """Yield a clean real-Postgres database for one integration test."""
    _reset_bursar_database(migrated_pg_database_url)
    with psycopg2.connect(migrated_pg_database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT bursar.create_tenant(%s, %s, %s)",
            (TEST_TENANT_ID, TEST_TENANT_SLUG, "Bursar tests"),
        )
    try:
        yield migrated_pg_database_url
    finally:
        _reset_bursar_database(migrated_pg_database_url)


@pytest.fixture(scope="function")
def pg_store(pg_database_url: str) -> Iterator[PostgresStore]:
    """Yield a ``PostgresStore`` against a migrated real Postgres."""
    store = PostgresStore(
        pg_database_url,
        tenant_id=TEST_TENANT_ID,
        max_pool_size=2,
        provider_environment="test",
    )
    try:
        yield store
    finally:
        store.close()
