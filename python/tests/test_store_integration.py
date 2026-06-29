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

import psycopg2
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
