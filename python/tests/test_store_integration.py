"""Integration tests for each storage provider.

Postgres is provided via a **real Postgres 16** instance. The single
``pg_database_url`` fixture lives in ``conftest.py`` and resolves a connection
string in this order: ``DATABASE_URL`` (what CI and the JS suite use) →
``DUCTO_TEST_PG_URL`` (legacy override) → ``pg_tmp`` (disposable) → skip.

If none is available the Postgres/Supabase-setup tests **skip** with a visible
reason (a DB is optional in a bare sandbox); they are correct and CI-runnable
against any source.

MemoryStore needs zero infra and is always exercised (including the
concurrency/double-spend test).
"""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

try:
    import psycopg2
except ModuleNotFoundError:
    psycopg2 = None  # type: ignore[assignment]

import pytest

from ducto import CreditManager, UsageMetrics
from ducto.interface.base import StoreError
from ducto.interface.memory import MemoryStore
from ducto.interface.models import CreditMetadata, PlanDefinition, PricingConfigData, SpendCap
from ducto.interface.postgres import PostgresStore
from ducto.interface.supabase import HttpxSupabaseStore

# ---------------------------------------------------------------------------
# Shared pricing config used across all tests
# ---------------------------------------------------------------------------

_PRICING = {
    "models": {
        "gpt-4": "input_tokens * 0.01 + output_tokens * 0.03",
        "_default": "input_tokens * 0.001 + output_tokens * 0.003",
    },
    "tools": {"_default": "tool_calls * 0"},
    "min_balance": 5,
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
# mechanism: DATABASE_URL → DUCTO_TEST_PG_URL → pg_tmp → skip).
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
        from ducto.manager import InsufficientCreditsError

        with pytest.raises(InsufficientCreditsError, match="Insufficient credits"):
            manager.deduct("user_1", _METRICS)

    def test_reserve_and_release(self, manager: CreditManager) -> None:
        manager.add_credits("user_1", 100)
        r = manager.reserve_credits("user_1", 30)
        assert r.error is None
        assert r.amount == 30

        # over-reserve → rejected
        r2 = manager.reserve_credits("user_1", 80)
        assert r2.error == "insufficient_credits"

    def test_setup_lists_all_bundled_migrations(self) -> None:
        """setup() derives its file list from the SQL glob, not a hardcode (L5)."""
        from ducto.sql import _get_sql_files

        store = MemoryStore()
        result = store.setup()
        expected = [f.name for f in _get_sql_files()]
        assert result.tables_created == expected
        # 015_atomic_deduct.sql et al. are present (would be missing if hardcoded).
        assert "015_atomic_deduct.sql" in result.tables_created
        assert "013_list_usage_events.sql" in result.tables_created

    def test_check_feature(self) -> None:
        store = MemoryStore()
        store.setup()
        store.set_active_pricing(
            PricingConfigData(
                models={"_default": "1"},
                plans={
                    "pro": PlanDefinition(
                        id="pro",
                        name="Pro",
                        free_allowance=Decimal("500"),
                        features={"ai_chat": True, "max_roadmaps": 20},
                    ),
                },
            )
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
            PricingConfigData(
                models={"_default": "1"},
                plans={"pro": PlanDefinition(id="pro", name="Pro", free_allowance=Decimal("10"))},
            )
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
            PricingConfigData(
                models={"_default": "1"},
                plans={"pro": PlanDefinition(id="pro", name="Pro", free_allowance=Decimal("10"))},
            )
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
            PricingConfigData(
                models={"_default": "1"},
                plans={
                    "pro": PlanDefinition(
                        id="pro",
                        name="Pro Plan",
                        free_allowance=Decimal("500"),
                        features={"ai_chat": True, "max_roadmaps": 20},
                    ),
                },
            )
        )
        # set_user_plan resolves "pro" plan_key to credit_plans UUID internally
        store.set_user_plan(_PG_USER, "pro")

        result = store.get_user_plan(_PG_USER)
        assert result.plan_name == "Pro Plan"
        assert result.features["ai_chat"] is True
        assert result.features["max_roadmaps"] == 20

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

    def test_list_user_transactions_pg(self, store: PostgresStore) -> None:
        store.add_credits(_PG_USER, Decimal("1000"), "purchase", CreditMetadata(reference_id=None))
        store.add_credits(_PG_USER, Decimal("500"), "signup_bonus")
        r = store.reserve_credits(_PG_USER, Decimal("200"), "usage")
        store.deduct_credits(_PG_USER, r.reservation_id, Decimal("200"), metadata=CreditMetadata(model="gpt-4"))
        result = store.list_user_transactions(_PG_USER)
        assert len(result) == 3
        assert result[0].total_count == 3
        assert sum(1 for t in result if t.type == "usage") == 1

        # pagination
        page = store.list_user_transactions(_PG_USER, limit=1, offset=0)
        assert len(page) == 1
        assert page[0].total_count == 3

        page2 = store.list_user_transactions(_PG_USER, limit=2, offset=1)
        assert len(page2) == 2
        assert page2[0].total_count == 3

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
            PricingConfigData(
                models={"_default": "1"},
                plans={
                    "basic": PlanDefinition(
                        id="basic",
                        name="Basic",
                        free_allowance=Decimal("5"),
                    )
                },
            )
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
            PricingConfigData(
                models={"_default": "1"},
                plans={
                    "basic": PlanDefinition(
                        id="basic",
                        name="Basic",
                        free_allowance=Decimal("5"),
                    )
                },
            )
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
            PricingConfigData(
                models={"_default": "1"},
                plans={
                    "basic": PlanDefinition(
                        id="basic",
                        name="Basic",
                        free_allowance=Decimal("10"),
                    )
                },
            )
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

    # -- error-envelope handling (M10) --------------------------------------

    def test_unexpected_error_envelope_raises_store_error(self, store: HttpxSupabaseStore) -> None:
        # A non-business error code (e.g. a Postgres detail) must raise, not be
        # silently swallowed into a result model.
        self._mock_post(store, {"error": 'syntax error at or near "x"'})
        with pytest.raises(StoreError, match="returned error"):
            store.get_balance("u1")

    def test_business_error_envelope_not_raised_for_reserve(self, store: HttpxSupabaseStore) -> None:
        # A known business code is returned on the result model, not raised.
        self._mock_post(store, {"error": "insufficient_credits"})
        result = store.reserve_credits("u1", Decimal("999"), operation_type="usage")
        assert result.error == "insufficient_credits"
        assert result.amount == Decimal("0")

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

    def test_reserve_credits(self, store: HttpxSupabaseStore) -> None:
        self._mock_post(
            store,
            {"reservation_id": "res_1", "user_id": "u1", "amount": 30, "balance": 70, "reserved": 30},
        )
        result = store.reserve_credits("u1", Decimal("30"), operation_type="usage")
        assert result.reservation_id == "res_1"
        assert result.amount == Decimal("30")

    def test_deduct_credits(self, store: HttpxSupabaseStore) -> None:
        self._mock_post(
            store,
            {"id": "tx_2", "user_id": "u1", "amount": -10, "new_balance": 90, "idempotent": False},
        )
        result = store.deduct_credits("u1", "res_1", Decimal("10"))
        assert result.transaction_id == "tx_2"
        assert result.balance_after == Decimal("90")
        assert not result.idempotent

    def test_get_active_pricing(self, store: HttpxSupabaseStore) -> None:
        self._mock_post(
            store,
            {"id": "1", "config": {"models": {"a": "b"}}, "is_active": True},
        )
        result = store.get_active_pricing()
        assert result is not None
        assert result.config.models == {"a": "b"}
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
        config = PricingConfigData(models={"_default": "1"})
        result = store.set_active_pricing(config, label="v1")
        assert result == "cfg_1"
        call = mock.call_args
        assert call.args[0] == "https://test.supabase.co/rest/v1/rpc/set_active_pricing_config"
        assert call.kwargs["json"]["p_label"] == "v1"
        assert "models" in call.kwargs["json"]["p_config"]
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
                "plan_name": "Pro Plan",
                "free_allowance": 500,
                "features": {"ai_chat": True, "max_roadmaps": 20},
            },
        )
        result = store.get_user_plan("u1")
        assert result.plan_id == "pro"
        assert result.free_allowance == Decimal("500")
        assert result.features["ai_chat"] is True
        assert result.features["max_roadmaps"] == 20

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

    def test_settle_declamped_overdraft_goes_negative(self, store: PostgresStore) -> None:
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
        # Actual 60 > hold 10 → de-clamped (D5); balance goes to -60 (past the floor).
        ded = store.settle_lease(_PG_USER, lease.lease_id, Decimal("60"))
        assert ded.error is None
        assert ded.balance_after == Decimal("-60")
        # New admission rejected once available ≤ floor.
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
            PricingConfigData(
                models={"_default": "input_tokens * 1"},
                min_balance=Decimal("0"),
                plans={
                    "pro": PlanDefinition(
                        id="pro",
                        name="Pro",
                        default_billing_mode="overdraft",
                        max_concurrent=3,
                        overdraft_floor=Decimal("-25"),
                    )
                },
            )
        )
        store.add_credits(_PG_USER, Decimal("0"), "adjustment")
        store.set_user_plan(_PG_USER, "pro")
        plan = store.get_user_plan(_PG_USER)
        assert plan.default_billing_mode == "overdraft"
        assert plan.max_concurrent == 3
        assert plan.overdraft_floor == Decimal("-25")

    def test_manager_reserve_settle_flow_pg(self, store: PostgresStore) -> None:
        m = CreditManager(store=store, policy="strict_prepaid")
        m.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}, "min_balance": 0})
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

    def test_concurrent_max_concurrent_pg(self, store: PostgresStore) -> None:
        store.add_credits(_PG_USER, Decimal("10000"), "purchase")

        def one(_: int) -> object:
            s = PostgresStore(store._database_url)
            return s.create_lease(_PG_USER, Decimal("1"), "chat", floor=Decimal("0"), max_concurrent=5)

        with ThreadPoolExecutor(max_workers=16) as ex:
            results = list(ex.map(one, range(40)))
        assert sum(1 for r in results if r.error is None) == 5  # type: ignore[attr-defined]

    def test_concurrent_settle_same_key_one_debit_pg(self, store: PostgresStore) -> None:
        store.add_credits(_PG_USER, Decimal("100"), "purchase")
        lease = store.create_lease(_PG_USER, Decimal("50"), "usage", floor=Decimal("0"))

        def one(_: int) -> object:
            s = PostgresStore(store._database_url)
            return s.settle_lease(_PG_USER, lease.lease_id, Decimal("50"), idempotency_key="k")

        with ThreadPoolExecutor(max_workers=12) as ex:
            list(ex.map(one, range(12)))
        assert store.get_balance(_PG_USER).balance == Decimal("50")  # charged exactly once

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
            PricingConfigData(
                models={"_default": "input_tokens * 1"},
                min_balance=Decimal("0"),
                plans={"free": PlanDefinition(id="free", name="Free", free_allowance=Decimal("10"))},
            )
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
