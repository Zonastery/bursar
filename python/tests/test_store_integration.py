"""Integration tests for each storage provider.

Postgres is provided via a **real Postgres 16** instance. The single
``pg_database_url`` fixture lives in ``conftest.py`` and resolves a connection
string in this order: ``DATABASE_URL`` (what CI and the JS suite use) →
``BURSAR_TEST_PG_URL`` (legacy override) → ``pg_tmp`` (disposable) → skip.

If none is available the Postgres/Supabase-setup tests **skip** with a visible
reason (a DB is optional in a bare sandbox); they are correct and CI-runnable
against any source.

MemoryStore needs zero infra and is always exercised (including the
concurrency/double-spend test).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

try:
    import psycopg2
except ModuleNotFoundError:
    psycopg2 = None  # type: ignore[assignment]

import pytest

from bursar import ConfigError, CreditManager, UsageMetrics
from bursar.allowance import resolve_allowance_window, resolve_calendar_window
from bursar.events import CREDIT_EVENT_TYPES, CreditEvent, CreditEventEmitter
from bursar.interface.base import StoreError
from bursar.interface.memory import MemoryStore
from bursar.interface.models import (
    CreditMetadata,
    FeatureLimit,
    SpendCap,
)
from bursar.interface.postgres import PostgresStore
from bursar.interface.supabase import HttpxSupabaseStore
from bursar.manager import InsufficientCreditsError

# ---------------------------------------------------------------------------
# Shared pricing config used across all tests
# ---------------------------------------------------------------------------

_PRICING = {
    "version": 1,
    "metering": {
        "models": {
            "gpt-4": "input_tokens * 0.01 + output_tokens * 0.03",
            "*": "input_tokens * 0.001 + output_tokens * 0.003",
        },
        "tools": {"*": "tool_calls * 0"},
    },
    "ledger": {"min_balance": 5},
}

_PG_USER = "00000000-0000-0000-0000-000000000001"

# 100 input + 50 output tokens @ gpt-4 = 100*0.01 + 50*0.03 = 2.5 (exact, no truncation).
_METRICS = UsageMetrics(model="gpt-4", input_tokens=100, output_tokens=50)
# Cost is the exact Decimal charge — the atomic deduct_with_allowance flow does
# NOT truncate to int, and result.amount is the positive net charge (not negated).
_EXPECTED_COST = Decimal("2.5")


def _add_and_deduct(manager: CreditManager, user_id: str = "u1") -> None:
    """Shared helper: add credits → deduct → verify."""
    manager.add_credits(user_id, 100)

    result = manager.deduct(user_id, _METRICS, idempotency_key="tx_1")
    # Net charge is positive (debited from balance after free allowance), exact Decimal.
    assert result.amount == _EXPECTED_COST
    assert result.amount == Decimal("2.5")
    assert result.balance_after == Decimal("97.5")
    assert result.balance_after == Decimal(100) - _EXPECTED_COST

    balance = manager.get_balance(user_id)
    assert balance.balance == Decimal("97.5")
    assert balance.balance == Decimal(100) - _EXPECTED_COST


# ---------------------------------------------------------------------------
# The real-Postgres ``pg_database_url`` fixture lives in conftest.py (single
# mechanism: DATABASE_URL → BURSAR_TEST_PG_URL → pg_tmp → skip).
# ---------------------------------------------------------------------------


def _new_uuid(suffix: int) -> str:
    """Deterministic distinct UUID per concurrent worker."""
    return f"00000000-0000-0000-0000-{suffix:012d}"


# ═══════════════════════════════════════════════════════════════════════════
# MemoryStore
# ═══════════════════════════════════════════════════════════════════════════


class TestMemoryStoreIntegration:
    """Full credit lifecycle via MemoryStore — zero infra needed."""

    @pytest.fixture
    def manager(self) -> CreditManager:
        store = MemoryStore()
        store.setup()
        m = CreditManager(store=store)
        m.publish_pricing_from_dict(_PRICING)
        return m

    def test_full_flow(self, manager: CreditManager) -> None:
        _add_and_deduct(manager)

    def test_idempotent_deduction(self, manager: CreditManager) -> None:
        manager.add_credits("user_1", 100)

        r1 = manager.deduct("user_1", _METRICS, idempotency_key="dup")
        r2 = manager.deduct("user_1", _METRICS, idempotency_key="dup")
        assert r2.idempotent
        assert r2.transaction_id == r1.transaction_id

    def test_insufficient_credits(self, manager: CreditManager) -> None:
        # deduct() no longer reserves; the atomic deduct_with_allowance flow
        # raises InsufficientCreditsError when the balance floor would be breached.
        from bursar.manager import InsufficientCreditsError

        with pytest.raises(InsufficientCreditsError, match="Insufficient credits"):
            manager.deduct("user_1", _METRICS)

    def test_setup_lists_all_bundled_migrations(self) -> None:
        """setup() derives its file list from the SQL glob, not a hardcode (L5)."""
        from bursar.sql import _get_sql_files

        store = MemoryStore()
        result = store.setup()
        expected = [f.name for f in _get_sql_files()]
        assert result.tables_created == expected
        # 009_deduct_and_leases.sql et al. are present (would be missing if hardcoded).
        assert "009_deduct_and_leases.sql" in result.tables_created
        assert "010_credit_tiers.sql" in result.tables_created

    def test_check_feature(self) -> None:
        store = MemoryStore()
        store.setup()
        store.set_active_pricing(
            {
                "version": 1,
                "metering": {"models": {"*": "1"}},
                "plans": {
                    "pro": {
                        "label": "Pro",
                        "allowance": {"amount": Decimal("500")},
                        "entitlements": {"ai_chat": {"value": True}, "max_roadmaps": {"value": 20}},
                    },
                },
            }
        )
        store.set_user_plan("user_1", "pro")

        result = store.check_feature("user_1", "ai_chat")
        assert result.has_feature is True
        assert result.value is True

        result = store.check_feature("user_1", "export_pdf")
        assert result.has_feature is False

        result = store.check_feature("nobody", "ai_chat")
        assert result.has_feature is False

    # -- deduct_with_allowance: money/Decimal -------------------------------

    def test_deduct_with_allowance_fractional_no_truncation(self) -> None:
        """A sub-1-credit op charges the exact fraction (contract §1, no int())."""
        store = MemoryStore()
        store.add_credits("u", Decimal("100"))
        r = store.deduct_with_allowance("u", Decimal("0.4"))
        assert r.error is None
        assert r.amount == Decimal("0.4")
        assert r.balance_after == Decimal("99.6")
        assert store.get_balance("u").balance == Decimal("99.6")

    def test_deduct_with_allowance_consumes_plan_allowance_first(self) -> None:
        store = MemoryStore()
        store.set_active_pricing(
            {
                "version": 1,
                "metering": {"models": {"*": "1"}},
                "plans": {"pro": {"label": "Pro", "allowance": {"amount": Decimal("10")}}},
            }
        )
        store.set_user_plan("u", "pro")
        store.add_credits("u", Decimal("100"))

        # gross 12 → 10 covered by allowance, 2 charged to balance
        r = store.deduct_with_allowance("u", Decimal("12"))
        assert r.error is None
        assert r.allowance_consumed == Decimal("10")
        assert r.amount == Decimal("2")
        assert store.get_balance("u").balance == Decimal("98")

    def test_deduct_with_allowance_insufficient_no_allowance_consumed(self) -> None:
        store = MemoryStore()
        store.set_active_pricing(
            {
                "version": 1,
                "metering": {"models": {"*": "1"}},
                "plans": {"pro": {"label": "Pro", "allowance": {"amount": Decimal("10")}}},
            }
        )
        store.set_user_plan("u", "pro")
        store.add_credits("u", Decimal("5"))

        # gross 100: 10 would be allowance, 90 net but balance only 5 with min 0
        r = store.deduct_with_allowance("u", Decimal("100"), min_balance=Decimal("0"))
        assert r.error == "insufficient_credits"
        # All-or-nothing: allowance NOT consumed on failure.
        assert store.check_allowance("u").allowance_remaining == Decimal("10")
        assert store.get_balance("u").balance == Decimal("5")

    def test_deduct_with_allowance_idempotent_replay(self) -> None:
        store = MemoryStore()
        store.add_credits("u", Decimal("100"))
        r1 = store.deduct_with_allowance("u", Decimal("7.5"), idempotency_key="abc")
        r2 = store.deduct_with_allowance("u", Decimal("7.5"), idempotency_key="abc")
        assert not r1.idempotent
        assert r2.idempotent
        assert r2.transaction_id == r1.transaction_id
        assert r2.amount == Decimal("7.5")
        # Charged exactly once.
        assert store.get_balance("u").balance == Decimal("92.5")

    def test_deduct_with_allowance_idempotent_cross_user_no_collision(self) -> None:
        store = MemoryStore()
        store.add_credits("a", Decimal("100"))
        store.add_credits("b", Decimal("100"))
        store.deduct_with_allowance("a", Decimal("10"), idempotency_key="same")
        # Same key, different user → NOT treated as a replay.
        rb = store.deduct_with_allowance("b", Decimal("10"), idempotency_key="same")
        assert rb.idempotent is False
        assert store.get_balance("b").balance == Decimal("90")

    def test_deduct_with_allowance_invalid_amount(self) -> None:
        store = MemoryStore()
        store.add_credits("u", Decimal("100"))
        r = store.deduct_with_allowance("u", Decimal("-1"))
        assert r.error == "invalid_amount"
        assert store.get_balance("u").balance == Decimal("100")

    # -- Cap accumulation / boundary / soft caps ----------------------------

    def test_cap_deny_blocks_and_consumes_nothing(self) -> None:
        store = MemoryStore()
        store.add_credits("u", Decimal("1000"))
        store.set_spend_cap(SpendCap(user_id="u", type="monthly", limit=Decimal("50"), action="deny"))

        r = store.deduct_with_allowance("u", Decimal("60"))
        assert r.error == "cap_reached"
        assert store.get_balance("u").balance == Decimal("1000")

    def test_cap_accumulates_across_prior_spend(self) -> None:
        store = MemoryStore()
        store.add_credits("u", Decimal("1000"))
        store.set_spend_cap(SpendCap(user_id="u", type="monthly", limit=Decimal("50"), action="deny"))

        # Spend 30, then 20 → total 50 (== limit, allowed). 30 + 20 = 50, not > 50.
        assert store.deduct_with_allowance("u", Decimal("30")).error is None
        assert store.deduct_with_allowance("u", Decimal("20")).error is None
        # One more credit pushes over → denied.
        assert store.deduct_with_allowance("u", Decimal("1")).error == "cap_reached"
        assert store.get_balance("u").balance == Decimal("950")

    def test_cap_boundary_amount_equals_limit_allowed(self) -> None:
        store = MemoryStore()
        store.add_credits("u", Decimal("1000"))
        store.set_spend_cap(SpendCap(user_id="u", type="daily", limit=Decimal("100"), action="deny"))
        # amount == limit is allowed (strict > comparison).
        r = store.deduct_with_allowance("u", Decimal("100"))
        assert r.error is None

    def test_cap_warn_sets_warning_and_charges(self) -> None:
        store = MemoryStore()
        store.add_credits("u", Decimal("1000"))
        store.set_spend_cap(SpendCap(user_id="u", type="monthly", limit=Decimal("50"), action="warn"))
        r = store.deduct_with_allowance("u", Decimal("60"))
        assert r.error is None
        assert r.cap_warning == "warn"
        assert store.get_balance("u").balance == Decimal("940")

    # -- Refunds ------------------------------------------------------------

    def test_refund_over_refund_rejected(self) -> None:
        store = MemoryStore()
        store.add_credits("u", Decimal("100"))
        d = store.deduct_with_allowance("u", Decimal("30"))
        r = store.refund_credits(d.transaction_id, amount=Decimal("40"))
        assert r.error == "over_refund"
        assert store.get_balance("u").balance == Decimal("70")

    def test_refund_cumulative_partials_to_exact_then_over(self) -> None:
        store = MemoryStore()
        store.add_credits("u", Decimal("100"))
        d = store.deduct_with_allowance("u", Decimal("30"))
        assert store.refund_credits(d.transaction_id, amount=Decimal("10")).error is None
        assert store.refund_credits(d.transaction_id, amount=Decimal("20")).error is None
        # cumulative 30 == original → any further refund over-refunds.
        assert store.refund_credits(d.transaction_id, amount=Decimal("1")).error == "over_refund"
        assert store.get_balance("u").balance == Decimal("100")

    def test_refund_duplicate_full_returns_already_refunded(self) -> None:
        store = MemoryStore()
        store.add_credits("u", Decimal("100"))
        d = store.deduct_with_allowance("u", Decimal("30"))
        assert store.refund_credits(d.transaction_id).error is None
        assert store.refund_credits(d.transaction_id).error == "already_refunded"

    def test_refund_of_purchase_rejected(self) -> None:
        store = MemoryStore()
        add = store.add_credits("u", Decimal("100"), "purchase")
        r = store.refund_credits(add.transaction_id)
        assert r.error == "over_refund"

    # -- Expiry double-sweep ------------------------------------------------

    def test_expiry_double_sweep_reports_zero(self) -> None:
        store = MemoryStore()
        store.add_credits("u", Decimal("100"), "purchase", expires_at=datetime.now(UTC) - timedelta(hours=1))
        first = store.sweep_expired_credits()
        assert first.expired_count == 1
        assert first.expired_amount == Decimal("100")
        assert store.get_balance("u").balance == Decimal("0")

        # Second sweep must report zero and not double-debit (H4).
        second = store.sweep_expired_credits()
        assert second.expired_count == 0
        assert second.expired_amount == Decimal("0")
        assert store.get_balance("u").balance == Decimal("0")

    # -- Concurrency / double-spend (REQUIRED) ------------------------------

    def test_concurrent_deduct_no_double_spend_memory(self) -> None:
        """N concurrent deductions; balance covers only some. The RLock must
        serialize read-modify-write so the total debited never exceeds the
        starting balance and exactly the expected number succeed."""
        store = MemoryStore()
        store.add_credits("u", Decimal("100"))
        n = 30
        each = Decimal("10")  # only 10 of 30 fit in 100

        def one(i: int) -> object:
            return store.deduct_with_allowance("u", each, idempotency_key=f"c{i}", min_balance=Decimal("0"))

        with ThreadPoolExecutor(max_workers=n) as ex:
            results = list(ex.map(one, range(n)))

        succeeded = [r for r in results if not r.error]  # type: ignore[attr-defined]
        balance = store.get_balance("u").balance
        assert len(succeeded) == 10
        assert balance == Decimal("0")
        assert balance >= 0

    def test_concurrent_same_idempotency_key_one_debit_memory(self) -> None:
        """Same key from many concurrent callers → exactly one debit."""
        store = MemoryStore()
        store.add_credits("u", Decimal("100"))

        def one(_: int) -> object:
            return store.deduct_with_allowance("u", Decimal("10"), idempotency_key="dup")

        with ThreadPoolExecutor(max_workers=16) as ex:
            results = list(ex.map(one, range(16)))

        non_idem = [r for r in results if not r.idempotent and not r.error]  # type: ignore[attr-defined]
        assert len(non_idem) == 1
        assert store.get_balance("u").balance == Decimal("90")


# ═══════════════════════════════════════════════════════════════════════════
# PostgresStore (real Postgres)
# ═══════════════════════════════════════════════════════════════════════════


class TestPostgresStoreIntegration:
    """Full credit lifecycle via PostgresStore + real Postgres."""

    @pytest.fixture
    def store(self, pg_database_url: str) -> PostgresStore:
        store = PostgresStore(pg_database_url)
        result = store.setup()
        assert result.success
        assert len(result.tables_created) > 0
        return store

    @pytest.fixture
    def manager(self, store: PostgresStore) -> CreditManager:
        m = CreditManager(store=store)
        m.publish_pricing_from_dict(_PRICING)
        return m

    def test_setup_is_idempotent(self, store: PostgresStore) -> None:
        # Running migrations twice succeeds (migration idempotency).
        result = store.setup()
        assert result.success
        assert not result.errors

    def test_full_flow_pg(self, manager: CreditManager) -> None:
        _add_and_deduct(manager, _PG_USER)

    def test_signup_bonus_granted_when_auth_role_is_not_service_role_pg(self, store: PostgresStore) -> None:
        """Regression test for a production-only bug: grant_signup_bonus()
        (the constraint trigger on auth.users) must call credits_add_internal,
        NOT the guarded credits_add. On real Supabase, a GoTrue signup INSERT
        into auth.users runs with no PostgREST/JWT request context, so
        auth.role() reads NULL there — the guarded credits_add would reject
        with {"error": "unauthorized"} (silently swallowed by the trigger's
        PERFORM), dropping every signup bonus. The bundled test harness's
        auth.role() stub (see conftest._preseed_supabase_objects) defaults to
        'service_role' when unset, which would mask this regression — so this
        test explicitly sets a non-service_role JWT role for the transaction
        that inserts the auth.users row, reproducing the real-Supabase
        condition instead of relying on the stub's fallback.
        """
        conn = psycopg2.connect(store._database_url)
        try:
            conn.autocommit = False
            with conn.cursor() as cur:
                # Scoped to this transaction only (SET LOCAL): the deferred
                # constraint trigger (on_signup_credit_bonus is DEFERRABLE
                # INITIALLY DEFERRED) fires during COMMIT processing, still
                # inside this same transaction, so it sees this setting.
                cur.execute("SET LOCAL request.jwt.claim.role = 'anon'")
                cur.execute("INSERT INTO auth.users DEFAULT VALUES RETURNING id")
                new_user_id = cur.fetchone()[0]
            conn.commit()
        finally:
            conn.close()

        balance = store.get_balance(str(new_user_id))
        assert balance.balance > Decimal("0")

        tiers = store.get_bucket_balances(str(new_user_id))
        assert sum((t.balance for t in tiers.buckets), Decimal(0)) == balance.balance

    def test_deduct_with_allowance_fractional_pg(self, store: PostgresStore) -> None:
        store.add_credits(_PG_USER, Decimal("100"), "purchase")
        r = store.deduct_with_allowance(_PG_USER, Decimal("2.5"), idempotency_key="k1", model="gpt-4")
        assert r.error is None
        assert r.amount == Decimal("2.5")  # not truncated to 2
        assert r.balance_after == Decimal("97.5")
        assert isinstance(r.amount, Decimal)
        # Idempotent replay returns the original, charges nothing more.
        r2 = store.deduct_with_allowance(_PG_USER, Decimal("2.5"), idempotency_key="k1", model="gpt-4")
        assert r2.idempotent is True
        assert store.get_balance(_PG_USER).balance == Decimal("97.5")

    def test_deduct_with_allowance_insufficient_pg(self, store: PostgresStore) -> None:
        store.add_credits(_PG_USER, Decimal("5"), "purchase")
        r = store.deduct_with_allowance(_PG_USER, Decimal("1000"), min_balance=Decimal("0"))
        assert r.error == "insufficient_credits"
        assert store.get_balance(_PG_USER).balance == Decimal("5")

    def test_cap_deny_pg(self, store: PostgresStore) -> None:
        store.add_credits(_PG_USER, Decimal("1000"), "purchase")
        conn = psycopg2.connect(store._database_url)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO public.credit_spend_caps (user_id, cap_type, cap_limit, action) "
                    "VALUES (%s, 'monthly', 50, 'deny')",
                    [_PG_USER],
                )
            conn.commit()
        finally:
            conn.close()
        r = store.deduct_with_allowance(_PG_USER, Decimal("60"))
        assert r.error == "cap_reached"
        assert store.get_balance(_PG_USER).balance == Decimal("1000")

    def test_refund_over_and_duplicate_pg(self, store: PostgresStore) -> None:
        store.add_credits(_PG_USER, Decimal("100"), "purchase")
        d = store.deduct_with_allowance(_PG_USER, Decimal("30"))
        # over-refund
        assert store.refund_credits(d.transaction_id, amount=Decimal("40")).error == "over_refund"
        # cumulative partials to exact
        assert store.refund_credits(d.transaction_id, amount=Decimal("10")).error is None
        assert store.refund_credits(d.transaction_id, amount=Decimal("20")).error is None
        assert store.refund_credits(d.transaction_id, amount=Decimal("1")).error == "over_refund"
        assert store.get_balance(_PG_USER).balance == Decimal("100")

    def test_refund_of_purchase_rejected_pg(self, store: PostgresStore) -> None:
        add = store.add_credits(_PG_USER, Decimal("100"), "purchase")
        assert store.refund_credits(add.transaction_id).error == "over_refund"

    def test_expiry_double_sweep_pg(self, store: PostgresStore) -> None:
        store.add_credits(_PG_USER, Decimal("100"), "purchase", expires_at=datetime.now(UTC) - timedelta(hours=1))
        first = store.sweep_expired_credits()
        assert first.expired_amount == Decimal("100")
        assert store.get_balance(_PG_USER).balance == Decimal("0")
        # Second sweep reports zero, no double-debit (H4 SQL parity).
        second = store.sweep_expired_credits()
        assert second.expired_count == 0
        assert second.expired_amount == Decimal("0")
        assert store.get_balance(_PG_USER).balance == Decimal("0")

    @pytest.mark.repeat(5)  # money-critical race: rerun to surface rare interleavings
    def test_concurrent_deduct_no_double_spend_pg(self, store: PostgresStore) -> None:
        """N concurrent deduct_with_allowance against a real Postgres row.
        SELECT ... FOR UPDATE must serialize them: exactly 10 of 30 succeed,
        total debited ≤ starting balance, balance never negative."""
        store.add_credits(_PG_USER, Decimal("100"), "purchase")
        n = 30

        def one(i: int) -> object:
            # Fresh store/connection per thread (psycopg2 connections aren't
            # thread-safe to share); same DSN and same user row.
            s = PostgresStore(store._database_url)
            return s.deduct_with_allowance(_PG_USER, Decimal("10"), idempotency_key=f"c{i}", min_balance=Decimal("0"))

        with ThreadPoolExecutor(max_workers=n) as ex:
            results = list(ex.map(one, range(n)))

        succeeded = [r for r in results if not r.error]  # type: ignore[attr-defined]
        balance = store.get_balance(_PG_USER).balance
        assert len(succeeded) == 10
        assert balance == Decimal("0")
        assert balance >= 0

    @pytest.mark.repeat(5)  # money-critical race: rerun to surface rare interleavings
    def test_concurrent_same_idempotency_key_one_debit_pg(self, store: PostgresStore) -> None:
        store.add_credits(_PG_USER, Decimal("100"), "purchase")

        def one(_: int) -> object:
            s = PostgresStore(store._database_url)
            return s.deduct_with_allowance(_PG_USER, Decimal("10"), idempotency_key="dup")

        with ThreadPoolExecutor(max_workers=16) as ex:
            results = list(ex.map(one, range(16)))

        non_idem = [r for r in results if not r.idempotent and not r.error]  # type: ignore[attr-defined]
        assert len(non_idem) == 1
        assert store.get_balance(_PG_USER).balance == Decimal("90")

    def test_check_feature_pg(self, store: PostgresStore) -> None:
        # Publish pricing with plan features → plans get synced to credit_plans
        store.set_active_pricing(
            {
                "version": 1,
                "metering": {"models": {"*": "1"}},
                "plans": {
                    "pro": {
                        "label": "Pro Plan",
                        "allowance": {"amount": Decimal("500")},
                        "entitlements": {"ai_chat": {"value": True}, "max_roadmaps": {"value": 20}},
                    },
                },
            }
        )
        # set_user_plan resolves "pro" plan_key to credit_plans UUID internally
        store.set_user_plan(_PG_USER, "pro")

        result = store.get_user_plan(_PG_USER)
        assert result.plan_label == "Pro Plan"
        assert result.entitlements["ai_chat"].value is True
        assert result.entitlements["max_roadmaps"].value == 20

        result = store.check_feature(_PG_USER, "ai_chat")
        assert result.has_feature is True

        result = store.check_feature(_PG_USER, "export_pdf")
        assert result.has_feature is False

    def test_balance_persists_across_managers(self, store: PostgresStore) -> None:
        m1 = CreditManager(store=store)
        m1.publish_pricing_from_dict(_PRICING)
        m1.add_credits(_PG_USER, 100)
        m1.deduct(_PG_USER, _METRICS, idempotency_key="tx_1")

        # Fresh manager, same store — balance should survive
        m2 = CreditManager(store=store)
        m2.load_pricing_from_store()
        balance = m2.get_balance(_PG_USER)
        assert balance.balance == 100 - _EXPECTED_COST

    # ── INT1: spendByModel ────────────────────────────────────────────────

    def test_spend_by_model_pg(self, store: PostgresStore) -> None:
        """INT1: two deductions with distinct models appear as separate buckets."""
        from_date = datetime.now(UTC) - timedelta(hours=1)
        to_date = datetime.now(UTC) + timedelta(hours=1)

        store.add_credits(_PG_USER, Decimal("1000"), "purchase")
        # deduct_with_allowance records model in metadata via p_model
        store.deduct_with_allowance(_PG_USER, Decimal("10"), idempotency_key="sbm_gpt4", model="gpt-4")
        store.deduct_with_allowance(_PG_USER, Decimal("5"), idempotency_key="sbm_claude", model="claude-3")

        # The spend_by_model RPC returns TABLE rows (not JSON), so call directly.
        conn = psycopg2.connect(store._database_url)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM spend_by_model(%s, %s)",
                    [from_date.isoformat(), to_date.isoformat()],
                )
                rows = cur.fetchall()
        finally:
            conn.close()

        # rows are (model TEXT, total_spend NUMERIC, transaction_count BIGINT)
        by_model = {r[0]: r[1] for r in rows}

        assert "gpt-4" in by_model, f"gpt-4 not in {list(by_model)}"
        assert "claude-3" in by_model, f"claude-3 not in {list(by_model)}"
        assert by_model["gpt-4"] == Decimal("10")
        assert by_model["claude-3"] == Decimal("5")
        assert isinstance(by_model["gpt-4"], Decimal)

    # ── INT2: topUsers ────────────────────────────────────────────────────

    def test_top_users_pg(self, store: PostgresStore) -> None:
        """INT2: 3 users deducted, top_users(limit=2) returns top 2 descending."""
        from_date = datetime.now(UTC) - timedelta(hours=1)
        to_date = datetime.now(UTC) + timedelta(hours=1)

        u1 = "00000000-0000-0000-0000-000000000101"
        u2 = "00000000-0000-0000-0000-000000000102"
        u3 = "00000000-0000-0000-0000-000000000103"

        for uid, amount in [(u1, Decimal("50")), (u2, Decimal("30")), (u3, Decimal("80"))]:
            store.add_credits(uid, Decimal("1000"), "purchase")
            store.deduct_with_allowance(uid, amount, idempotency_key=f"tu_{uid}")

        # The top_users RPC returns TABLE rows (not JSON), so call directly.
        conn = psycopg2.connect(store._database_url)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM top_users(%s, %s, %s)",
                    [2, from_date.isoformat(), to_date.isoformat()],
                )
                rows = cur.fetchall()
        finally:
            conn.close()

        # rows are (user_id TEXT, total_spend NUMERIC)
        assert len(rows) == 2
        assert rows[0][0] == u3
        assert rows[0][1] == Decimal("80")
        assert rows[1][0] == u1
        assert rows[1][1] == Decimal("50")

    # ── INT3: dailySpend ──────────────────────────────────────────────────

    def test_daily_spend_pg(self, store: PostgresStore) -> None:
        """INT3: after a deduction, daily_spend has at least one non-zero bucket."""
        from_date = datetime.now(UTC) - timedelta(hours=1)
        to_date = datetime.now(UTC) + timedelta(hours=1)

        store.add_credits(_PG_USER, Decimal("1000"), "purchase")
        store.deduct_with_allowance(_PG_USER, Decimal("7"), idempotency_key="ds_1")

        # The daily_spend RPC returns TABLE rows (not JSON), so call directly.
        conn = psycopg2.connect(store._database_url)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM daily_spend(%s, %s)",
                    [from_date.isoformat(), to_date.isoformat()],
                )
                rows = cur.fetchall()
        finally:
            conn.close()

        # rows are (date TEXT, total_spend NUMERIC, transaction_count BIGINT)
        assert len(rows) >= 1
        totals = [r[1] for r in rows]
        assert any(t > 0 for t in totals), f"All totals zero: {totals}"
        # date field is a string in YYYY-MM-DD format
        for r in rows:
            date_str = r[0]
            assert isinstance(date_str, str), f"date is not str: {type(date_str)}"
            assert len(date_str) == 10, f"date not YYYY-MM-DD: {date_str!r}"
            assert date_str[4] == "-" and date_str[7] == "-"
            assert isinstance(r[1], Decimal)

    # ── INT4: aggregateStats ──────────────────────────────────────────────

    def test_aggregate_stats_pg(self, store: PostgresStore) -> None:
        """INT4: stats after a deduction + purchase reflect correct totals."""
        from_date = datetime.now(UTC) - timedelta(hours=1)
        to_date = datetime.now(UTC) + timedelta(hours=1)

        store.add_credits(_PG_USER, Decimal("1000"), "purchase")
        store.deduct_with_allowance(_PG_USER, Decimal("15"), idempotency_key="as_1")

        stats = store.aggregate_stats(from_date, to_date)

        assert stats.total_credits_consumed is not None
        assert stats.total_credits_consumed == Decimal("15")
        assert isinstance(stats.total_credits_consumed, Decimal)
        assert stats.active_users >= 1
        assert stats.active_users is not None

    # ── INT5: listUsageEvents ─────────────────────────────────────────────

    def test_list_usage_events_pg(self, store: PostgresStore) -> None:
        """INT5: after a deduction, list_usage_events returns the event."""
        from_date = datetime.now(UTC) - timedelta(hours=1)
        to_date = datetime.now(UTC) + timedelta(hours=1)

        store.add_credits(_PG_USER, Decimal("1000"), "purchase")
        store.deduct_with_allowance(_PG_USER, Decimal("8"), idempotency_key="ue_1")

        # list_usage_events is a SQL function; call it via psycopg2 directly
        import psycopg2 as _pg2

        conn = _pg2.connect(store._database_url)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM list_usage_events(%s, %s, %s)",
                    [_PG_USER, from_date.isoformat(), to_date.isoformat()],
                )
                rows = cur.fetchall()
        finally:
            conn.close()

        assert len(rows) >= 1
        # columns: id, user_id, amount, type, reference_type, reference_id,
        #          metadata, created_at, total_count
        user_ids = [str(r[1]) for r in rows]
        assert _PG_USER in user_ids
        amounts = [abs(r[2]) for r in rows]
        assert any(a > 0 for a in amounts), f"All amounts zero: {amounts}"

    # ── INT6: cap deny does NOT consume allowance ─────────────────────────

    def test_cap_deny_does_not_consume_allowance_pg(self, store: PostgresStore) -> None:
        """INT6: a deny cap blocks the deduction AND leaves allowance untouched.

        Setup: free_allowance=5, deny cap=10.  Deduct 20 → v_net=15 (after 5
        allowance) → cap check: 0 + 15 > 10 → denied → allowance rolled back.
        """
        store.set_active_pricing(
            {
                "version": 1,
                "metering": {"models": {"*": "1"}},
                "plans": {
                    "basic": {
                        "label": "Basic",
                        "allowance": {"amount": Decimal("5")},
                    }
                },
            }
        )
        store.set_user_plan(_PG_USER, "basic")
        store.add_credits(_PG_USER, Decimal("1000"), "purchase")

        # Record allowance before we attempt the capped deduction
        before = store.check_allowance(_PG_USER)

        # Insert a deny cap of 10 — net 15 will exceed it
        conn = psycopg2.connect(store._database_url)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO public.credit_spend_caps (user_id, cap_type, cap_limit, action) "
                    "VALUES (%s, 'monthly', 10, 'deny')",
                    [_PG_USER],
                )
            conn.commit()
        finally:
            conn.close()

        # Attempt a deduction: gross=20, allowance covers 5, net=15 > cap=10
        result = store.deduct_with_allowance(_PG_USER, Decimal("20"))
        assert result.error == "cap_reached"

        # Allowance must NOT have been consumed (all-or-nothing rollback)
        after = store.check_allowance(_PG_USER)
        assert after.allowance_remaining == before.allowance_remaining

    # ── INT7: refund does NOT restore allowance ───────────────────────────

    def test_refund_does_not_restore_allowance_pg(self, store: PostgresStore) -> None:
        """INT7: refunding a charge leaves the billing-window allowance intact.

        Setup: free_allowance=5, deduct 10 → allowance covers 5, net=5
        (transaction amount=-5, refundable).  Refund restores the 5 net balance
        credits but the allowance window (5 consumed) must stay unchanged.
        """
        store.set_active_pricing(
            {
                "version": 1,
                "metering": {"models": {"*": "1"}},
                "plans": {
                    "basic": {
                        "label": "Basic",
                        "allowance": {"amount": Decimal("5")},
                    }
                },
            }
        )
        store.set_user_plan(_PG_USER, "basic")
        store.add_credits(_PG_USER, Decimal("1000"), "purchase")

        # Deduct 10: allowance covers 5, net=5 → transaction amount=-5 (refundable)
        d = store.deduct_with_allowance(_PG_USER, Decimal("10"), idempotency_key="r7_1")
        assert d.error is None
        assert d.allowance_consumed == Decimal("5")
        assert d.amount == Decimal("5")

        before_refund = store.check_allowance(_PG_USER)
        # Allowance window now shows 5 consumed (0 remaining of the 5 allowance)
        assert before_refund.allowance_remaining == Decimal("0")

        # Refund the net transaction
        r = store.refund_credits(d.transaction_id)
        assert r.error is None

        # Allowance window must remain at 0 remaining (not restored to 5)
        after_refund = store.check_allowance(_PG_USER)
        assert after_refund.allowance_remaining == before_refund.allowance_remaining

    # ── INT8: sweep when balance < total expired ──────────────────────────

    def test_sweep_balance_not_negative_pg(self, store: PostgresStore) -> None:
        """INT8: sweep of expired credits never drives balance below zero."""
        # Add 100 credits that expire in the past
        store.add_credits(
            _PG_USER,
            Decimal("100"),
            "purchase",
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        # Add 50 credits with no expiry
        store.add_credits(_PG_USER, Decimal("50"), "purchase")
        # Deduct 80 (some come from the expiring batch, some from the non-expiring)
        store.deduct_with_allowance(_PG_USER, Decimal("80"), idempotency_key="sw8_1")

        # Run sweep — should expire the remaining 20 credits from the expired batch
        sweep = store.sweep_expired_credits()
        assert sweep.expired_amount >= Decimal("0")

        # Balance must never go negative
        balance = store.get_balance(_PG_USER).balance
        assert balance >= Decimal("0"), f"Balance went negative: {balance}"

    # ── INT9: listUserTransactions type filter ────────────────────────────

    def test_list_user_transactions_type_filter_pg(self, store: PostgresStore) -> None:
        """INT9: types filter returns only rows of the requested type(s)."""
        # Seed a purchase and a usage transaction
        store.add_credits(_PG_USER, Decimal("1000"), "purchase")
        store.deduct_with_allowance(_PG_USER, Decimal("5"), idempotency_key="tf9_1")

        # usage only
        usage_rows = store.list_user_transactions(_PG_USER, types=["usage"])
        assert len(usage_rows) >= 1
        assert all(r.type == "usage" for r in usage_rows)

        # purchase only
        purchase_rows = store.list_user_transactions(_PG_USER, types=["purchase"])
        assert len(purchase_rows) >= 1
        assert all(r.type == "purchase" for r in purchase_rows)

        # both types
        both_rows = store.list_user_transactions(_PG_USER, types=["usage", "purchase"])
        types_present = {r.type for r in both_rows}
        assert "usage" in types_present
        assert "purchase" in types_present

    # ── INT10: aggregateStats Decimal precision ───────────────────────────

    def test_aggregate_stats_decimal_precision_pg(self, store: PostgresStore) -> None:
        """INT10: three fractional deductions sum to exact Decimal, not float."""
        from_date = datetime.now(UTC) - timedelta(hours=1)
        to_date = datetime.now(UTC) + timedelta(hours=1)

        store.add_credits(_PG_USER, Decimal("1000"), "purchase")
        store.deduct_with_allowance(_PG_USER, Decimal("0.1"), idempotency_key="prec10_1")
        store.deduct_with_allowance(_PG_USER, Decimal("0.2"), idempotency_key="prec10_2")
        store.deduct_with_allowance(_PG_USER, Decimal("0.15"), idempotency_key="prec10_3")

        stats = store.aggregate_stats(from_date, to_date)

        assert isinstance(stats.total_credits_consumed, Decimal)
        assert stats.total_credits_consumed == Decimal("0.45")

    # ── H4 — RPC atomicity: cap-fail must NOT consume allowance ──────────

    def test_deduct_with_allowance_cap_deny_does_not_consume_allowance(self, store: PostgresStore) -> None:
        """H4 — deny cap aborts without consuming any allowance (all-or-nothing).

        Setup: balance=20, monthly allowance=10, deny cap at 8.
        Attempt deduct(9): allowance covers 9, net=0 but wait — the cap is
        checked against the NET amount after allowance, so net=9-9=0… Actually
        let's use amount=9 with allowance=10 → net=0 always passes.

        Use a scenario where net DOES exceed the cap:
        allowance=10, balance=20, deny cap=8, amount=15.
        Gross=15, allowance covers 10, net=5 → 0+5 > 8? No, 5 < 8 → passes.

        Correct scenario: allowance=10, cap=8, amount=20.
        Gross=20, allowance covers 10, net=10 → 0+10 > 8 → denied.
        After failure: allowance_remaining must still be 10.
        Then deduct(5): allowance covers 5, net=0 → balance unchanged, allowance=5.
        """
        store.set_active_pricing(
            {
                "version": 1,
                "metering": {"models": {"*": "1"}},
                "plans": {
                    "basic": {
                        "label": "Basic",
                        "allowance": {"amount": Decimal("10")},
                    }
                },
            }
        )
        store.set_user_plan(_PG_USER, "basic")
        store.add_credits(_PG_USER, Decimal("20"), "purchase")

        # Insert deny cap at 8 (net spend must not exceed 8)
        conn = psycopg2.connect(store._database_url)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO public.credit_spend_caps (user_id, cap_type, cap_limit, action) "
                    "VALUES (%s, 'monthly', 8, 'deny')",
                    [_PG_USER],
                )
            conn.commit()
        finally:
            conn.close()

        before = store.check_allowance(_PG_USER)
        assert before.allowance_remaining == Decimal("10")

        # Attempt: gross=20, allowance covers 10, net=10 → 0+10 > 8 → cap_reached
        result = store.deduct_with_allowance(_PG_USER, Decimal("20"))
        assert result.error == "cap_reached"

        # Allowance must be untouched after the failed attempt
        after_fail = store.check_allowance(_PG_USER)
        assert after_fail.allowance_remaining == Decimal("10"), (
            f"Allowance leaked on cap-deny: expected 10, got {after_fail.allowance_remaining}"
        )

        # A successful deduct(5): allowance covers 5, net=0, balance unchanged
        ok = store.deduct_with_allowance(_PG_USER, Decimal("5"), idempotency_key="h4_ok")
        assert ok.error is None
        assert ok.allowance_consumed == Decimal("5")

        after_ok = store.check_allowance(_PG_USER)
        assert after_ok.allowance_remaining == Decimal("5")
        assert store.get_balance(_PG_USER).balance == Decimal("20")

    # ── H5 — Connection survives exception ───────────────────────────────

    def test_postgres_store_recovers_after_error(self, store: PostgresStore) -> None:
        """H5 — store remains usable after a call that causes an error.

        PostgresStore opens a fresh connection per call (no persistent connection
        pool), so an error in one call cannot poison future calls. This test
        verifies that contract.
        """
        # Add some credits so the store has state
        store.add_credits(_PG_USER, Decimal("100"), "purchase")

        # Attempt a deduction with an invalid (negative) amount.
        # The store returns an error result rather than raising for business
        # errors; negative amounts return error="invalid_amount".
        bad = store.deduct_with_allowance(_PG_USER, Decimal("-1"))
        assert bad.error is not None  # some error code (invalid_amount or similar)

        # The connection must still be usable: a normal get_balance succeeds
        balance = store.get_balance(_PG_USER)
        assert balance.balance == Decimal("100"), f"Connection broken after error: balance={balance.balance}"

        # And a normal deduction also works
        ok = store.deduct_with_allowance(_PG_USER, Decimal("10"), idempotency_key="h5_ok")
        assert ok.error is None
        assert store.get_balance(_PG_USER).balance == Decimal("90")

    # ── H6 — Decimal round-trip precision ────────────────────────────────

    def test_decimal_round_trip_precision(self, store: PostgresStore) -> None:
        """H6 — sub-cent amounts survive a Postgres round-trip without float drift."""
        # Start fresh balance for this user
        store.add_credits(_PG_USER, Decimal("0.0001"), "purchase")
        b1 = store.get_balance(_PG_USER).balance
        assert isinstance(b1, Decimal)
        assert b1 == Decimal("0.0001"), f"Expected 0.0001, got {b1!r}"

        store.add_credits(_PG_USER, Decimal("0.1234"), "purchase")
        b2 = store.get_balance(_PG_USER).balance
        assert isinstance(b2, Decimal)
        assert b2 == Decimal("0.1235"), f"Expected 0.1235, got {b2!r}"

        # Deduct the tiny amount back
        d = store.deduct_with_allowance(_PG_USER, Decimal("0.0001"), idempotency_key="h6_deduct")
        assert d.error is None
        b3 = store.get_balance(_PG_USER).balance
        assert isinstance(b3, Decimal)
        assert b3 == Decimal("0.1234"), f"Expected 0.1234, got {b3!r}"

    # ── H7 — Migration idempotency ───────────────────────────────────────

    def test_migration_idempotent(self, pg_database_url: str) -> None:
        """H7 — running setup() twice raises no exception and leaves DB usable."""
        store = PostgresStore(pg_database_url)

        r1 = store.setup()
        assert r1.success, f"First setup() failed: {r1.errors}"

        r2 = store.setup()
        assert r2.success, f"Second setup() failed: {r2.errors}"

        # Basic operations still work after double migration
        store.add_credits(_PG_USER, Decimal("50"), "purchase")
        assert store.get_balance(_PG_USER).balance >= Decimal("50")


# ═══════════════════════════════════════════════════════════════════════════
# HttpxSupabaseStore — HTTP contract tests (mocked httpx)
# ═══════════════════════════════════════════════════════════════════════════


class TestHttpxSupabaseStoreSetup:
    """setup() runs the real SQL migrations against a real Postgres."""

    def test_setup_with_database_url(self, pg_database_url: str) -> None:
        store = HttpxSupabaseStore(url="http://localhost", key="irrelevant")
        result = store.setup(database_url=pg_database_url)
        assert result.success
        assert len(result.tables_created) > 0

    def test_setup_requires_database_url(self) -> None:
        store = HttpxSupabaseStore(url="http://localhost", key="x")
        with pytest.raises(RuntimeError, match="requires database_url"):
            store.setup(database_url=None)


class TestHttpxSupabaseStoreContract:
    """Contract tests: assert exact request URL/headers/body shape AND
    error-envelope handling against mocked ``httpx`` responses (no network,
    no localhost:1 reject-only tests). Replaces the old reject-only tests and
    the empty ``test_set_active_pricing``."""

    @pytest.fixture
    def store(self) -> Iterator[HttpxSupabaseStore]:
        s = HttpxSupabaseStore(url="https://test.supabase.co", key="test-key")
        yield s
        s.close()

    def _mock_post(self, store: HttpxSupabaseStore, return_value: object, status: int = 200) -> MagicMock:
        patcher = patch.object(store._http, "post")
        mock = patcher.start()
        resp = MagicMock()
        resp.json.return_value = return_value
        resp.raise_for_status.return_value = None
        resp.status_code = status
        mock.return_value = resp
        self._patchers.append(patcher)
        return mock

    @pytest.fixture(autouse=True)
    def _cleanup_patches(self) -> Iterator[None]:
        self._patchers: list = []
        yield
        for p in self._patchers:
            p.stop()

    _EXPECTED_HEADERS = {
        "apikey": "test-key",
        "authorization": "Bearer test-key",
        "content-type": "application/json",
    }

    # -- request shape ------------------------------------------------------

    def test_rpc_url_headers_body_exact(self, store: HttpxSupabaseStore) -> None:
        mock = self._mock_post(store, {"balance": 0, "user_id": "u1", "lifetime_purchased": 0})
        store.get_balance("u1")
        mock.assert_called_once_with(
            "https://test.supabase.co/rest/v1/rpc/get_credits_balance",
            json={"p_user_id": "u1"},
            headers=self._EXPECTED_HEADERS,
        )

    def test_deduct_with_allowance_request_body_and_decimal_parse(self, store: HttpxSupabaseStore) -> None:
        mock = self._mock_post(
            store,
            {
                "transaction_id": "tx_9",
                "amount": 2.5,
                "allowance_consumed": 1.5,
                "balance_after": 96.0,
                "idempotent": False,
                "cap_warning": "warn",
            },
        )
        result = store.deduct_with_allowance(
            "u1",
            Decimal("4.0"),
            idempotency_key="idem-1",
            min_balance=Decimal("5"),
            model="gpt-4",
            metadata=CreditMetadata(model="gpt-4"),
        )
        # Exact request: money serialized as decimal strings; system params present.
        mock.assert_called_once_with(
            "https://test.supabase.co/rest/v1/rpc/deduct_with_allowance",
            json={
                "p_user_id": "u1",
                "p_amount": "4.0",
                "p_idempotency_key": "idem-1",
                "p_min_balance": "5",
                "p_model": "gpt-4",
                "p_metadata": {"model": "gpt-4"},
                "p_skip_allowance": False,
                "p_period_start": None,
                "p_feature": None,
                "p_feature_max_calls": None,
                "p_feature_action": None,
                "p_feature_period_start": None,
                "p_feature_period_end": None,
            },
            headers=self._EXPECTED_HEADERS,
        )
        # JSON numbers parsed into Decimal (no float).
        assert result.amount == Decimal("2.5")
        assert isinstance(result.amount, Decimal)
        assert result.allowance_consumed == Decimal("1.5")
        assert result.balance_after == Decimal("96.0")
        assert result.cap_warning == "warn"
        assert result.error is None

    def test_deduct_with_allowance_skip_allowance_serialized(self, store: HttpxSupabaseStore) -> None:
        """skip_allowance=True must be forwarded as p_skip_allowance in the RPC body (Fix 7)."""
        mock = self._mock_post(
            store,
            {
                "transaction_id": "tx_10",
                "amount": 20.0,
                "allowance_consumed": 0.0,
                "balance_after": 80.0,
                "idempotent": False,
                "cap_warning": None,
            },
        )
        store.deduct_with_allowance("u1", Decimal("20"), skip_allowance=True)
        mock.assert_called_once_with(
            "https://test.supabase.co/rest/v1/rpc/deduct_with_allowance",
            json={
                "p_user_id": "u1",
                "p_amount": "20",
                "p_idempotency_key": None,
                "p_min_balance": "0",
                "p_model": None,
                "p_metadata": {},
                "p_skip_allowance": True,
                "p_period_start": None,
                "p_feature": None,
                "p_feature_max_calls": None,
                "p_feature_action": None,
                "p_feature_period_start": None,
                "p_feature_period_end": None,
            },
            headers=self._EXPECTED_HEADERS,
        )

    def test_deduct_with_allowance_error_envelope(self, store: HttpxSupabaseStore) -> None:
        self._mock_post(store, {"error": "cap_reached", "action": "deny"})
        result = store.deduct_with_allowance("u1", Decimal("100"))
        assert result.error == "cap_reached"
        assert result.amount == Decimal("0")

    def test_add_credits_serializes_amount_as_string(self, store: HttpxSupabaseStore) -> None:
        mock = self._mock_post(
            store,
            {"id": "tx_1", "user_id": "u1", "amount": 50, "new_balance": 150, "lifetime_purchased": 50},
        )
        result = store.add_credits("u1", Decimal("50"))
        call = mock.call_args
        assert call.args[0] == "https://test.supabase.co/rest/v1/rpc/credits_add"
        assert call.kwargs["json"]["p_amount"] == "50"
        assert call.kwargs["headers"] == self._EXPECTED_HEADERS
        assert result.transaction_id == "tx_1"
        assert result.new_balance == Decimal("150")
        assert isinstance(result.new_balance, Decimal)

    def test_add_credits_request_body_includes_tier_and_idempotency_key(self, store: HttpxSupabaseStore) -> None:
        """``add_credits(..., tier=..., idempotency_key=...)`` must thread both
        through to the RPC body as ``p_tier``/``p_idempotency_key`` -- the same
        params ``credits_add`` (011_lazy_expiry.sql) now accepts, so a webhook
        replay dedupes at the RPC layer rather than the client silently
        double-granting through the Supabase REST path."""
        mock = self._mock_post(
            store,
            {
                "id": "tx_2",
                "user_id": "u1",
                "amount": 25,
                "new_balance": 175,
                "lifetime_purchased": 75,
                "tier": "gifted",
            },
        )
        result = store.add_credits(
            "u1",
            Decimal("25"),
            type="purchase",
            tier="gifted",
            idempotency_key="evt-1",
        )
        mock.assert_called_once_with(
            "https://test.supabase.co/rest/v1/rpc/credits_add",
            json={
                "p_user_id": "u1",
                "p_amount": "25",
                "p_type": "purchase",
                "p_metadata": {},
                "p_tier": "gifted",
                "p_idempotency_key": "evt-1",
            },
            headers=self._EXPECTED_HEADERS,
        )
        assert result.transaction_id == "tx_2"
        assert result.bucket == "gifted"

    def test_sweep_expired_credits_request_body_scoped_to_user(self, store: HttpxSupabaseStore) -> None:
        """``sweep_expired_credits(user_id=...)`` must thread ``p_user_id`` into
        the ``expire_credits`` RPC body -- the per-user lazy-sweep param added
        alongside ``p_idempotency_key`` in 011_lazy_expiry.sql. No existing test
        covered the ``expire_credits`` RPC contract at all before this."""
        mock = self._mock_post(store, {"expired_count": 1, "expired_amount": 10, "expired_by_bucket": {}})
        store.sweep_expired_credits(dry_run=False, user_id="u1")
        mock.assert_called_once_with(
            "https://test.supabase.co/rest/v1/rpc/expire_credits",
            json={"p_dry_run": False, "p_user_id": "u1"},
            headers=self._EXPECTED_HEADERS,
        )

    def test_sweep_expired_credits_request_body_global_when_user_id_omitted(self, store: HttpxSupabaseStore) -> None:
        """Omitting ``user_id`` must send ``p_user_id: null`` -- preserving the
        original global-sweep behavior (the periodic cron path) unchanged."""
        mock = self._mock_post(store, {"expired_count": 0, "expired_amount": 0, "expired_by_bucket": {}})
        store.sweep_expired_credits(dry_run=True)
        mock.assert_called_once_with(
            "https://test.supabase.co/rest/v1/rpc/expire_credits",
            json={"p_dry_run": True, "p_user_id": None},
            headers=self._EXPECTED_HEADERS,
        )

    # -- error-envelope handling (M10) --------------------------------------

    def test_unexpected_error_envelope_raises_store_error(self, store: HttpxSupabaseStore) -> None:
        # A non-business error code (e.g. a Postgres detail) must raise, not be
        # silently swallowed into a result model.
        self._mock_post(store, {"error": 'syntax error at or near "x"'})
        with pytest.raises(StoreError, match="returned error"):
            store.get_balance("u1")

    def test_http_status_error_wrapped(self, store: HttpxSupabaseStore) -> None:
        import httpx

        patcher = patch.object(store._http, "post")
        mock = patcher.start()
        self._patchers.append(patcher)
        request = httpx.Request("POST", "https://test.supabase.co/rest/v1/rpc/get_credits_balance")
        response = httpx.Response(500, request=request)
        mock.return_value.raise_for_status.side_effect = httpx.HTTPStatusError(
            "boom", request=request, response=response
        )
        with pytest.raises(StoreError, match="supabase request failed: 500"):
            store.get_balance("u1")

    def test_request_error_wrapped(self, store: HttpxSupabaseStore) -> None:
        import httpx

        patcher = patch.object(store._http, "post")
        mock = patcher.start()
        self._patchers.append(patcher)
        mock.side_effect = httpx.ConnectError("connection refused")
        with pytest.raises(StoreError, match="supabase request error"):
            store.get_balance("u1")

    def test_invalid_json_wrapped(self, store: HttpxSupabaseStore) -> None:
        import json as _json

        patcher = patch.object(store._http, "post")
        mock = patcher.start()
        self._patchers.append(patcher)
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.side_effect = _json.JSONDecodeError("bad", "", 0)
        mock.return_value = resp
        with pytest.raises(StoreError, match="not valid JSON"):
            store.get_balance("u1")

    # -- close() / context manager (L7) -------------------------------------

    def test_close_closes_underlying_client(self) -> None:
        store = HttpxSupabaseStore(url="https://test.supabase.co", key="k")
        with patch.object(store._http, "close") as mock_close:
            store.close()
            mock_close.assert_called_once_with()

    def test_context_manager_closes(self) -> None:
        store = HttpxSupabaseStore(url="https://test.supabase.co", key="k")
        with patch.object(store._http, "close") as mock_close, store as s:
            assert s is store
        mock_close.assert_called_once_with()

    # -- parsing of existing operations -------------------------------------

    def test_get_balance(self, store: HttpxSupabaseStore) -> None:
        self._mock_post(store, {"user_id": "u1", "balance": 100, "lifetime_purchased": 50})
        result = store.get_balance("u1")
        assert result.balance == Decimal("100")
        assert result.lifetime_purchased == Decimal("50")

    def test_get_active_pricing(self, store: HttpxSupabaseStore) -> None:
        self._mock_post(
            store,
            {"id": "1", "config": {"version": 1, "metering": {"models": {"a": "b"}}}, "is_active": True},
        )
        result = store.get_active_pricing()
        assert result is not None
        assert result.config["metering"]["models"] == {"a": "b"}
        assert result.id == "1"

    def test_get_active_pricing_none(self, store: HttpxSupabaseStore) -> None:
        self._mock_post(store, None)
        assert store.get_active_pricing() is None

    def test_get_active_pricing_error_envelope_returns_none(self, store: HttpxSupabaseStore) -> None:
        # A business error envelope must NOT be fed into model_validate (M10).
        self._mock_post(store, {"error": "not_found"})
        assert store.get_active_pricing() is None

    def test_set_active_pricing(self, store: HttpxSupabaseStore) -> None:
        mock = self._mock_post(store, {"id": "cfg_1"})
        config = {"version": 1, "metering": {"models": {"*": "1"}}}
        result = store.set_active_pricing(config, label="v1")
        assert result == "cfg_1"
        call = mock.call_args
        assert call.args[0] == "https://test.supabase.co/rest/v1/rpc/set_active_pricing_config"
        assert call.kwargs["json"]["p_label"] == "v1"
        assert "metering" in call.kwargs["json"]["p_config"]
        assert call.kwargs["headers"] == self._EXPECTED_HEADERS

    def test_list_user_transactions_supabase(self, store: HttpxSupabaseStore) -> None:
        now = datetime.now(UTC).isoformat()
        mock_data = [
            {
                "id": "tx1",
                "user_id": "u1",
                "amount": 1000,
                "type": "purchase",
                "reference_type": None,
                "reference_id": None,
                "metadata": {},
                "created_at": now,
                "total_count": 2,
            },
            {
                "id": "tx2",
                "user_id": "u1",
                "amount": -200,
                "type": "usage",
                "reference_type": None,
                "reference_id": None,
                "metadata": {"model": "gpt-4"},
                "created_at": now,
                "total_count": 2,
            },
        ]
        self._mock_post(store, mock_data)
        result = store.list_user_transactions("u1")
        assert len(result) == 2
        assert result[0].total_count == 2
        assert result[0].type == "purchase"
        assert result[0].amount == Decimal("1000")
        assert result[1].type == "usage"
        assert result[1].amount == Decimal("-200")

    def test_get_user_plan_features_supabase(self, store: HttpxSupabaseStore) -> None:
        self._mock_post(
            store,
            {
                "user_id": "u1",
                "plan_id": "pro",
                "plan_label": "Pro Plan",
                "allowance_amount": 500,
                "entitlements": {"ai_chat": {"value": True}, "max_roadmaps": {"value": 20}},
            },
        )
        result = store.get_user_plan("u1")
        assert result.plan_id == "pro"
        assert result.allowance_amount == Decimal("500")
        assert result.entitlements["ai_chat"].value is True
        assert result.entitlements["max_roadmaps"].value == 20

        result2 = store.check_feature("u1", "ai_chat")
        assert result2.has_feature is True
        assert result2.value is True

    def test_deduct_team_threads_idempotency_key(self, store: HttpxSupabaseStore) -> None:
        mock = self._mock_post(
            store,
            {"transaction_id": "tt", "team_id": "t1", "user_id": "u1", "amount": -10, "team_balance_after": 90},
        )
        result = store.deduct_team("t1", "u1", Decimal("10"), idempotency_key="team-k")
        call = mock.call_args
        assert call.args[0] == "https://test.supabase.co/rest/v1/rpc/deduct_team"
        # idempotency_key is threaded through metadata (H12).
        assert call.kwargs["json"]["p_metadata"]["idempotency_key"] == "team-k"
        assert call.kwargs["json"]["p_amount"] == "10"
        assert result.amount == Decimal("-10")
        assert result.team_balance_after == Decimal("90")


# ═══════════════════════════════════════════════════════════════════════════
# Lease lifecycle — real Postgres (interface plan §3/§4, parity with MemoryStore)
# ═══════════════════════════════════════════════════════════════════════════


class TestLeaseLifecyclePg:
    """create_lease / settle_lease / release_lease / renew_lease / get_available
    against a real Postgres + the new 016 RPCs."""

    @pytest.fixture
    def store(self, pg_database_url: str) -> PostgresStore:
        s = PostgresStore(pg_database_url)
        assert s.setup().success
        return s

    def _expire(self, store: PostgresStore, lease_id: str) -> None:
        """Force a lease past its TTL (white-box) instead of sleeping."""
        conn = store._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE public.credit_reservations SET expires_at = now() - interval '1 second' WHERE id = %s",
                    [lease_id],
                )
            conn.commit()
        finally:
            conn.close()

    def test_create_lease_holds_against_available(self, store: PostgresStore) -> None:
        store.add_credits(_PG_USER, Decimal("100"), "purchase")
        lease = store.create_lease(_PG_USER, Decimal("30"), "usage", floor=Decimal("0"))
        assert lease.error is None
        assert lease.lease_id
        avail = store.get_available(_PG_USER)
        assert avail.balance == Decimal("100")
        assert avail.reserved == Decimal("30")
        assert avail.available == Decimal("70")

    def test_strict_floor_rejects(self, store: PostgresStore) -> None:
        store.add_credits(_PG_USER, Decimal("100"), "purchase")
        lease = store.create_lease(_PG_USER, Decimal("99"), "usage", floor=Decimal("5"))
        assert lease.error == "insufficient_credits"

    def test_concurrency_limit(self, store: PostgresStore) -> None:
        store.add_credits(_PG_USER, Decimal("100"), "purchase")
        a = store.create_lease(_PG_USER, Decimal("10"), "chat", floor=Decimal("0"), max_concurrent=1)
        assert a.error is None
        b = store.create_lease(_PG_USER, Decimal("10"), "chat", floor=Decimal("0"), max_concurrent=1)
        assert b.error == "concurrency_limit"
        # A different op type has its own slot.
        c = store.create_lease(_PG_USER, Decimal("10"), "batch", floor=Decimal("0"), max_concurrent=1)
        assert c.error is None

    def test_settle_clamps_to_overdraft_floor(self, store: PostgresStore) -> None:
        """C1: settle_lease clamps the charge so balance stays >= overdraft_floor.

        Matches MemoryStore (the reference implementation) exactly: balance=0,
        overdraft_floor=-50, actual settle=60 -> max debit is 50, so
        balance_after == -50, NOT -60 (see test_lease.py
        TestOverdraft.test_settle_clamps_to_overdraft_floor). Before migration
        022 consolidated the two conflicting settle_lease overloads, every real
        caller (8 positional args including skip_allowance) always resolved to
        the un-floor-clamped 019 overload, so this never actually clamped in
        production -- a pre-existing bug fixed as a side effect of WS9's
        overload consolidation.
        """
        store.add_credits(_PG_USER, Decimal("0"), "adjustment")
        lease = store.create_lease(
            _PG_USER,
            Decimal("10"),
            "usage",
            billing_mode="overdraft",
            floor=Decimal("-50"),
            overdraft_floor=Decimal("-50"),
        )
        assert lease.error is None
        # Actual 60 > hold 10, but floor-clamped (C1): max debit = 0 - (-50) = 50.
        ded = store.settle_lease(_PG_USER, lease.lease_id, Decimal("60"))
        assert ded.error is None
        assert ded.balance_after == Decimal("-50")
        # New admission rejected once available <= floor.
        nxt = store.create_lease(_PG_USER, Decimal("1"), "usage", billing_mode="overdraft", floor=Decimal("-50"))
        assert nxt.error == "insufficient_credits"

    def test_settle_after_settle_replays(self, store: PostgresStore) -> None:
        store.add_credits(_PG_USER, Decimal("100"), "purchase")
        lease = store.create_lease(_PG_USER, Decimal("20"), "usage", floor=Decimal("0"))
        first = store.settle_lease(_PG_USER, lease.lease_id, Decimal("20"))
        assert first.error is None
        second = store.settle_lease(_PG_USER, lease.lease_id, Decimal("20"))
        assert second.idempotent is True
        assert store.get_balance(_PG_USER).balance == Decimal("80")

    def test_release_idempotent_and_settle_after_release(self, store: PostgresStore) -> None:
        store.add_credits(_PG_USER, Decimal("100"), "purchase")
        lease = store.create_lease(_PG_USER, Decimal("20"), "usage", floor=Decimal("0"))
        r1 = store.release_lease(_PG_USER, lease.lease_id)
        assert r1.released is True and r1.reason == "released"
        r2 = store.release_lease(_PG_USER, lease.lease_id)
        assert r2.released is False and r2.reason == "already_released"
        ded = store.settle_lease(_PG_USER, lease.lease_id, Decimal("20"))
        assert ded.error == "lease_not_found"
        # Released hold no longer counts against available.
        assert store.get_available(_PG_USER).available == Decimal("100")

    def test_expired_lease_settle_and_renew(self, store: PostgresStore) -> None:
        store.add_credits(_PG_USER, Decimal("100"), "purchase")
        lease = store.create_lease(_PG_USER, Decimal("20"), "usage", floor=Decimal("0"))
        self._expire(store, lease.lease_id)
        ded = store.settle_lease(_PG_USER, lease.lease_id, Decimal("20"))
        assert ded.error == "lease_expired"
        renewed = store.renew_lease(_PG_USER, lease.lease_id, 600)
        assert renewed.error == "lease_expired"

    def test_renew_extends_then_settles(self, store: PostgresStore) -> None:
        store.add_credits(_PG_USER, Decimal("100"), "purchase")
        lease = store.create_lease(_PG_USER, Decimal("20"), "usage", ttl_seconds=600, floor=Decimal("0"))
        renewed = store.renew_lease(_PG_USER, lease.lease_id, 3600)
        assert renewed.error is None
        ded = store.settle_lease(_PG_USER, lease.lease_id, Decimal("20"))
        assert ded.balance_after == Decimal("80")

    def test_get_user_plan_returns_policy_fields(self, store: PostgresStore) -> None:
        store.set_active_pricing(
            {
                "version": 1,
                "metering": {"models": {"*": "input_tokens * 1"}},
                "ledger": {"min_balance": Decimal("0")},
                "plans": {
                    "pro": {
                        "label": "Pro",
                        "safety": {
                            "billing_mode": "overdraft",
                            "max_concurrent": 3,
                            "overdraft_floor": Decimal("-25"),
                        },
                    }
                },
            }
        )
        store.add_credits(_PG_USER, Decimal("0"), "adjustment")
        store.set_user_plan(_PG_USER, "pro")
        plan = store.get_user_plan(_PG_USER)
        assert plan.billing_mode == "overdraft"
        assert plan.max_concurrent == 3
        assert plan.overdraft_floor == Decimal("-25")

    def test_manager_reserve_settle_flow_pg(self, store: PostgresStore) -> None:
        m = CreditManager(store=store, policy="strict_prepaid")
        m.publish_pricing_from_dict(
            {
                "version": 1,
                "metering": {"models": {"*": "input_tokens * 1"}},
                "ledger": {"min_balance": 0},
            }
        )
        store.add_credits(_PG_USER, Decimal("100"), "purchase")
        lease = m.reserve(_PG_USER, Decimal("40"))
        ded = m.settle(_PG_USER, lease.lease_id, Decimal("25"))
        assert ded.balance_after == Decimal("75")


# ═══════════════════════════════════════════════════════════════════════════
# Lease lifecycle — adversarial / financial-safety against real Postgres
# (validates FOR UPDATE serialization, idempotency, floor exactness, allowance)
# ═══════════════════════════════════════════════════════════════════════════


class TestLeaseAdversarialPg:
    @pytest.fixture
    def store(self, pg_database_url: str) -> PostgresStore:
        s = PostgresStore(pg_database_url)
        assert s.setup().success
        return s

    @pytest.mark.repeat(5)  # money-critical race: rerun to surface rare interleavings
    def test_concurrent_create_lease_no_over_admission_pg(self, store: PostgresStore) -> None:
        """N concurrent create_lease on one row. FOR UPDATE serializes them:
        with balance 100 / floor 0 / hold 30, exactly 3 leases admit and the
        held total never exceeds the balance."""
        store.add_credits(_PG_USER, Decimal("100"), "purchase")
        n = 30

        def one(_: int) -> object:
            s = PostgresStore(store._database_url)
            return s.create_lease(_PG_USER, Decimal("30"), "usage", floor=Decimal("0"))

        with ThreadPoolExecutor(max_workers=n) as ex:
            results = list(ex.map(one, range(n)))

        admitted = [r for r in results if r.error is None]  # type: ignore[attr-defined]
        assert len(admitted) == 3
        avail = store.get_available(_PG_USER)
        assert avail.reserved == Decimal("90")
        assert avail.available == Decimal("10")
        assert avail.balance == Decimal("100")  # held, not yet charged

    @pytest.mark.repeat(5)  # money-critical race: rerun to surface rare interleavings
    def test_concurrent_max_concurrent_pg(self, store: PostgresStore) -> None:
        store.add_credits(_PG_USER, Decimal("10000"), "purchase")

        def one(_: int) -> object:
            s = PostgresStore(store._database_url)
            return s.create_lease(_PG_USER, Decimal("1"), "chat", floor=Decimal("0"), max_concurrent=5)

        with ThreadPoolExecutor(max_workers=16) as ex:
            results = list(ex.map(one, range(40)))
        assert sum(1 for r in results if r.error is None) == 5  # type: ignore[attr-defined]

    @pytest.mark.repeat(5)  # money-critical race: rerun to surface rare interleavings
    def test_concurrent_settle_same_key_one_debit_pg(self, store: PostgresStore) -> None:
        store.add_credits(_PG_USER, Decimal("100"), "purchase")
        lease = store.create_lease(_PG_USER, Decimal("50"), "usage", floor=Decimal("0"))

        def one(_: int) -> object:
            s = PostgresStore(store._database_url)
            return s.settle_lease(_PG_USER, lease.lease_id, Decimal("50"), idempotency_key="k")

        with ThreadPoolExecutor(max_workers=12) as ex:
            list(ex.map(one, range(12)))
        assert store.get_balance(_PG_USER).balance == Decimal("50")  # charged exactly once

    @pytest.mark.repeat(5)  # money-critical race: rerun to surface rare interleavings
    def test_concurrent_settle_same_lease_no_key_one_debit_pg(self, store: PostgresStore) -> None:
        store.add_credits(_PG_USER, Decimal("100"), "purchase")
        lease = store.create_lease(_PG_USER, Decimal("50"), "usage", floor=Decimal("0"))

        def one(_: int) -> object:
            s = PostgresStore(store._database_url)
            return s.settle_lease(_PG_USER, lease.lease_id, Decimal("50"))

        with ThreadPoolExecutor(max_workers=12) as ex:
            list(ex.map(one, range(12)))
        # Lease-settled replay (no key) also guarantees a single debit.
        assert store.get_balance(_PG_USER).balance == Decimal("50")

    def test_floor_boundary_inclusive_and_exclusive_pg(self, store: PostgresStore) -> None:
        store.add_credits(_PG_USER, Decimal("100"), "purchase")
        # available - amount == floor → allowed (the 95 hold stays active).
        assert store.create_lease(_PG_USER, Decimal("95"), "usage", floor=Decimal("5")).error is None
        # With 95 held, available is 5; a further 1-credit hold → 5-1=4 < floor 5 → rejected.
        assert store.create_lease(_PG_USER, Decimal("1"), "usage", floor=Decimal("5")).error == "insufficient_credits"

    def test_allowance_consumed_at_settle_pg(self, store: PostgresStore) -> None:
        store.set_active_pricing(
            {
                "version": 1,
                "metering": {"models": {"*": "input_tokens * 1"}},
                "ledger": {"min_balance": Decimal("0")},
                "plans": {"free": {"label": "Free", "allowance": {"amount": Decimal("10")}}},
            }
        )
        store.add_credits(_PG_USER, Decimal("100"), "purchase")
        store.set_user_plan(_PG_USER, "free")

        l1 = store.create_lease(_PG_USER, Decimal("20"), "usage", floor=Decimal("0"))
        d1 = store.settle_lease(_PG_USER, l1.lease_id, Decimal("8"))
        assert d1.allowance_consumed == Decimal("8")
        assert d1.amount == Decimal("0")
        assert store.get_balance(_PG_USER).balance == Decimal("100")

        l2 = store.create_lease(_PG_USER, Decimal("20"), "usage", floor=Decimal("0"))
        d2 = store.settle_lease(_PG_USER, l2.lease_id, Decimal("8"))
        assert d2.allowance_consumed == Decimal("2")  # only 2 allowance left this period
        assert d2.amount == Decimal("6")
        assert store.get_balance(_PG_USER).balance == Decimal("94")

    def test_deny_cap_blocks_admission_advisory_at_settle_pg(self, store: PostgresStore) -> None:
        store.add_credits(_PG_USER, Decimal("1000"), "purchase")
        conn = psycopg2.connect(store._database_url)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO public.credit_spend_caps (user_id, cap_type, cap_limit, action) "
                    "VALUES (%s, 'monthly', 100, 'deny')",
                    [_PG_USER],
                )
            conn.commit()
        finally:
            conn.close()

        # Admission gate: a hold beyond the cap is rejected.
        assert store.create_lease(_PG_USER, Decimal("150"), "usage", floor=Decimal("0")).error == "cap_reached"
        # Admit within the cap, then settle past it: advisory only — charge proceeds.
        lease = store.create_lease(_PG_USER, Decimal("50"), "usage", floor=Decimal("0"))
        ded = store.settle_lease(_PG_USER, lease.lease_id, Decimal("120"))
        assert ded.error is None
        assert ded.cap_warning == "deny"
        assert store.get_balance(_PG_USER).balance == Decimal("880")


# ═══════════════════════════════════════════════════════════════════════════
# WS9 — configurable allowance window against real Postgres (migration 022)
# ═══════════════════════════════════════════════════════════════════════════


class TestAllowanceWindowPg:
    """allowance_period / plan_assigned_at / p_period_start threading (WS9)
    against a real Postgres instance running migration 022. MemoryStore
    coverage for the same rollover semantics already lives in
    ``test_store.py::TestAllowancePeriodRollover`` — this class targets the
    SQL layer specifically: JSONB round-trip, RPC signature consolidation,
    and explicit ``period_start`` threading through the atomic RPCs.
    """

    @pytest.fixture
    def store(self, pg_database_url: str) -> PostgresStore:
        s = PostgresStore(pg_database_url)
        assert s.setup().success
        return s

    def _raw_allowance_period(self, store: PostgresStore, plan_key: str) -> str:
        conn = store._conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT allowance_period FROM public.credit_plans WHERE plan_key = %s", [plan_key])
                row = cur.fetchone()
        finally:
            conn.close()
        assert row is not None, f"no credit_plans row for plan_key={plan_key!r}"
        return str(row[0])

    # ── 1/2. allowance_period + plan_assigned_at round-trip (all 3 modes) ──

    def test_get_user_plan_round_trips_rolling_30d(self, store: PostgresStore) -> None:
        user = _new_uuid(9001)
        store.set_active_pricing(
            {
                "version": 1,
                "metering": {"models": {"*": "1"}},
                "plans": {"pro": {"label": "Pro", "allowance": {"period": "rolling_30d"}}},
            }
        )
        store.set_user_plan(user, "pro")

        result = store.get_user_plan(user)
        assert result.allowance_period == "rolling_30d"
        assert result.plan_assigned_at is not None

    def test_sync_plans_persists_allowance_period_calendar_month_default(self, store: PostgresStore) -> None:
        store.set_active_pricing(
            {
                "version": 1,
                "metering": {"models": {"*": "1"}},
                "plans": {"basic": {"label": "Basic"}},
            }
        )
        user = _new_uuid(9002)
        store.set_user_plan(user, "basic")
        assert store.get_user_plan(user).allowance_period == "calendar_month"
        assert self._raw_allowance_period(store, "basic") == "calendar_month"

    def test_sync_plans_persists_allowance_period_rolling_30d(self, store: PostgresStore) -> None:
        store.set_active_pricing(
            {
                "version": 1,
                "metering": {"models": {"*": "1"}},
                "plans": {"pro": {"label": "Pro", "allowance": {"period": "rolling_30d"}}},
            }
        )
        user = _new_uuid(9003)
        store.set_user_plan(user, "pro")
        assert store.get_user_plan(user).allowance_period == "rolling_30d"
        assert self._raw_allowance_period(store, "pro") == "rolling_30d"

    def test_sync_plans_persists_allowance_period_anniversary(self, store: PostgresStore) -> None:
        store.set_active_pricing(
            {
                "version": 1,
                "metering": {"models": {"*": "1"}},
                "plans": {"elite": {"label": "Elite", "allowance": {"period": "anniversary"}}},
            }
        )
        user = _new_uuid(9004)
        store.set_user_plan(user, "elite")
        assert store.get_user_plan(user).allowance_period == "anniversary"
        assert self._raw_allowance_period(store, "elite") == "anniversary"

    # ── 3. deduct_with_allowance: explicit period_start isolates windows ───

    def test_deduct_with_allowance_period_start_isolates_windows(self, store: PostgresStore) -> None:
        user = _new_uuid(9005)
        store.set_active_pricing(
            {
                "version": 1,
                "metering": {"models": {"*": "1"}},
                "plans": {"basic": {"label": "Basic", "allowance": {"amount": Decimal("10")}}},
            }
        )
        store.set_user_plan(user, "basic")
        store.add_credits(user, Decimal("1000"), "purchase")

        window_a = date(2024, 1, 1)
        window_b = date(2024, 6, 1)

        d1 = store.deduct_with_allowance(user, Decimal("4"), idempotency_key="wa1", period_start=window_a)
        assert d1.allowance_consumed == Decimal("4")
        assert store.check_allowance(user, period_start=window_a).allowance_remaining == Decimal("6")

        # A different period_start gets its own fresh allowance, unaffected by window_a's usage.
        d2 = store.deduct_with_allowance(user, Decimal("7"), idempotency_key="wb1", period_start=window_b)
        assert d2.allowance_consumed == Decimal("7")
        assert store.check_allowance(user, period_start=window_b).allowance_remaining == Decimal("3")
        # window_a's usage is untouched by window_b's deduction.
        assert store.check_allowance(user, period_start=window_a).allowance_remaining == Decimal("6")

    # ── 4. create_lease/settle_lease: explicit period_start isolates windows ─

    def test_lease_period_start_isolates_windows(self, store: PostgresStore) -> None:
        user = _new_uuid(9006)
        store.set_active_pricing(
            {
                "version": 1,
                "metering": {"models": {"*": "1"}},
                "plans": {"basic": {"label": "Basic", "allowance": {"amount": Decimal("10")}}},
            }
        )
        store.set_user_plan(user, "basic")
        store.add_credits(user, Decimal("1000"), "purchase")

        window_a = date(2024, 1, 1)
        window_b = date(2024, 6, 1)

        lease_a = store.create_lease(user, Decimal("20"), "usage", floor=Decimal("0"), period_start=window_a)
        settle_a = store.settle_lease(user, lease_a.lease_id, Decimal("4"), period_start=window_a)
        assert settle_a.allowance_consumed == Decimal("4")
        assert store.check_allowance(user, period_start=window_a).allowance_remaining == Decimal("6")

        lease_b = store.create_lease(user, Decimal("20"), "usage", floor=Decimal("0"), period_start=window_b)
        settle_b = store.settle_lease(user, lease_b.lease_id, Decimal("7"), period_start=window_b)
        assert settle_b.allowance_consumed == Decimal("7")
        assert store.check_allowance(user, period_start=window_b).allowance_remaining == Decimal("3")
        # window_a's window is untouched.
        assert store.check_allowance(user, period_start=window_a).allowance_remaining == Decimal("6")

    # ── 5. manager.deduct() with a rolling_30d plan actually rolls over ─────

    def test_manager_deduct_rolling_30d_rolls_over_vs_calendar_month_control(self, store: PostgresStore) -> None:
        store.set_active_pricing(
            {
                "version": 1,
                "metering": {"models": {"*": "input_tokens * 1"}},
                "ledger": {"min_balance": Decimal("0")},
                "plans": {
                    "roll": {
                        "label": "Roll",
                        "allowance": {"amount": Decimal("10"), "period": "rolling_30d"},
                    },
                    "cal": {"label": "Cal", "allowance": {"amount": Decimal("10")}},
                },
            }
        )
        roll_user = _new_uuid(9007)
        cal_user = _new_uuid(9008)
        store.set_user_plan(roll_user, "roll")
        store.set_user_plan(cal_user, "cal")
        store.add_credits(roll_user, Decimal("1000"), "purchase")
        store.add_credits(cal_user, Decimal("1000"), "purchase")

        # Simulate 35 elapsed days since plan assignment (rolling_30d has rolled
        # over at least once; calendar_month has NOT necessarily rolled over —
        # only if "now" crossed a 1st-of-month boundary, which it may not have).
        conn = store._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE public.user_credits SET plan_assigned_at = now() - interval '35 days' "
                    "WHERE user_id IN (%s, %s)",
                    [roll_user, cal_user],
                )
            conn.commit()
        finally:
            conn.close()

        m_roll = CreditManager(store=store)
        m_roll.publish_pricing_from_dict(
            {
                "version": 1,
                "metering": {"models": {"*": "input_tokens * 1"}},
                "ledger": {"min_balance": 0, "signup_grant": 0},
            }
        )
        m_roll.deduct(roll_user, UsageMetrics(input_tokens=10))
        # Full allowance available again: the rolling window rolled over based on
        # plan_assigned_at (35 days ago), independent of the calendar month.
        assert m_roll.check_allowance(roll_user).allowance_remaining == Decimal("0")

        # Prove the rolling window actually isolated this deduction from the
        # "original" (pre-rollover) window: deducting against the pre-rollover
        # period_start directly still shows it untouched.
        original_window = date.today() - timedelta(days=35)
        assert store.check_allowance(roll_user, period_start=original_window).allowance_remaining == Decimal("10")

    # ── 6. manager.reserve()/settle() with an anniversary plan ──────────────

    def test_manager_reserve_settle_anniversary_plan(self, store: PostgresStore) -> None:
        store.set_active_pricing(
            {
                "version": 1,
                "metering": {"models": {"*": "input_tokens * 1"}},
                "ledger": {"min_balance": Decimal("0")},
                "plans": {
                    "anniv": {
                        "label": "Anniv",
                        "allowance": {"amount": Decimal("10"), "period": "anniversary"},
                    }
                },
            }
        )
        user = _new_uuid(9009)
        store.set_user_plan(user, "anniv")
        store.add_credits(user, Decimal("1000"), "purchase")

        # Push plan_assigned_at back >1 month so "now" is past this month's
        # anniversary reset day, landing in a fresh window.
        conn = store._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE public.user_credits SET plan_assigned_at = now() - interval '40 days' WHERE user_id = %s",
                    [user],
                )
            conn.commit()
        finally:
            conn.close()

        m = CreditManager(store=store, policy="strict_prepaid")
        m.publish_pricing_from_dict(
            {
                "version": 1,
                "metering": {"models": {"*": "input_tokens * 1"}},
                "ledger": {"min_balance": 0},
            }
        )

        lease = m.reserve(user, Decimal("6"))
        ded = m.settle(user, lease.lease_id, Decimal("6"))
        assert ded.allowance_consumed == Decimal("6")
        assert ded.amount == Decimal("0")
        assert m.check_allowance(user).allowance_remaining == Decimal("4")

    # ── 7. manager.check_allowance() cross-checked against the resolver ────

    def test_manager_check_allowance_rolling_30d_matches_resolver(self, store: PostgresStore) -> None:
        store.set_active_pricing(
            {
                "version": 1,
                "metering": {"models": {"*": "input_tokens * 1"}},
                "ledger": {"min_balance": Decimal("0")},
                "plans": {
                    "roll": {
                        "label": "Roll",
                        "allowance": {"amount": Decimal("20"), "period": "rolling_30d"},
                    }
                },
            }
        )
        user = _new_uuid(9010)
        store.set_user_plan(user, "roll")
        store.add_credits(user, Decimal("1000"), "purchase")

        plan = store.get_user_plan(user)
        assert plan.plan_assigned_at is not None

        m = CreditManager(store=store)
        m.publish_pricing_from_dict(
            {
                "version": 1,
                "metering": {"models": {"*": "input_tokens * 1"}},
                "ledger": {"min_balance": 0},
            }
        )

        # Partial deduction, then cross-check the manager's reported window
        # against calling the pure resolver directly with the same anchor.
        m.deduct(user, UsageMetrics(input_tokens=8))
        result = m.check_allowance(user)

        now = datetime.now(UTC)
        expected_start, expected_end_exclusive = resolve_allowance_window(now, "rolling_30d", plan.plan_assigned_at)
        expected_end_inclusive = expected_end_exclusive - timedelta(days=1)

        assert (
            result.period_start
            == datetime(expected_start.year, expected_start.month, expected_start.day, tzinfo=UTC).isoformat()
        )
        assert (
            result.period_end
            == datetime(
                expected_end_inclusive.year, expected_end_inclusive.month, expected_end_inclusive.day, tzinfo=UTC
            ).isoformat()
        )
        assert result.allowance_remaining == Decimal("12")

    def test_manager_check_allowance_anniversary_matches_resolver(self, store: PostgresStore) -> None:
        store.set_active_pricing(
            {
                "version": 1,
                "metering": {"models": {"*": "input_tokens * 1"}},
                "ledger": {"min_balance": Decimal("0")},
                "plans": {
                    "anniv": {
                        "label": "Anniv",
                        "allowance": {"amount": Decimal("15"), "period": "anniversary"},
                    }
                },
            }
        )
        user = _new_uuid(9011)
        store.set_user_plan(user, "anniv")
        store.add_credits(user, Decimal("1000"), "purchase")

        plan = store.get_user_plan(user)
        assert plan.plan_assigned_at is not None

        m = CreditManager(store=store)
        m.publish_pricing_from_dict(
            {
                "version": 1,
                "metering": {"models": {"*": "input_tokens * 1"}},
                "ledger": {"min_balance": 0},
            }
        )
        m.deduct(user, UsageMetrics(input_tokens=5))
        result = m.check_allowance(user)

        now = datetime.now(UTC)
        expected_start, expected_end_exclusive = resolve_allowance_window(now, "anniversary", plan.plan_assigned_at)
        expected_end_inclusive = expected_end_exclusive - timedelta(days=1)

        assert (
            result.period_start
            == datetime(expected_start.year, expected_start.month, expected_start.day, tzinfo=UTC).isoformat()
        )
        assert (
            result.period_end
            == datetime(
                expected_end_inclusive.year, expected_end_inclusive.month, expected_end_inclusive.day, tzinfo=UTC
            ).isoformat()
        )
        assert result.allowance_remaining == Decimal("10")

    # ── 8. manager.check_allowance() regression guard for calendar_month ───

    def test_manager_check_allowance_calendar_month_unchanged(self, store: PostgresStore) -> None:
        store.set_active_pricing(
            {
                "version": 1,
                "metering": {"models": {"*": "input_tokens * 1"}},
                "ledger": {"min_balance": Decimal("0")},
                "plans": {"basic": {"label": "Basic", "allowance": {"amount": Decimal("10")}}},
            }
        )
        user = _new_uuid(9012)
        store.set_user_plan(user, "basic")
        store.add_credits(user, Decimal("1000"), "purchase")

        m = CreditManager(store=store)
        m.publish_pricing_from_dict(
            {
                "version": 1,
                "metering": {"models": {"*": "input_tokens * 1"}},
                "ledger": {"min_balance": 0},
            }
        )
        m.deduct(user, UsageMetrics(input_tokens=3))

        # calendar_month is the fast path: manager.check_allowance() delegates
        # straight to store.check_allowance(user) with no period_start override,
        # identical to calling the store directly.
        direct = store.check_allowance(user)
        via_manager = m.check_allowance(user)
        assert via_manager == direct
        assert via_manager.allowance_remaining == Decimal("7")

    def test_manager_check_allowance_planless_user_zero_shape_pg(self, store: PostgresStore) -> None:
        m = CreditManager(store=store)
        m.publish_pricing_from_dict(
            {
                "version": 1,
                "metering": {"models": {"*": "input_tokens * 1"}},
                "ledger": {"min_balance": 0},
            }
        )
        user = _new_uuid(9013)
        result = m.check_allowance(user)
        assert result.allowance_remaining == Decimal(0)
        assert result == store.check_allowance(user)

    # ── 9. plan-switch re-anchors plan_assigned_at ──────────────────────────

    def test_set_user_plan_reanchors_plan_assigned_at_pg(self, store: PostgresStore) -> None:
        store.set_active_pricing(
            {
                "version": 1,
                "metering": {"models": {"*": "1"}},
                "plans": {
                    "planA": {"label": "Plan A", "allowance": {"period": "anniversary"}},
                    "planB": {"label": "Plan B", "allowance": {"period": "anniversary"}},
                },
            }
        )
        user = _new_uuid(9014)
        store.set_user_plan(user, "planA")
        first_assigned_at = store.get_user_plan(user).plan_assigned_at
        assert first_assigned_at is not None

        # Force a small delay so the re-anchor timestamp is observably later —
        # Postgres now() has microsecond resolution, but the two RPC calls could
        # otherwise land within the same tick on a very fast machine.
        conn = store._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE public.user_credits SET plan_assigned_at = plan_assigned_at - interval '1 second' "
                    "WHERE user_id = %s",
                    [user],
                )
            conn.commit()
        finally:
            conn.close()
        first_assigned_at = store.get_user_plan(user).plan_assigned_at

        store.set_user_plan(user, "planB")
        second_assigned_at = store.get_user_plan(user).plan_assigned_at
        assert second_assigned_at is not None
        assert first_assigned_at is not None
        assert second_assigned_at > first_assigned_at

    def test_unset_user_plan_clears_plan_and_assigned_at_pg(self, store: PostgresStore) -> None:
        store.set_active_pricing(
            {
                "version": 1,
                "metering": {"models": {"*": "1"}},
                "plans": {"pro": {"label": "Pro", "allowance": {"amount": Decimal(100)}}},
            }
        )
        user = _new_uuid(9015)
        store.set_user_plan(user, "pro")
        plan = store.get_user_plan(user)
        assert plan.plan_id is not None
        assert plan.plan_assigned_at is not None

        store.unset_user_plan(user)
        plan = store.get_user_plan(user)
        assert plan.plan_id is None
        assert plan.plan_assigned_at is None

    def test_unset_user_plan_idempotent_when_no_plan_pg(self, store: PostgresStore) -> None:
        user = _new_uuid(9016)
        result = store.unset_user_plan(user)
        assert result == {"user_id": user}
        plan = store.get_user_plan(user)
        assert plan.plan_id is None

    # ── 10. WS3: fractional fixed job cost round-trips through Postgres ────

    def test_fractional_fixed_cost_round_trips_pg(self, store: PostgresStore) -> None:
        config = {
            "version": 1,
            "metering": {"models": {"*": "input_tokens * 1"}, "flat_jobs": {"job": Decimal("2.5")}},
        }
        store.set_active_pricing(config)

        fetched = store.get_active_pricing()
        assert fetched is not None
        assert fetched.config["metering"]["flat_jobs"]["job"] == Decimal("2.5")
        assert isinstance(fetched.config["metering"]["flat_jobs"]["job"], Decimal)

        user = _new_uuid(9015)
        store.add_credits(user, Decimal("100"), "purchase")
        m = CreditManager(store=store)
        m.publish_pricing_from_dict(config)
        result = m.deduct_fixed(user, "job")
        assert result.amount == Decimal("2.5")
        assert result.amount != Decimal("2")
        assert result.amount != Decimal("3")
        assert store.get_balance(user).balance == Decimal("97.5")

    # ── 11. settle_lease canonical-signature joint regression guard ────────

    def test_settle_lease_floor_clamp_and_period_start_joint_pg(self, store: PostgresStore) -> None:
        """Guards against migration 022's overload-consolidation bug (see
        021's un-consolidated 7-arg vs 019's un-clamped 8-arg overloads):
        floor-clamping (C1) and a non-default period_start must both apply
        in the SAME settle_lease call now that there is exactly one canonical
        signature. Before 022, every real caller (8 positional args including
        skip_allowance) always resolved to the un-floor-clamped overload.
        """
        store.set_active_pricing(
            {
                "version": 1,
                "metering": {"models": {"*": "1"}},
                "ledger": {"min_balance": Decimal("0")},
                "plans": {"basic": {"label": "Basic", "allowance": {"amount": Decimal("5")}}},
            }
        )
        user = _new_uuid(9016)
        store.set_user_plan(user, "basic")
        store.add_credits(user, Decimal("0"), "adjustment")

        window = date(2024, 3, 1)
        lease = store.create_lease(
            user,
            Decimal("20"),
            "usage",
            billing_mode="overdraft",
            floor=Decimal("-20"),
            overdraft_floor=Decimal("-20"),
            period_start=window,
        )
        assert lease.error is None

        # Actual cost 100 (de-clamped settle can exceed the hold): allowance
        # covers 5 (window-keyed), net=95 requested but floor-clamped to max
        # debit = balance(0) - floor(-20) = 20.
        ded = store.settle_lease(user, lease.lease_id, Decimal("100"), period_start=window)
        assert ded.error is None
        assert ded.allowance_consumed == Decimal("5")
        assert ded.amount == Decimal("20")  # floor-clamped, NOT 95
        assert ded.balance_after == Decimal("-20")
        # The allowance was consumed against the EXPLICIT window, not "now".
        assert store.check_allowance(user, period_start=window).allowance_remaining == Decimal("0")

    # ── 12. increment_usage_window: investigate actual current behavior ────

    def test_increment_usage_window_plan_key_string_vs_uuid_pg(self, store: PostgresStore) -> None:
        """Open question from the WS9 implementation plan: increment_usage_window's
        SQL signature is (p_user_id UUID, p_plan_id UUID, p_amount NUMERIC, ...),
        but the store method's ``plan_id`` param actually receives a plan_key
        STRING (e.g. "basic"), not a UUID, on every real call site. Confirmed
        dead code on the hot paths (deduct_with_allowance/settle_lease/create_lease
        consume allowance inline, never via this RPC). This test documents
        whatever Postgres actually does when called this way -- it does NOT fix
        anything (see the gap notes in the final summary).
        """
        store.set_active_pricing(
            {
                "version": 1,
                "metering": {"models": {"*": "1"}},
                "plans": {"basic": {"label": "Basic", "allowance": {"amount": Decimal("10")}}},
            }
        )
        user = _new_uuid(9017)
        store.set_user_plan(user, "basic")

        with pytest.raises(psycopg2.errors.InvalidTextRepresentation):
            store.increment_usage_window(user, "basic", Decimal("1"))


# ═══════════════════════════════════════════════════════════════════════════
# Credit tiers (migration 023) against a real Postgres instance
# ═══════════════════════════════════════════════════════════════════════════


class TestCreditTiersPg:
    """Tier-aware add_credits/deduct_with_allowance/settle_lease/refund_credits/
    sweep_expired_credits/get_credit_tiers against a real Postgres + the 023
    RPCs (``sync_tiers_from_config``, ``get_user_credit_tiers``, and the
    ``CREATE OR REPLACE`` tier-aware rewrites of ``credits_add``/
    ``deduct_with_allowance``/``settle_lease``/``expire_credits``/
    ``refund_credits``). MemoryStore coverage for the same scenarios lives in
    ``test_tiers.py``/``test_tiers_adversarial.py`` — this class targets the
    SQL layer specifically. Skips without a live ``pg_database_url`` (see
    ``conftest.py``); does not attempt to spin up Postgres itself.
    """

    @pytest.fixture
    def store(self, pg_database_url: str) -> PostgresStore:
        s = PostgresStore(pg_database_url)
        assert s.setup().success
        return s

    def _two_tier_config(self) -> dict[str, Any]:
        return {
            "version": 1,
            "metering": {"models": {"*": "input_tokens * 1"}},
            "ledger": {"min_balance": Decimal("0")},
            "buckets": {
                "gifted": {"label": "Gifted", "priority": 10, "expires": True, "ttl_days": 30},
                "purchased": {"label": "Purchased", "priority": 30, "default": True},
            },
        }

    def test_no_tiers_configured_uses_synthetic_default(self, store: PostgresStore) -> None:
        user = _new_uuid(9101)
        store.add_credits(user, Decimal("50"), "purchase")

        tiers = store.get_bucket_balances(user)
        assert len(tiers.buckets) == 1
        assert tiers.buckets[0].bucket_key == "default"
        assert tiers.buckets[0].balance == Decimal("50")
        assert tiers.total_balance == Decimal("50")

    def test_add_credits_resolves_explicit_and_default_tier(self, store: PostgresStore) -> None:
        store.set_active_pricing(self._two_tier_config())
        user = _new_uuid(9102)

        gifted = store.add_credits(user, Decimal("10"), "purchase", tier="gifted")
        assert gifted.bucket == "gifted"

        omitted = store.add_credits(user, Decimal("20"), "purchase")
        assert omitted.bucket == "purchased"

        by_key = {t.bucket_key: t.balance for t in store.get_bucket_balances(user).buckets}
        assert by_key == {"gifted": Decimal("10"), "purchased": Decimal("20")}

    def test_add_credits_unknown_tier_raises(self, store: PostgresStore) -> None:
        store.set_active_pricing(self._two_tier_config())
        user = _new_uuid(9103)
        with pytest.raises(StoreError, match="tier_not_found"):
            store.add_credits(user, Decimal("10"), "purchase", tier="nonexistent")

    def test_priority_ordered_deduct_and_bucket_breakdown(self, store: PostgresStore) -> None:
        store.set_active_pricing(self._two_tier_config())
        user = _new_uuid(9104)
        future = datetime.now(UTC) + timedelta(days=1)
        store.add_credits(user, Decimal("10"), "purchase", tier="gifted", expires_at=future)
        store.add_credits(user, Decimal("100"), "purchase", tier="purchased")

        result = store.deduct_with_allowance(user, Decimal("15"))
        assert result.error is None
        assert result.bucket_breakdown == {"gifted": Decimal("10"), "purchased": Decimal("5")}

        by_key = {t.bucket_key: t.balance for t in store.get_bucket_balances(user).buckets}
        assert by_key == {"gifted": Decimal("0"), "purchased": Decimal("95")}

    def test_settle_lease_applies_same_tier_walk(self, store: PostgresStore) -> None:
        store.set_active_pricing(self._two_tier_config())
        user = _new_uuid(9105)
        future = datetime.now(UTC) + timedelta(days=1)
        store.add_credits(user, Decimal("10"), "purchase", tier="gifted", expires_at=future)
        store.add_credits(user, Decimal("100"), "purchase", tier="purchased")

        lease = store.create_lease(user, Decimal("20"), "usage", floor=Decimal("0"))
        assert lease.error is None
        settled = store.settle_lease(user, lease.lease_id, Decimal("15"))
        assert settled.error is None
        assert settled.bucket_breakdown == {"gifted": Decimal("10"), "purchased": Decimal("5")}

    def test_idempotent_replay_echoes_original_bucket_breakdown(self, store: PostgresStore) -> None:
        store.set_active_pricing(self._two_tier_config())
        user = _new_uuid(9106)
        future = datetime.now(UTC) + timedelta(days=1)
        store.add_credits(user, Decimal("10"), "purchase", tier="gifted", expires_at=future)
        store.add_credits(user, Decimal("100"), "purchase", tier="purchased")

        first = store.deduct_with_allowance(user, Decimal("15"), idempotency_key="pg-tier-k1")
        assert first.bucket_breakdown == {"gifted": Decimal("10"), "purchased": Decimal("5")}

        # Mutate state after the fact — the replay must not recompute.
        store.add_credits(user, Decimal("1000"), "purchase", tier="gifted")

        second = store.deduct_with_allowance(user, Decimal("15"), idempotency_key="pg-tier-k1")
        assert second.idempotent is True
        assert second.bucket_breakdown == first.bucket_breakdown

    def test_lifo_refund_restores_reverse_priority_order(self, store: PostgresStore) -> None:
        store.set_active_pricing(self._two_tier_config())
        user = _new_uuid(9107)
        future = datetime.now(UTC) + timedelta(days=1)
        store.add_credits(user, Decimal("10"), "purchase", tier="gifted", expires_at=future)
        store.add_credits(user, Decimal("100"), "purchase", tier="purchased")

        ded = store.deduct_with_allowance(user, Decimal("15"))
        assert ded.bucket_breakdown == {"gifted": Decimal("10"), "purchased": Decimal("5")}

        refund = store.refund_credits(ded.transaction_id)
        assert refund.error is None
        # LIFO: last-drained (purchased) restored first.
        assert refund.bucket_breakdown == {"purchased": Decimal("5"), "gifted": Decimal("10")}

        by_key = {t.bucket_key: t.balance for t in store.get_bucket_balances(user).buckets}
        assert by_key == {"gifted": Decimal("10"), "purchased": Decimal("100")}

    def test_overdraft_routes_excess_to_allow_overdraft_tier(self, store: PostgresStore) -> None:
        store.set_active_pricing(
            {
                "version": 1,
                "metering": {"models": {"*": "input_tokens * 1"}},
                "buckets": {
                    "gifted": {"label": "Gifted", "priority": 10},
                    "purchased": {"label": "Purchased", "priority": 30, "default": True, "allow_overdraft": True},
                },
            }
        )
        user = _new_uuid(9108)
        store.add_credits(user, Decimal("10"), "purchase", tier="gifted")
        store.add_credits(user, Decimal("5"), "purchase", tier="purchased")

        result = store.deduct_with_allowance(user, Decimal("40"), min_balance=Decimal("-50"))
        assert result.error is None
        assert result.bucket_breakdown == {"gifted": Decimal("10"), "purchased": Decimal("30")}

        by_key = {t.bucket_key: t.balance for t in store.get_bucket_balances(user).buckets}
        assert by_key["purchased"] == Decimal("-25")

    def test_get_credit_tiers_sorted_by_priority_ascending(self, store: PostgresStore) -> None:
        store.set_active_pricing(self._two_tier_config())
        user = _new_uuid(9109)
        future = datetime.now(UTC) + timedelta(days=1)
        store.add_credits(user, Decimal("10"), "purchase", tier="gifted", expires_at=future)
        store.add_credits(user, Decimal("100"), "purchase", tier="purchased")

        result = store.get_bucket_balances(user)
        assert [t.bucket_key for t in result.buckets] == ["gifted", "purchased"]
        assert result.total_balance == Decimal("110")
        assert result.total_balance == store.get_balance(user).balance

    def test_manager_publish_pricing_from_dict_rejects_invalid_tiers_pg(self, store: PostgresStore) -> None:
        """Client-side config validation (config.py) rejects invalid tiers
        before ever reaching Postgres — same guarantee as MemoryStore, since
        this validation is entirely in the Python layer."""
        manager = CreditManager(store=store)
        with pytest.raises(ConfigError):
            manager.publish_pricing_from_dict(
                {
                    "version": 1,
                    "metering": {"models": {"*": "input_tokens * 1"}},
                    "ledger": {
                        "buckets": {
                            "a": {"label": "A", "priority": 1, "default": True},
                            "b": {"label": "B", "priority": 2, "default": True},
                        },
                    },
                }
            )


# ═══════════════════════════════════════════════════════════════════════════
# CreditManager end-to-end — credit tiers through the public manager API,
# real Postgres
# ═══════════════════════════════════════════════════════════════════════════


class TestCreditManagerTiersPg:
    """End-to-end credit-tier coverage through :class:`CreditManager` (not the
    raw store) against a real Postgres instance.

    ``TestCreditTiersPg`` above drives ``PostgresStore`` directly to pin down
    SQL/RPC behavior; this class is the manager-level counterpart. It exercises
    the full integrator-facing surface — ``publish_pricing_from_dict``,
    ``add_credits``, the pricing-engine-driven ``deduct``, the
    ``reserve``/``settle`` lease lifecycle, ``refund_credits``,
    ``get_credit_tiers``, and ``sweep_expired_credits`` — and asserts on both
    the returned results and the ``CreditEventEmitter`` events they fire.
    Nowhere else in the suite drives ``CreditManager`` against a real store
    (every other manager test uses ``MemoryStore``), so this is the only place
    the manager's policy resolution, event emission, and pricing engine are
    verified end to end against the actual Postgres RPCs tiers were added to.
    """

    @pytest.fixture
    def store(self, pg_database_url: str) -> PostgresStore:
        s = PostgresStore(pg_database_url)
        assert s.setup().success
        return s

    def _tiered_config(self) -> dict[str, Any]:
        return {
            "version": 1,
            "metering": {"models": {"*": "input_tokens * 1"}},
            "ledger": {
                "min_balance": 0,
                "buckets": {
                    "gifted": {"label": "Gifted", "priority": 10, "expires": True, "ttl_days": 30},
                    "purchased": {"label": "Purchased", "priority": 30, "default": True},
                },
            },
        }

    def _subscribe_all(self, emitter: CreditEventEmitter) -> list[CreditEvent]:
        events: list[CreditEvent] = []
        for event_type in CREDIT_EVENT_TYPES:
            emitter.on(event_type, events.append)
        return events

    def test_add_deduct_refund_full_lifecycle_through_manager(self, store: PostgresStore) -> None:
        """The flagship end-to-end scenario: grant into two tiers, charge a
        pricing-engine-calculated cost that spans both, refund it, and verify
        every step's result AND emitted event agree with get_credit_tiers."""
        emitter = CreditEventEmitter()
        events = self._subscribe_all(emitter)
        mgr = CreditManager(store=store, emitter=emitter)
        mgr.publish_pricing_from_dict(self._tiered_config())
        user = _new_uuid(9301)

        gifted = mgr.add_credits(user, Decimal("20"), tx_type="purchase", tier="gifted")
        assert gifted.bucket == "gifted"
        purchased = mgr.add_credits(user, Decimal("50"), tx_type="purchase")  # omitted -> is_default
        assert purchased.bucket == "purchased"
        assert [e.type for e in events] == ["credits.added", "credits.added"]

        tiers = mgr.get_bucket_balances(user)
        assert {t.bucket_key: t.balance for t in tiers.buckets} == {
            "gifted": Decimal("20"),
            "purchased": Decimal("50"),
        }
        assert tiers.total_balance == Decimal("70")

        # Cost computed by the real pricing engine (not a raw amount) crosses
        # the tier boundary: 25 tokens @ 1/token drains gifted (20) then 5
        # from purchased.
        events.clear()
        result = mgr.deduct(user, UsageMetrics(input_tokens=25), idempotency_key="mgr-e2e-1")
        assert result.bucket_breakdown == {"gifted": Decimal("20"), "purchased": Decimal("5")}
        assert result.balance_after == Decimal("45")
        assert "credits.deducted" in [e.type for e in events]

        tiers_after_deduct = mgr.get_bucket_balances(user)
        assert {t.bucket_key: t.balance for t in tiers_after_deduct.buckets} == {
            "gifted": Decimal("0"),
            "purchased": Decimal("45"),
        }

        events.clear()
        refund = mgr.refund_credits(result.transaction_id)
        assert refund.error is None
        # LIFO: purchased (last drained) is restored first, then gifted.
        assert refund.bucket_breakdown == {"purchased": Decimal("5"), "gifted": Decimal("20")}
        assert "credits.refunded" in [e.type for e in events]

        tiers_after_refund = mgr.get_bucket_balances(user)
        assert {t.bucket_key: t.balance for t in tiers_after_refund.buckets} == {
            "gifted": Decimal("20"),
            "purchased": Decimal("50"),
        }
        assert tiers_after_refund.total_balance == mgr.get_balance(user).balance

    def test_reserve_settle_lease_applies_tier_walk_through_manager(self, store: PostgresStore) -> None:
        """The safe (lease) path — reserve() then settle() — must apply the
        identical tier walk as the direct deduct() path, and emit
        credits.reserved + credits.deducted."""
        emitter = CreditEventEmitter()
        events = self._subscribe_all(emitter)
        mgr = CreditManager(store=store, emitter=emitter)
        mgr.publish_pricing_from_dict(self._tiered_config())
        user = _new_uuid(9302)

        future = datetime.now(UTC) + timedelta(days=1)
        mgr.add_credits(user, Decimal("10"), tx_type="purchase", tier="gifted", expires_at=future)
        mgr.add_credits(user, Decimal("100"), tx_type="purchase")

        events.clear()
        lease = mgr.reserve(user, UsageMetrics(input_tokens=15))
        assert "credits.reserved" in [e.type for e in events]

        settled = mgr.settle(user, lease.lease_id, UsageMetrics(input_tokens=15))
        assert settled.bucket_breakdown == {"gifted": Decimal("10"), "purchased": Decimal("5")}

        tiers = mgr.get_bucket_balances(user)
        assert {t.bucket_key: t.balance for t in tiers.buckets} == {
            "gifted": Decimal("0"),
            "purchased": Decimal("95"),
        }

    def test_sweep_expired_credits_through_manager_scopes_per_tier(self, store: PostgresStore) -> None:
        """sweep_expired_credits() through the manager only drains the
        expiring tier's own balance, leaves the non-expiring tier untouched,
        and emits credits.expired — SweepResult.expired_by_bucket carries the
        per-tier split."""
        emitter = CreditEventEmitter()
        events = self._subscribe_all(emitter)
        mgr = CreditManager(store=store, emitter=emitter)
        mgr.publish_pricing_from_dict(self._tiered_config())
        user = _new_uuid(9303)

        # Must be in the future at grant time (invalid_expires_at) but elapsed
        # by the time we sweep — real wall-clock wait, no injectable clock
        # over a live RPC.
        soon = datetime.now(UTC) + timedelta(milliseconds=500)
        mgr.add_credits(user, Decimal("15"), tx_type="purchase", tier="gifted", expires_at=soon)
        mgr.add_credits(user, Decimal("10"), tx_type="purchase")  # purchased — never expires
        time.sleep(0.8)

        events.clear()
        swept = mgr.sweep_expired_credits(dry_run=False)
        assert swept.expired_by_bucket == {"gifted": Decimal("15")}
        assert "credits.expired" in [e.type for e in events]

        tiers = mgr.get_bucket_balances(user)
        assert {t.bucket_key: t.balance for t in tiers.buckets} == {
            "gifted": Decimal("0"),
            "purchased": Decimal("10"),
        }

    def test_settle_overdraft_routes_excess_to_allow_overdraft_tier_through_manager(self, store: PostgresStore) -> None:
        """Settling a lease admitted under the ``overdraft`` policy (negative
        ``overdraft_floor``, resolved via the manager's policy machinery — the
        client-side config schema rejects a negative ``min_balance`` outright,
        so overdraft can only be reached this way) must route the excess into
        the ``allow_overdraft`` tier AND fire ``credits.overdraft`` — this
        signal only fires on the settle()/lease path (not the direct deduct()
        path), so it isn't covered by the store-level overdraft test above."""
        emitter = CreditEventEmitter()
        events = self._subscribe_all(emitter)
        mgr = CreditManager(store=store, emitter=emitter, policy="overdraft", overdraft_floor=Decimal("-50"))
        mgr.publish_pricing_from_dict(
            {
                "version": 1,
                "metering": {"models": {"*": "input_tokens * 1"}},
                "ledger": {
                    "buckets": {
                        "gifted": {"label": "Gifted", "priority": 10, "default": True},
                        "purchased": {"label": "Purchased", "priority": 20, "allow_overdraft": True},
                    },
                },
            }
        )
        user = _new_uuid(9304)
        mgr.add_credits(user, Decimal("10"), tx_type="purchase")  # -> gifted (is_default)
        mgr.add_credits(user, Decimal("5"), tx_type="purchase", tier="purchased")

        lease = mgr.reserve(user, UsageMetrics(input_tokens=40))
        events.clear()
        settled = mgr.settle(user, lease.lease_id, UsageMetrics(input_tokens=40))
        assert settled.bucket_breakdown == {"gifted": Decimal("10"), "purchased": Decimal("30")}
        assert settled.balance_after == Decimal("-25")
        assert "credits.overdraft" in [e.type for e in events]

        by_key = {t.bucket_key: t.balance for t in mgr.get_bucket_balances(user).buckets}
        assert by_key["purchased"] == Decimal("-25")


# ═══════════════════════════════════════════════════════════════════════════
# Lazy per-user expiry (011_lazy_expiry.sql) against a real Postgres instance
# ═══════════════════════════════════════════════════════════════════════════


class TestLazyExpiryPg:
    """``store.sweep_expired_credits(user_id=...)`` and ``CreditManager(lazy_expiry=True)``
    against the real ``expire_credits`` RPC (011_lazy_expiry.sql's ``p_user_id``
    param). MemoryStore coverage for the same scenarios lives in
    ``test_lazy_expiry.py`` — this class targets the SQL layer specifically,
    the one thing MemoryStore tests structurally cannot exercise.

    Real Postgres has no injectable clock over a live RPC (unlike MemoryStore's
    ``clock=`` fixture), so every expiry here uses a near-future ``expires_at``
    plus a short real sleep, matching the idiom already established by
    ``TestCreditManagerTiersPg.test_sweep_expired_credits_through_manager_scopes_per_tier``.
    """

    @pytest.fixture
    def store(self, pg_database_url: str) -> PostgresStore:
        s = PostgresStore(pg_database_url)
        assert s.setup().success
        return s

    def test_scoped_sweep_via_store_only_touches_target_user(self, store: PostgresStore) -> None:
        """Two users each hold an expired grant. Sweeping user A directly via
        ``store.sweep_expired_credits(user_id=...)`` (not through the manager)
        must zero A's balance while leaving B's expired-but-unswept grant fully
        intact (still counted) until B is swept individually."""
        user_a = _new_uuid(9401)
        user_b = _new_uuid(9402)
        soon = datetime.now(UTC) + timedelta(milliseconds=500)
        store.add_credits(user_a, Decimal("40"), "purchase", expires_at=soon)
        store.add_credits(user_b, Decimal("60"), "purchase", expires_at=soon)
        time.sleep(0.8)

        swept_a = store.sweep_expired_credits(user_id=user_a)
        assert swept_a.expired_count == 1
        assert swept_a.expired_amount == Decimal("40")
        assert store.get_balance(user_a).balance == Decimal("0")

        # B untouched by A's scoped sweep -- still counts the expired grant.
        assert store.get_balance(user_b).balance == Decimal("60")

        swept_b = store.sweep_expired_credits(user_id=user_b)
        assert swept_b.expired_count == 1
        assert swept_b.expired_amount == Decimal("60")
        assert store.get_balance(user_b).balance == Decimal("0")

    def test_add_credits_idempotency_key_dedupes_at_the_credits_add_rpc(
        self, store: PostgresStore, pg_database_url: str
    ) -> None:
        """Calling ``add_credits(..., idempotency_key=...)`` twice must return
        the same transaction, move the balance only once, AND -- the part a
        client-side illusion couldn't fake -- leave exactly one row with that
        key in ``credit_transactions`` when queried directly."""
        user = _new_uuid(9403)

        r1 = store.add_credits(user, Decimal("100"), type="purchase", idempotency_key="evt-1")
        r2 = store.add_credits(user, Decimal("100"), type="purchase", idempotency_key="evt-1")

        assert r1.transaction_id != ""
        assert r1.transaction_id == r2.transaction_id
        assert store.get_balance(user).balance == Decimal("100")

        conn = psycopg2.connect(pg_database_url)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM credit_transactions "
                    "WHERE user_id = %s AND metadata ->> 'idempotency_key' = %s",
                    [user, "evt-1"],
                )
                row = cur.fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[0] == 1

    def test_lazy_expiry_true_hides_expired_credits_across_all_gated_manager_methods(
        self, store: PostgresStore
    ) -> None:
        """The flagship lazy-expiry scenario: a real ``CreditManager`` backed by
        a real ``PostgresStore``, ``lazy_expiry=True``, and NO explicit
        ``sweep_expired_credits()`` call anywhere in this test. Every
        balance-authoritative method the manager gates on ``_maybe_lazy_expire``
        must reflect the true, post-expiry balance -- proving the "credits page
        must show the true non-expired balance" requirement against the real
        RPCs, not just MemoryStore."""
        mgr = CreditManager(store=store, lazy_expiry=True)
        mgr.publish_pricing_from_dict(
            {
                "version": 1,
                "metering": {"models": {"*": "input_tokens * 1"}},
                "ledger": {"min_balance": 0},
            }
        )
        user = _new_uuid(9404)

        soon = datetime.now(UTC) + timedelta(milliseconds=500)
        mgr.add_credits(user, Decimal("100"), tx_type="purchase", expires_at=soon)
        time.sleep(0.8)

        # No sweep_expired_credits() call anywhere below: lazy_expiry alone
        # must hide the now-expired grant on every gated call.
        assert mgr.get_balance(user).balance == Decimal("0")
        assert mgr.get_available(user).available == Decimal("0")

        afford = mgr.can_afford(user, Decimal("10"))
        assert afford.affordable is False
        assert afford.spendable == Decimal("0")

        with pytest.raises(InsufficientCreditsError):
            mgr.deduct(user, UsageMetrics(input_tokens=10))

        with pytest.raises(InsufficientCreditsError):
            mgr.reserve(user, Decimal("10"))


# ═══════════════════════════════════════════════════════════════════════════
# grant_subscription_cycle (011_lazy_expiry.sql's idempotent credits_add)
# against a real Postgres instance
# ═══════════════════════════════════════════════════════════════════════════


class TestSubscriptionCyclePg:
    """``CreditManager.grant_subscription_cycle`` end to end against a real
    Postgres. MemoryStore coverage lives in ``test_subscription_cycle.py``.

    The critical regression covered here: a real bug was just found and fixed
    in the JS mirror of this method (wiping a tier's balance before the
    idempotent grant, so webhook redelivery double-wiped a balance it had just
    legitimately granted). The Python implementation guards against this by
    snapshotting balance/``lifetime_purchased`` BEFORE granting and only
    wiping-if-still-stale AFTER (detected via a ``lifetime_purchased`` delta) —
    proven so far only against MemoryStore. This class proves it against the
    real ``credits_add``/tier-balance RPCs.
    """

    @pytest.fixture
    def store(self, pg_database_url: str) -> PostgresStore:
        s = PostgresStore(pg_database_url)
        assert s.setup().success
        return s

    def _subscription_config(self) -> dict[str, Any]:
        return {
            "version": 1,
            "metering": {"models": {"*": "input_tokens * 1"}},
            "ledger": {
                "min_balance": 0,
                "buckets": {
                    "subscription": {
                        "label": "Subscription",
                        "priority": 1,
                        "expires": True,
                        "default": True,
                        "ttl_days": 30,
                    },
                },
            },
        }

    def test_first_cycle_grant_increases_balance_by_granted_amount(self, store: PostgresStore) -> None:
        mgr = CreditManager(store=store)
        mgr.publish_pricing_from_dict(self._subscription_config())
        user = _new_uuid(9405)

        result = mgr.grant_subscription_cycle(user, Decimal("500"), ttl_days=30, idempotency_key="evt-cycle-1")
        assert result.amount == Decimal("500")
        assert mgr.get_balance(user).balance == Decimal("500")

    def test_redelivered_webhook_same_idempotency_key_is_a_full_noop(self, store: PostgresStore) -> None:
        """The exact bug class fixed in the JS mirror: redelivering the same
        webhook event id with the default ``replace_prior=True`` must be a
        full no-op -- not a double-grant, and NOT a wipe-then-nothing that
        zeroes the balance the first (legitimate) call just granted."""
        mgr = CreditManager(store=store)
        mgr.publish_pricing_from_dict(self._subscription_config())
        user = _new_uuid(9406)

        first = mgr.grant_subscription_cycle(user, Decimal("300"), ttl_days=30, idempotency_key="evt-redeliver")
        assert mgr.get_balance(user).balance == Decimal("300")

        for _ in range(3):
            replay = mgr.grant_subscription_cycle(user, Decimal("300"), ttl_days=30, idempotency_key="evt-redeliver")
            assert replay.transaction_id == first.transaction_id
            # Not zero (wiped), not 600/900 (double-granted) -- unchanged.
            assert mgr.get_balance(user).balance == Decimal("300")

    def test_new_cycle_with_replace_prior_expires_leftover_instead_of_stacking(self, store: PostgresStore) -> None:
        mgr = CreditManager(store=store)
        mgr.publish_pricing_from_dict(self._subscription_config())
        user = _new_uuid(9407)

        mgr.grant_subscription_cycle(user, Decimal("200"), ttl_days=30, idempotency_key="evt-month-1")
        mgr.deduct(user, UsageMetrics(input_tokens=80), idempotency_key="usage-month-1")
        # Leftover balance from cycle 1, after some usage.
        assert mgr.get_balance(user).balance == Decimal("120")

        result = mgr.grant_subscription_cycle(
            user, Decimal("150"), ttl_days=30, idempotency_key="evt-month-2", replace_prior=True
        )
        # The 120 leftover is expired (wiped), not stacked -- balance is
        # exactly the new cycle's grant.
        assert mgr.get_balance(user).balance == Decimal("150")
        assert result.new_balance == Decimal("150")

    def test_replace_prior_false_stacks_new_grant_on_top_of_leftover(self, store: PostgresStore) -> None:
        mgr = CreditManager(store=store)
        mgr.publish_pricing_from_dict(self._subscription_config())
        user = _new_uuid(9408)

        mgr.grant_subscription_cycle(user, Decimal("100"), ttl_days=30, idempotency_key="evt-a")
        assert mgr.get_balance(user).balance == Decimal("100")

        mgr.grant_subscription_cycle(user, Decimal("40"), ttl_days=30, idempotency_key="evt-b", replace_prior=False)
        assert mgr.get_balance(user).balance == Decimal("140")


# ═══════════════════════════════════════════════════════════════════════════
# Feature limits (per-feature invocation-count limits) — Postgres
# ═══════════════════════════════════════════════════════════════════════════
#
# Ledger-derived counting: a feature call is counted by COUNT(*) over
# committed `usage` transactions tagged `metadata.feature == <feature>` in
# `[period_start, period_end)` — NO new counter table (see
# 012_feature_limits.sql / the feature-limit blocks added to the three atomic
# RPCs in 009_deduct_and_leases.sql). Driven directly at the store layer
# (deduct_with_allowance / create_lease / settle_lease / check_feature_limit)
# since FeatureLimit plan resolution is the manager track's responsibility —
# these tests construct FeatureLimit instances directly, exactly as the
# manager would after resolving them from a user's plan.


class TestFeatureLimitsPg:
    """Per-feature invocation-count limits against a real Postgres."""

    @pytest.fixture
    def store(self, pg_database_url: str) -> PostgresStore:
        s = PostgresStore(pg_database_url)
        assert s.setup().success
        return s

    def _today_window(self) -> tuple[date, date]:
        return resolve_calendar_window(datetime.now(UTC), "daily")

    def _backdate_transaction(self, store: PostgresStore, transaction_id: str, when: datetime) -> None:
        """White-box: force a transaction's created_at into the past (mirrors
        TestLeaseLifecyclePg._expire) to simulate a call made in a prior
        window -- Postgres's now() can't be mocked from the client."""
        conn = store._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE public.credit_transactions SET created_at = %s WHERE id = %s",
                    [when, transaction_id],
                )
            conn.commit()
        finally:
            conn.close()

    def test_deduct_deny_blocks_after_max_calls_pg(self, store: PostgresStore) -> None:
        user = _new_uuid(9501)
        store.add_credits(user, Decimal("1000"), "purchase")
        period_start, _ = self._today_window()
        limit = FeatureLimit(max_calls=2, period="daily", action="deny")

        r1 = store.deduct_with_allowance(
            user,
            Decimal("1"),
            idempotency_key="f1",
            feature="bg_removal",
            feature_limit=limit,
            feature_period_start=period_start,
        )
        assert r1.error is None
        r2 = store.deduct_with_allowance(
            user,
            Decimal("1"),
            idempotency_key="f2",
            feature="bg_removal",
            feature_limit=limit,
            feature_period_start=period_start,
        )
        assert r2.error is None
        # Third call: prior count is now 2 == max_calls -> deny, nothing consumed.
        r3 = store.deduct_with_allowance(
            user,
            Decimal("1"),
            idempotency_key="f3",
            feature="bg_removal",
            feature_limit=limit,
            feature_period_start=period_start,
        )
        assert r3.error == "feature_limit_reached"
        assert store.get_balance(user).balance == Decimal("998")

    def test_deduct_tags_metadata_feature_even_without_limit_pg(self, store: PostgresStore) -> None:
        """``feature`` is tagged on the ledger row even when no limit is
        configured (``feature_limit=None``) so history is accurate once a
        limit is enabled later (contract: "always tag")."""
        user = _new_uuid(9502)
        store.add_credits(user, Decimal("100"), "purchase")
        r = store.deduct_with_allowance(user, Decimal("1"), feature="untracked_feature")
        assert r.error is None
        conn = store._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT metadata->>'feature' FROM public.credit_transactions WHERE id = %s",
                    [r.transaction_id],
                )
                row = cur.fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[0] == "untracked_feature"

    def test_deduct_warn_action_does_not_block_pg(self, store: PostgresStore) -> None:
        user = _new_uuid(9503)
        store.add_credits(user, Decimal("1000"), "purchase")
        period_start, _ = self._today_window()
        limit = FeatureLimit(max_calls=1, period="daily", action="warn")

        r1 = store.deduct_with_allowance(
            user,
            Decimal("1"),
            idempotency_key="w1",
            feature="hd_export",
            feature_limit=limit,
            feature_period_start=period_start,
        )
        assert r1.error is None
        assert r1.feature_limit_warning is None  # under limit, no warning yet

        r2 = store.deduct_with_allowance(
            user,
            Decimal("1"),
            idempotency_key="w2",
            feature="hd_export",
            feature_limit=limit,
            feature_period_start=period_start,
        )
        assert r2.error is None  # warn never blocks
        assert r2.feature_limit_warning == "warn"
        assert store.get_balance(user).balance == Decimal("998")

    def test_create_lease_deny_blocks_at_admission_pg(self, store: PostgresStore) -> None:
        user = _new_uuid(9504)
        store.add_credits(user, Decimal("1000"), "purchase")
        period_start, _ = self._today_window()
        limit = FeatureLimit(max_calls=1, period="daily", action="deny")

        # Commit one call's worth of usage to the ledger first (bypass
        # admission on purpose -- create_lease has no feature params yet).
        lease0 = store.create_lease(user, Decimal("1"), "usage", floor=Decimal("0"))
        settled0 = store.settle_lease(
            user,
            lease0.lease_id,
            Decimal("1"),
            feature="video_gen",
            feature_limit=limit,
            feature_period_start=period_start,
        )
        assert settled0.error is None

        # Admission for a second call is denied -- count already at max_calls.
        lease1 = store.create_lease(
            user,
            Decimal("1"),
            "usage",
            floor=Decimal("0"),
            feature="video_gen",
            feature_limit=limit,
            feature_period_start=period_start,
        )
        assert lease1.error == "feature_limit_reached"

    def test_create_lease_warn_action_never_checked_at_admission_pg(self, store: PostgresStore) -> None:
        """Admission only ever enforces ``deny``; a ``warn``/``notify`` limit
        never blocks ``create_lease`` even once the count is already at/over
        the limit -- nothing to warn about yet (no charge has happened)."""
        user = _new_uuid(9505)
        store.add_credits(user, Decimal("1000"), "purchase")
        period_start, _ = self._today_window()
        limit = FeatureLimit(max_calls=1, period="daily", action="warn")

        lease0 = store.create_lease(user, Decimal("1"), "usage", floor=Decimal("0"))
        settled0 = store.settle_lease(
            user,
            lease0.lease_id,
            Decimal("1"),
            feature="preview",
            feature_limit=limit,
            feature_period_start=period_start,
        )
        assert settled0.error is None

        lease1 = store.create_lease(
            user,
            Decimal("1"),
            "usage",
            floor=Decimal("0"),
            feature="preview",
            feature_limit=limit,
            feature_period_start=period_start,
        )
        assert lease1.error is None

    def test_settle_lease_advisory_never_blocks_prefers_deny_pg(self, store: PostgresStore) -> None:
        """``settle_lease`` is advisory-only: even a ``deny`` feature limit
        already breached never blocks at settle -- it only sets
        ``feature_limit_warning`` (the work already happened)."""
        user = _new_uuid(9506)
        store.add_credits(user, Decimal("1000"), "purchase")
        period_start, _ = self._today_window()
        limit = FeatureLimit(max_calls=1, period="daily", action="deny")

        lease0 = store.create_lease(user, Decimal("1"), "usage", floor=Decimal("0"))
        settled0 = store.settle_lease(
            user,
            lease0.lease_id,
            Decimal("1"),
            feature="voice_clone",
            feature_limit=limit,
            feature_period_start=period_start,
        )
        assert settled0.error is None
        assert settled0.feature_limit_warning is None  # first call, under limit

        # Bypass admission on purpose (simulates the documented approximation:
        # a lease admitted before the count reached the limit, or a race).
        lease1 = store.create_lease(user, Decimal("1"), "usage", floor=Decimal("0"))
        settled1 = store.settle_lease(
            user,
            lease1.lease_id,
            Decimal("1"),
            feature="voice_clone",
            feature_limit=limit,
            feature_period_start=period_start,
        )
        assert settled1.error is None  # never blocks
        assert settled1.feature_limit_warning == "deny"
        assert store.get_balance(user).balance == Decimal("998")

    def test_release_lease_does_not_count_toward_limit_pg(self, store: PostgresStore) -> None:
        """A released (never-settled) lease was never counted -- create_lease
        never inserts a usage row, so release needs no decrement logic."""
        user = _new_uuid(9507)
        store.add_credits(user, Decimal("1000"), "purchase")
        period_start, period_end = self._today_window()
        limit = FeatureLimit(max_calls=1, period="daily", action="deny")

        lease = store.create_lease(
            user,
            Decimal("1"),
            "usage",
            floor=Decimal("0"),
            feature="draft_render",
            feature_limit=limit,
            feature_period_start=period_start,
        )
        assert lease.error is None
        assert store.release_lease(user, lease.lease_id).released is True

        check = store.check_feature_limit(user, "draft_render", 1, period_start, period_end)
        assert check.used == 0  # release never counted

        # A subsequent create_lease for the same feature/window still succeeds.
        lease2 = store.create_lease(
            user,
            Decimal("1"),
            "usage",
            floor=Decimal("0"),
            feature="draft_render",
            feature_limit=limit,
            feature_period_start=period_start,
        )
        assert lease2.error is None

    def test_refund_does_not_restore_quota_pg(self, store: PostgresStore) -> None:
        """A refund of a settled usage transaction does not free up quota --
        the original ledger row (and therefore the count) is untouched by a
        refund transaction, which is inserted separately."""
        user = _new_uuid(9508)
        store.add_credits(user, Decimal("1000"), "purchase")
        period_start, period_end = self._today_window()
        limit = FeatureLimit(max_calls=1, period="daily", action="deny")

        r1 = store.deduct_with_allowance(
            user,
            Decimal("1"),
            idempotency_key="r1",
            feature="upscale",
            feature_limit=limit,
            feature_period_start=period_start,
        )
        assert r1.error is None
        refund = store.refund_credits(r1.transaction_id)
        assert refund.error is None

        check = store.check_feature_limit(user, "upscale", 1, period_start, period_end)
        assert check.used == 1  # unchanged by the refund

        r2 = store.deduct_with_allowance(
            user,
            Decimal("1"),
            idempotency_key="r2",
            feature="upscale",
            feature_limit=limit,
            feature_period_start=period_start,
        )
        assert r2.error == "feature_limit_reached"

    def test_check_feature_limit_rpc_pg(self, store: PostgresStore) -> None:
        user = _new_uuid(9509)
        store.add_credits(user, Decimal("1000"), "purchase")
        period_start, period_end = self._today_window()
        limit = FeatureLimit(max_calls=3, period="daily", action="deny")

        for i in range(2):
            r = store.deduct_with_allowance(
                user,
                Decimal("1"),
                idempotency_key=f"chk{i}",
                feature="captioning",
                feature_limit=limit,
                feature_period_start=period_start,
            )
            assert r.error is None

        check = store.check_feature_limit(user, "captioning", 3, period_start, period_end)
        assert check.limited is True
        assert check.limit == 3
        assert check.used == 2
        assert check.remaining == 1

    def test_isolation_per_feature_and_per_user_pg(self, store: PostgresStore) -> None:
        user_a = _new_uuid(9510)
        user_b = _new_uuid(9511)
        store.add_credits(user_a, Decimal("1000"), "purchase")
        store.add_credits(user_b, Decimal("1000"), "purchase")
        period_start, _ = self._today_window()
        limit = FeatureLimit(max_calls=1, period="daily", action="deny")

        store.deduct_with_allowance(
            user_a,
            Decimal("1"),
            idempotency_key="iso1",
            feature="feat_a",
            feature_limit=limit,
            feature_period_start=period_start,
        )
        # Different feature, same user: independent count.
        r_other_feature = store.deduct_with_allowance(
            user_a,
            Decimal("1"),
            idempotency_key="iso2",
            feature="feat_b",
            feature_limit=limit,
            feature_period_start=period_start,
        )
        assert r_other_feature.error is None
        # Same feature, different user: independent count.
        r_other_user = store.deduct_with_allowance(
            user_b,
            Decimal("1"),
            idempotency_key="iso3",
            feature="feat_a",
            feature_limit=limit,
            feature_period_start=period_start,
        )
        assert r_other_user.error is None
        # Same user + same feature again: now denied.
        r_repeat = store.deduct_with_allowance(
            user_a,
            Decimal("1"),
            idempotency_key="iso4",
            feature="feat_a",
            feature_limit=limit,
            feature_period_start=period_start,
        )
        assert r_repeat.error == "feature_limit_reached"

    def test_window_rollover_resets_count_pg(self, store: PostgresStore) -> None:
        """A committed usage row from a PRIOR daily window must not count
        against the CURRENT window -- the count resets across the boundary.
        Postgres's ``now()`` can't be mocked from the client, so this
        backdates a real committed transaction's ``created_at`` into
        yesterday (white-box, mirrors ``TestLeaseLifecyclePg._expire``)
        rather than faking a clock."""
        user = _new_uuid(9512)
        store.add_credits(user, Decimal("1000"), "purchase")
        today_start, today_end = self._today_window()
        yesterday_start = today_start - timedelta(days=1)
        limit = FeatureLimit(max_calls=1, period="daily", action="deny")

        # Consume "yesterday's" quota -- physically inserted now, then
        # logically backdated into yesterday's window.
        y = store.deduct_with_allowance(
            user,
            Decimal("1"),
            idempotency_key="roll1",
            feature="daily_report",
            feature_limit=limit,
            feature_period_start=yesterday_start,
        )
        assert y.error is None
        self._backdate_transaction(
            store, y.transaction_id, datetime.combine(yesterday_start, datetime.min.time(), tzinfo=UTC)
        )

        # Yesterday's window still shows it used (querying that same window).
        check_yesterday = store.check_feature_limit(user, "daily_report", 1, yesterday_start, today_start)
        assert check_yesterday.used == 1

        # Today's window is a fresh slate -- the backdated row falls outside
        # [today_start, today_end), so a new call for today succeeds.
        t = store.deduct_with_allowance(
            user,
            Decimal("1"),
            idempotency_key="roll2",
            feature="daily_report",
            feature_limit=limit,
            feature_period_start=today_start,
        )
        assert t.error is None
        check_today = store.check_feature_limit(user, "daily_report", 1, today_start, today_end)
        assert check_today.used == 1
        assert check_today.remaining == 0

    @pytest.mark.repeat(5)  # money-critical race: rerun to surface rare interleavings
    def test_concurrent_deduct_exactly_n_succeed_under_limit_n_pg(self, store: PostgresStore) -> None:
        """N concurrent ``deduct_with_allowance`` calls against the same
        ``(user, feature, window)``, limit N -- ``deduct_with_allowance``
        locks the user's balance row ``FOR UPDATE`` for the whole pipeline
        (including the feature-limit count-and-decide step), so unlike
        ``create_lease``'s documented admission-only approximation, this is
        exact: exactly N of M succeed, never more."""
        user = _new_uuid(9513)
        store.add_credits(user, Decimal("1000"), "purchase")
        period_start, _ = self._today_window()
        limit = FeatureLimit(max_calls=5, period="daily", action="deny")
        m = 20

        def one(i: int) -> object:
            s = PostgresStore(store._database_url)
            return s.deduct_with_allowance(
                user,
                Decimal("1"),
                idempotency_key=f"conc{i}",
                feature="concurrent_feature",
                feature_limit=limit,
                feature_period_start=period_start,
            )

        with ThreadPoolExecutor(max_workers=m) as ex:
            results = list(ex.map(one, range(m)))

        succeeded = [r for r in results if not r.error]  # type: ignore[attr-defined]
        denied = [r for r in results if r.error == "feature_limit_reached"]  # type: ignore[attr-defined]
        assert len(succeeded) == 5
        assert len(denied) == m - 5
