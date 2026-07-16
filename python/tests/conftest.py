"""Fixtures for integration tests — one canonical Postgres source.

The ``pg_database_url`` fixture resolves a connection string to a **real
Postgres 16** from a single, consistent mechanism (resolution order):

1. ``DATABASE_URL`` — the env var CI and the JS (vitest) suite already use
   (see ``.github/workflows/ci.yml`` and ``javascript/tests/store-integration.test.ts``).
   Preferred so the Python and JS suites point at the same DB::

       DATABASE_URL=postgres://bursar:bursar@localhost:5432/bursar_test uv run pytest

2. ``BURSAR_TEST_PG_URL`` — legacy override for an already-running Postgres
   (e.g. a ``postgres:16`` Docker container on a non-default port). Folded in
   here so there is one mechanism; ``DATABASE_URL`` wins when both are set::

       docker run -d --name bursar-pg-test -e POSTGRES_PASSWORD=bursar \
           -e POSTGRES_DB=bursar -p 55432:5432 postgres:16
       BURSAR_TEST_PG_URL=postgresql://postgres:bursar@localhost:55432/bursar uv run pytest

3. **testcontainers** — a disposable ``postgres:16`` container, started once
   per test session (requires only a reachable Docker daemon; no manual setup,
   no ``ephemeralpg``/``pg_tmp`` install). This is the default local path: a
   bare ``pytest`` run with Docker available exercises the real SQL RPCs
   instead of silently skipping them, so a green run without a DB is no longer
   possible when Docker is present.

Only if Docker itself is unreachable do the Postgres/Supabase-setup tests
**skip** with a visible reason.

For every source the fixture bootstraps the Supabase ``auth`` schema stubs +
standard roles so bursar's bundled SQL migrations apply cleanly on a bare
``postgres:16`` (migrations themselves are applied by ``store.setup()`` in the
per-store fixtures). Every test gets a clean slate: bursar's tables are
TRUNCATEd before each test so cross-test state never bleeds, whether the
underlying Postgres is a persistent DB or the session-scoped container.
"""

from __future__ import annotations

import atexit
import os
import time
import warnings
from collections.abc import Generator, Iterator

import psycopg2
import pytest

from bursar.interface.postgres import PostgresStore


def _pg2_conn(dsn: str) -> Generator[psycopg2.connection, None, None]:
    """Context manager yielding a fresh psycopg2 connection — auto-closes."""
    conn = psycopg2.connect(dsn)
    try:
        yield conn
    finally:
        conn.close()


def _insert_deny_cap(conn: psycopg2.connection, user_id: str, limit: int) -> None:
    """Insert a daily deny spend cap at ``limit`` for ``user_id``."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO bursar.credit_spend_caps (user_id, cap_type, cap_limit, action) "
            "VALUES (%s, 'daily', %s, 'deny')",
            (user_id, limit),
        )


def _preseed_supabase_objects(dsn: str) -> None:
    """Create minimal Supabase objects (auth schema, roles, functions) in a
    plain Postgres so bursar's bundled SQL migrations can run without error.

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

            # 2b. Minimal public.user table for the signup-bonus trigger
            # (migration 018 moved the trigger from auth.users to
            # better-auth's "user" table).
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS public."user" (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    email TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                """
            )
            cur.execute(
                """
                CREATE OR REPLACE FUNCTION public.handle_updated_at() RETURNS trigger
                LANGUAGE plpgsql AS $$
                BEGIN
                    NEW.updated_at = now();
                    RETURN NEW;
                END;
                $$;
                """
            )
            # Seed test users so migration 021's FK from user_credits to
            # public."user" does not reject credit operations. Insert a range
            # covering the fixed IDs (1–17, 99) plus the dynamic _new_uuid
            # range used across test_store_integration (9000–9514).
            cur.execute(
                """
                INSERT INTO public."user" (id)
                SELECT ('00000000-0000-0000-0000-' || LPAD(s::text, 12, '0'))::uuid
                FROM generate_series(1, 200) AS s
                ON CONFLICT (id) DO NOTHING
                """
            )
            cur.execute(
                """
                INSERT INTO public."user" (id)
                SELECT ('00000000-0000-0000-0000-' || LPAD(s::text, 12, '0'))::uuid
                FROM generate_series(9000, 9514) AS s
                ON CONFLICT (id) DO NOTHING
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

            # 4. Platform-level privilege defaults a real hosted Supabase project
            # grants automatically (via its own bootstrap migrations, not
            # bursar's). Bursar's SQL only ever REVOKEs from PUBLIC/anon/
            # authenticated on individual RPCs (see e.g. 002_credit_rpcs.sql) —
            # it never explicitly re-GRANTs to service_role, because on real
            # Supabase service_role already has broad schema-wide access and
            # BYPASSRLS. Without reproducing that here, `SET ROLE service_role`
            # would (correctly, but misleadingly) fail on every RPC — not
            # because bursar's lockdown is broken, but because this bare
            # Postgres never gave service_role the platform privileges it has
            # in production. anon/authenticated get the same broad table
            # access Supabase grants them by default; bursar's RLS policies
            # (not the absence of a GRANT) are what's supposed to restrict
            # their rows to `auth.uid() = user_id`.
            cur.execute("ALTER ROLE service_role BYPASSRLS")
            cur.execute("GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role")
            # Real Supabase also grants schema access on `auth` to these roles
            # (app/RPC code calls `auth.uid()`/`auth.jwt()` directly, not just
            # from within RLS policy predicates — those are resolved at
            # policy-definition time and don't need this, but a direct
            # `SELECT auth.uid()` from application code does).
            cur.execute("GRANT USAGE ON SCHEMA auth TO anon, authenticated, service_role")
            cur.execute(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO anon, authenticated"
            )
            cur.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO service_role")
            cur.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO service_role")
            cur.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT EXECUTE ON FUNCTIONS TO service_role")
            # Also apply to any tables/functions that already exist (a persistent
            # DATABASE_URL may already have bursar's schema from a prior run;
            # ALTER DEFAULT PRIVILEGES only covers objects created afterwards).
            cur.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO anon, authenticated")
            cur.execute("GRANT ALL ON ALL TABLES IN SCHEMA public TO service_role")
            cur.execute("GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO service_role")
            cur.execute("GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO service_role")
    finally:
        conn.close()


def _truncate_bursar_tables(dsn: str) -> None:
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
                        WHERE schemaname = 'bursar'
                          AND (tablename LIKE 'credit_%' OR tablename = 'user_credits'
                               OR tablename LIKE 'billing_%'
                               OR tablename IN ('bursar_config', 'signup_grant_failures'))
                    LOOP
                        EXECUTE format('TRUNCATE TABLE bursar.%I CASCADE', t);
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

    DATABASE_URL (CI / JS suite) → BURSAR_TEST_PG_URL (legacy override) → None.
    """
    return os.environ.get("DATABASE_URL") or os.environ.get("BURSAR_TEST_PG_URL")


# Session-scoped testcontainers Postgres, started lazily on first use and
# reused for the rest of the run (starting a fresh container per test would
# dominate wall time). ``None`` means "not yet attempted"; the sentinel
# ``_UNAVAILABLE`` means "attempted and failed" (e.g. no Docker daemon) so we
# only try once and skip cleanly for every subsequent test.
_UNAVAILABLE = object()
_container_dsn: str | object | None = None


def _testcontainers_dsn() -> str | None:
    """Return a DSN for a session-scoped ``postgres:16`` testcontainer.

    Returns ``None`` if Docker itself is unavailable (e.g. no daemon
    reachable) so the caller can skip with a clear reason instead of erroring.
    """
    global _container_dsn
    if _container_dsn is _UNAVAILABLE:
        return None
    if _container_dsn is not None:
        return _container_dsn  # type: ignore[return-value]

    try:
        from testcontainers.postgres import PostgresContainer
    except ModuleNotFoundError:
        _container_dsn = _UNAVAILABLE
        return None

    try:
        container = PostgresContainer("postgres:16", driver=None)
        container.start()
    except Exception as exc:  # Docker daemon unreachable, image pull failed, etc.
        warnings.warn(
            f"testcontainers could not start postgres:16 ({exc}); "
            "DB integration tests will skip. Set DATABASE_URL to point at an "
            "already-running Postgres instead.",
            stacklevel=2,
        )
        _container_dsn = _UNAVAILABLE
        return None

    atexit.register(container.stop)
    dsn = container.get_connection_url()
    _preseed_supabase_objects(dsn)
    _container_dsn = dsn
    return dsn


@pytest.fixture(scope="function")
def pg_database_url() -> Iterator[str]:
    """Yield a connection URL to a real Postgres, or skip if none is available.

    Resolution order: ``DATABASE_URL`` → ``BURSAR_TEST_PG_URL`` →
    testcontainers-managed ``postgres:16`` → skip.
    """
    # 1 & 2: a persistent, already-running Postgres (DATABASE_URL or legacy override).
    persistent = _resolve_persistent_dsn()
    dsn = persistent
    if dsn:
        _wait_until_ready(dsn)
        _preseed_supabase_objects(dsn)
    else:
        # 3: disposable Postgres via testcontainers (session-scoped, lazy).
        dsn = _testcontainers_dsn()
        if dsn is None:
            pytest.skip(
                "No real Postgres available: set DATABASE_URL (e.g. postgres:16 "
                "on localhost:5432, as CI and the JS suite use) or "
                "BURSAR_TEST_PG_URL, or make Docker available for testcontainers."
            )

    # Clean slate per test so cross-test state never bleeds (store.setup() in
    # the per-store fixtures then applies all migrations idempotently).
    _truncate_bursar_tables(dsn)
    yield dsn


@pytest.fixture(scope="function")
def pg_store(pg_database_url: str) -> Iterator[PostgresStore]:
    """Yield a ``PostgresStore`` with migrations applied against a real Postgres.

    Depends on ``pg_database_url``, so this fixture auto-skips when no Postgres
    is available. The store's cleanup (migration replay) is idempotent.
    """
    store = PostgresStore(pg_database_url, max_pool_size=2)
    result = store.setup()
    assert result.success
    yield store
    store.close()
