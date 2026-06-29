"""Fixtures for integration tests — one canonical Postgres source.

The ``pg_database_url`` fixture resolves a connection string to a **real
Postgres 16** from a single, consistent mechanism (resolution order):

1. ``DATABASE_URL`` — the env var CI and the JS (vitest) suite already use
   (see ``.github/workflows/ci.yml`` and ``javascript/tests/store-integration.test.ts``).
   Preferred so the Python and JS suites point at the same DB::

       DATABASE_URL=postgres://ducto:ducto@localhost:5432/ducto_test uv run pytest

2. ``DUCTO_TEST_PG_URL`` — legacy override for an already-running Postgres
   (e.g. a ``postgres:16`` Docker container on a non-default port). Folded in
   here so there is one mechanism; ``DATABASE_URL`` wins when both are set::

       docker run -d --name ducto-pg-test -e POSTGRES_PASSWORD=ducto \
           -e POSTGRES_DB=ducto -p 55432:5432 postgres:16
       DUCTO_TEST_PG_URL=postgresql://postgres:ducto@localhost:55432/ducto uv run pytest

3. ``pg_tmp`` (ephemeralpg) — a disposable Postgres spun up per session if the
   binary is installed (``brew install ephemeralpg``).

If none is available the Postgres/Supabase-setup tests **skip** with a visible
reason (a DB is optional in a bare sandbox).

For every source the fixture bootstraps the Supabase ``auth`` schema stubs +
standard roles so ducto's bundled SQL migrations apply cleanly on a bare
``postgres:16`` (migrations themselves are applied by ``store.setup()`` in the
per-store fixtures). When pointed at a persistent DB (``DATABASE_URL`` /
``DUCTO_TEST_PG_URL``) it TRUNCATEs ducto's tables before each test so
cross-test state never bleeds.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Iterator

import pytest

PG_TMP: str | None = shutil.which("pg_tmp")


def _preseed_supabase_objects(dsn: str) -> None:
    """Create minimal Supabase objects (auth schema, roles, functions) in a
    plain Postgres so ducto's bundled SQL migrations can run without error.

    This mirrors what Supabase provides automatically in its hosted Postgres:
    the ``auth`` schema with ``uid()``/``role()`` (role defaults to
    ``service_role`` so RPCs pass their guard), a minimal ``auth.users`` table
    for the signup-bonus trigger, and the standard roles. Idempotent.
    """
    import psycopg2

    conn = psycopg2.connect(dsn)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            # 1. auth schema + core uid/role functions
            cur.execute("CREATE SCHEMA IF NOT EXISTS auth")
            for func in [
                """
                CREATE OR REPLACE FUNCTION auth.uid() RETURNS uuid
                LANGUAGE sql STABLE
                AS $$ SELECT coalesce(
                    nullif(current_setting('request.jwt.claim.sub', true), ''),
                    current_setting('request.jwt.claims', true)::jsonb ->> 'sub'
                )::uuid $$;
                """,
                """
                CREATE OR REPLACE FUNCTION auth.role() RETURNS text
                LANGUAGE sql STABLE
                AS $$ SELECT coalesce(
                    nullif(current_setting('request.jwt.claim.role', true), ''),
                    'service_role'
                ) $$;
                """,
            ]:
                try:
                    cur.execute(func)
                except Exception:
                    conn.rollback()
                else:
                    conn.commit()

            # 2. Minimal auth.users table for the signup-bonus trigger
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS auth.users (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    email TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                """
            )

            # 3. Standard Supabase roles
            for role in ("anon", "authenticated", "service_role"):
                try:
                    cur.execute(f"CREATE ROLE {role}")
                except Exception:
                    conn.rollback()
                else:
                    conn.commit()
    finally:
        conn.close()


def _truncate_ducto_tables(dsn: str) -> None:
    """Give each test a clean slate on a persistent DB so state never bleeds.

    No-op the first time (tables don't exist yet); safe to call before
    ``store.setup()`` has ever run.
    """
    import psycopg2

    conn = psycopg2.connect(dsn)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                """
                DO $$
                DECLARE t text;
                BEGIN
                    FOR t IN
                        SELECT tablename FROM pg_tables
                        WHERE schemaname = 'public'
                          AND (tablename LIKE 'credit_%' OR tablename = 'user_credits')
                    LOOP
                        EXECUTE format('TRUNCATE TABLE public.%I CASCADE', t);
                    END LOOP;
                EXCEPTION WHEN undefined_table THEN NULL;
                END $$;
                """
            )
    finally:
        conn.close()


def _wait_until_ready(dsn: str, timeout: float = 30.0) -> None:
    """Block until Postgres at ``dsn`` accepts connections (or raise)."""
    import psycopg2

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
    """Return the already-running-Postgres DSN, preferring DATABASE_URL.

    DATABASE_URL (CI / JS suite) → DUCTO_TEST_PG_URL (legacy override) → None.
    """
    return os.environ.get("DATABASE_URL") or os.environ.get("DUCTO_TEST_PG_URL")


@pytest.fixture(scope="function")
def pg_database_url() -> Iterator[str]:
    """Yield a connection URL to a real Postgres, or skip if none is available.

    Resolution order: ``DATABASE_URL`` → ``DUCTO_TEST_PG_URL`` → ``pg_tmp`` → skip.
    """
    # 1 & 2: a persistent, already-running Postgres (DATABASE_URL or legacy override).
    persistent = _resolve_persistent_dsn()
    if persistent:
        _wait_until_ready(persistent)
        _preseed_supabase_objects(persistent)
        # Clean slate per test so cross-test state never bleeds (store.setup()
        # in the per-store fixtures then applies all migrations idempotently).
        _truncate_ducto_tables(persistent)
        yield persistent
        return

    # 3: disposable Postgres via pg_tmp.
    if PG_TMP is None:
        pytest.skip(
            "No real Postgres available: set DATABASE_URL (e.g. postgres:16 on "
            "localhost:5432, as CI and the JS suite use) or DUCTO_TEST_PG_URL, "
            "or install pg_tmp (brew install ephemeralpg)."
        )

    proc = subprocess.Popen(
        [PG_TMP, "-w", "120"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    dsn = proc.stdout.readline().strip() if proc.stdout else ""
    if not dsn:
        stderr = proc.stderr.read() if proc.stderr else ""
        proc.kill()
        raise RuntimeError(f"pg_tmp failed to start: {stderr}")

    # pg_tmp backgrounds itself and exits. Wait for Postgres to accept connections.
    _wait_until_ready(dsn)

    # Create Supabase-like objects that ducto SQL migrations depend on.
    _preseed_supabase_objects(dsn)

    yield dsn

    # pg_tmp -w N schedules auto-stop; no explicit teardown needed.
