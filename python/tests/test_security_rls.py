"""RLS / privilege-lockdown tests against a real Postgres (P0.2).

The rest of the integration suite deliberately connects as the DSN's admin
user (a Postgres superuser), and ``conftest._preseed_supabase_objects``' stub
``auth.role()`` defaults to ``'service_role'`` whenever no JWT claim is set.
That combination means every existing test silently bypasses BOTH of
bursar's authorization mechanisms:

1. Postgres's native ``REVOKE EXECUTE ... FROM PUBLIC, anon, authenticated``
   on every RPC (superusers ignore ``GRANT``/``REVOKE`` entirely).
2. The in-function ``IF auth.role() IS DISTINCT FROM 'service_role'`` guard
   (defaults to passing).

So the actual privilege lockdown — the thing that stops an ``anon`` or
``authenticated`` PostgREST request from draining another tenant's credits —
has never been exercised by any test. A regression that drops a ``REVOKE`` or
a table's RLS policy would be invisible. This module closes that gap by
connecting as the literal Postgres roles ``anon`` / ``authenticated`` /
``service_role`` (``SET LOCAL ROLE``, mirroring what PostgREST does per
request) with ``request.jwt.claim.*`` GUCs set the way a real JWT would
populate them.

Two independent mechanisms are exercised:
  - EXECUTE privilege on each RPC (enforced by the connected ROLE, checked by
    Postgres before the function body ever runs).
  - Row-level security on the raw tables (``auth.uid() = user_id``), which
    only matters once a role has table-level SELECT — a real Supabase
    project grants that by platform convention, so this module grants it
    locally (see ``_grant_platform_table_access``) purely to reproduce that
    starting point; RLS is what's actually asserted.
"""

from __future__ import annotations

from uuid import uuid4

import psycopg2
import psycopg2.errors
import pytest

from bursar.interface.postgres import PostgresStore

pytestmark = [pytest.mark.integration, pytest.mark.security]

# Real Supabase's platform bootstrap (not bursar's own migrations) grants
# anon/authenticated broad table access by default — RLS is what actually
# restricts rows. conftest._preseed_supabase_objects grants this to
# anon/authenticated/service_role already (see the "platform-level privilege
# defaults" section there); nothing further is needed here.


def _run_as(
    dsn: str,
    role: str,
    sql: str,
    params: tuple = (),
    *,
    jwt_role: str | None = None,
    jwt_sub: str | None = None,
) -> list[tuple]:
    """Execute ``sql`` in a fresh transaction impersonating ``role``.

    Mirrors what PostgREST does per request: ``SET LOCAL ROLE`` to the
    literal Postgres role for the caller's API key, plus
    ``request.jwt.claim.*`` GUCs so ``auth.uid()``/``auth.role()`` resolve
    exactly as they would for a real end-user request. Always rolled back —
    this helper is for read/permission-check assertions, never to persist
    state under a test-impersonated role.
    """
    conn = psycopg2.connect(dsn)
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute(f"SET LOCAL ROLE {role}")
            if jwt_role is not None:
                cur.execute("SET LOCAL request.jwt.claim.role = %s", (jwt_role,))
            if jwt_sub is not None:
                cur.execute("SET LOCAL request.jwt.claim.sub = %s", (jwt_sub,))

            # Self-verify the impersonation actually took effect before trusting
            # anything the query below returns. A silently-failed role switch
            # would look EXACTLY like an RLS/REVOKE bug from the outside: the
            # query would run as the connecting superuser (which bypasses both
            # privilege checks and RLS) and return data a real anon/authenticated
            # caller could never see. This turns that ambiguity into a clear,
            # actionable diagnostic instead of a confusing "wrong row" failure.
            cur.execute("SELECT current_user, auth.uid()::text, auth.role()")
            actual_user, actual_uid, actual_role = cur.fetchone()
            assert actual_user == role, (
                f"SET LOCAL ROLE did not take effect: current_user={actual_user!r}, expected {role!r}"
            )
            assert actual_uid == jwt_sub, f"auth.uid() mismatch: got {actual_uid!r}, expected jwt_sub={jwt_sub!r}"
            if jwt_role is not None:
                assert actual_role == jwt_role, f"auth.role() mismatch: got {actual_role!r}, expected {jwt_role!r}"

            cur.execute(sql, params)
            try:
                return cur.fetchall()
            except psycopg2.ProgrammingError:
                return []
    finally:
        conn.rollback()
        conn.close()


@pytest.fixture
def rls_store(pg_database_url: str) -> PostgresStore:
    store = PostgresStore(pg_database_url)
    result = store.setup()
    assert result.success
    return store


# ---------------------------------------------------------------------------
# 1. RPC privilege lockdown — anon/authenticated denied, service_role allowed
# ---------------------------------------------------------------------------

# One representative call per RPC family (mutating writes, team ops, admin
# config, analytics reads). Argument VALUES don't need to satisfy business
# rules: Postgres checks EXECUTE privilege when resolving the call, before
# the function body ever runs, so a plausible-shaped call is enough to prove
# the REVOKE holds (or doesn't).
_RPC_CALLS: dict[str, tuple[str, tuple]] = {
    "credits_add": (
        "SELECT public.credits_add(%s, 10, 'purchase', '{}'::jsonb, NULL)",
        (str(uuid4()),),
    ),
    "deduct_with_allowance": (
        "SELECT public.deduct_with_allowance(%s, 10)",
        (str(uuid4()),),
    ),
    "refund_credits": (
        "SELECT public.refund_credits(%s)",
        (str(uuid4()),),
    ),
    "create_team": (
        "SELECT public.create_team('t', 100)",
        (),
    ),
    "deduct_team": (
        "SELECT public.deduct_team(%s, %s, 10)",
        (str(uuid4()), str(uuid4())),
    ),
    "set_active_pricing_config": (
        "SELECT public.set_active_pricing_config('{}'::jsonb)",
        (),
    ),
    "spend_by_model": (
        "SELECT * FROM public.spend_by_model(now() - interval '1 day', now())",
        (),
    ),
}


class TestRpcPrivilegeLockdown:
    @pytest.mark.parametrize("rpc_name", sorted(_RPC_CALLS))
    @pytest.mark.parametrize("role", ["anon", "authenticated"])
    def test_non_service_role_denied(self, rls_store: PostgresStore, rpc_name: str, role: str) -> None:
        sql, params = _RPC_CALLS[rpc_name]
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            _run_as(rls_store.database_url, role, sql, params, jwt_role=role)

    @pytest.mark.parametrize("rpc_name", sorted(_RPC_CALLS))
    def test_service_role_allowed(self, rls_store: PostgresStore, rpc_name: str) -> None:
        sql, params = _RPC_CALLS[rpc_name]
        # Must not raise InsufficientPrivilege. The call may still fail for a
        # business reason (e.g. refunding a nonexistent transaction) — that's
        # a 200-with-error-envelope or a non-privilege exception, not a
        # privilege denial, and this test only asserts the latter never fires.
        try:
            _run_as(rls_store.database_url, "service_role", sql, params, jwt_role="service_role")
        except psycopg2.errors.InsufficientPrivilege:
            pytest.fail(f"service_role was denied EXECUTE on {rpc_name} — lockdown is over-broad")
        except psycopg2.errors.UndefinedFunction as exc:
            # Caught explicitly (not swallowed by the broad except below): a
            # typo'd/renamed RPC in _RPC_CALLS would otherwise silently pass
            # this test while giving zero real signal about the lockdown.
            pytest.fail(f"RPC {rpc_name!r} does not exist — check the SQL call in _RPC_CALLS: {exc}")
        except psycopg2.Error:
            pass  # any other DB error is a business-logic outcome, not a privilege failure


# ---------------------------------------------------------------------------
# 2. RLS tenant isolation on the raw tables
# ---------------------------------------------------------------------------


class TestRlsTenantIsolation:
    def _grant(self, dsn: str, user_id: str, amount: int) -> None:
        """Grant credits as service_role and COMMIT so the row persists."""
        conn = psycopg2.connect(dsn)
        try:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute("SET LOCAL ROLE service_role")
                cur.execute("SET LOCAL request.jwt.claim.role = 'service_role'")
                cur.execute(
                    "SELECT public.credits_add(%s, %s, 'purchase', '{}'::jsonb, NULL)",
                    (user_id, amount),
                )
            conn.commit()
        finally:
            conn.close()

    def test_authenticated_user_cannot_see_another_users_credits(self, rls_store: PostgresStore) -> None:
        dsn = rls_store.database_url
        user_a, user_b = str(uuid4()), str(uuid4())
        self._grant(dsn, user_a, 10)
        self._grant(dsn, user_b, 20)

        rows_other = _run_as(
            dsn,
            "authenticated",
            "SELECT user_id, balance FROM public.user_credits WHERE user_id = %s",
            (user_b,),
            jwt_role="authenticated",
            jwt_sub=user_a,
        )
        assert rows_other == [], f"authenticated user A could read user B's balance: {rows_other}"

        rows_own = _run_as(
            dsn,
            "authenticated",
            "SELECT user_id, balance FROM public.user_credits WHERE user_id = %s",
            (user_a,),
            jwt_role="authenticated",
            jwt_sub=user_a,
        )
        assert len(rows_own) == 1
        assert str(rows_own[0][0]) == user_a

    def test_authenticated_user_cannot_see_another_users_transactions(self, rls_store: PostgresStore) -> None:
        dsn = rls_store.database_url
        user_a, user_b = str(uuid4()), str(uuid4())
        self._grant(dsn, user_a, 10)
        self._grant(dsn, user_b, 20)

        rows_other = _run_as(
            dsn,
            "authenticated",
            "SELECT user_id FROM public.credit_transactions WHERE user_id = %s",
            (user_b,),
            jwt_role="authenticated",
            jwt_sub=user_a,
        )
        assert rows_other == [], f"authenticated user A could read user B's transactions: {rows_other}"

    def test_anon_with_no_jwt_sub_sees_no_rows(self, rls_store: PostgresStore) -> None:
        """`anon` has table-level SELECT (platform default) but no `sub`
        claim, so `auth.uid()` is NULL and the RLS predicate
        (`auth.uid() = user_id`) can never match — anon sees nothing,
        regardless of how many rows exist."""
        dsn = rls_store.database_url
        self._grant(dsn, str(uuid4()), 10)

        rows = _run_as(dsn, "anon", "SELECT user_id FROM public.user_credits", jwt_role="anon")
        assert rows == []


# ---------------------------------------------------------------------------
# 3. Schema-drift guard — every non-trigger public function must be locked
#    down. This is the durable protection for the RPC privilege lockdown: it
#    fails the moment a new RPC is added without the matching REVOKE.
# ---------------------------------------------------------------------------


class TestSchemaDriftGuard:
    def test_every_public_function_is_revoked_from_anon_and_authenticated(self, rls_store: PostgresStore) -> None:
        conn = psycopg2.connect(rls_store.database_url)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT p.proname,
                           has_function_privilege('anon', p.oid, 'EXECUTE') AS anon_exec,
                           has_function_privilege('authenticated', p.oid, 'EXECUTE') AS auth_exec
                    FROM pg_proc p
                    JOIN pg_namespace n ON n.oid = p.pronamespace
                    WHERE n.nspname = 'public'
                      -- Trigger functions (e.g. grant_signup_bonus,
                      -- handle_updated_at) are invoked internally by the
                      -- executor on DML, never called directly by a role, so
                      -- they're intentionally not part of the RPC surface
                      -- REVOKE covers.
                      AND p.prorettype != 'trigger'::regtype
                    ORDER BY p.proname
                    """
                )
                rows = cur.fetchall()
        finally:
            conn.close()

        assert rows, "expected bursar's RPC functions to exist after migrations"
        leaks = [name for name, anon_exec, auth_exec in rows if anon_exec or auth_exec]
        assert not leaks, (
            f"function(s) callable by anon/authenticated without an explicit REVOKE: {leaks}. "
            "Every mutating or reading RPC must end with "
            "`REVOKE EXECUTE ON FUNCTION public.<name>(...) FROM PUBLIC, anon, authenticated;` "
            "qualified with its exact signature (see 002_credit_rpcs.sql for the pattern)."
        )
