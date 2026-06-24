"""Integration tests for each storage provider with a mock LLM call.

Spins up a real Postgres process via pytest-postgresql (pg_tmp) for
PostgresStore and HttpxSupabaseStore.setup() — no Docker or Supabase needed.
MemoryStore needs zero infra.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from ducto import CreditManager, UsageMetrics
from ducto.interface.memory import MemoryStore
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

# 100 input + 50 output tokens @ gpt-4 = 100*0.01 + 50*0.03 = 2.5 → int = 2
_METRICS = UsageMetrics(model="gpt-4", input_tokens=100, output_tokens=50)
_EXPECTED_COST = 2


def _add_and_deduct(manager: CreditManager, user_id: str = "u1") -> None:
    """Shared helper: add credits → deduct → verify."""
    manager.add_credits(user_id, 100)

    result = manager.deduct(user_id, _METRICS, idempotency_key="tx_1")
    assert result.amount == -_EXPECTED_COST
    assert result.balance_after == 100 - _EXPECTED_COST

    balance = manager.get_balance(user_id)
    assert balance.balance == 100 - _EXPECTED_COST


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
        from ducto.manager import InsufficientCreditsError

        with pytest.raises(InsufficientCreditsError, match="Credit reservation failed"):
            manager.deduct("user_1", _METRICS)

    def test_reserve_and_release(self, manager: CreditManager) -> None:
        manager.add_credits("user_1", 100)
        r = manager.reserve_credits("user_1", 30)
        assert r.error is None
        assert r.amount == 30

        # over-reserve → rejected
        r2 = manager.reserve_credits("user_1", 80)
        assert r2.error == "insufficient_credits"


# ═══════════════════════════════════════════════════════════════════════════
# PostgresStore
# ═══════════════════════════════════════════════════════════════════════════


class TestPostgresStoreIntegration:
    """Full credit lifecycle via PostgresStore + real Postgres (pg_tmp)."""

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
        result = store.setup()
        assert result.success

    def test_full_flow_pg(self, manager: CreditManager) -> None:
        _add_and_deduct(manager, _PG_USER)

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


# ═══════════════════════════════════════════════════════════════════════════
# HttpxSupabaseStore
# ═══════════════════════════════════════════════════════════════════════════


class TestHttpxSupabaseStoreIntegration:
    """HttpxSupabaseStore: setup via real Postgres, runtime via HTTP mocks.

    True Supabase integration requires a running Supabase project.  Here we
    test the two halves independently:
    - ``setup()`` runs SQL migrations against a real Postgres (same SQL that
      Supabase's PostgREST would execute).
    - Runtime methods are tested against mocked HTTP responses to verify
      correct URL construction, headers, and response parsing.
    """

    # -- Real Postgres: migrations ------------------------------------------

    def test_setup_with_database_url(self, pg_database_url: str) -> None:
        """setup() runs migrations via raw psycopg2."""
        store = HttpxSupabaseStore(url="http://localhost", key="irrelevant")
        result = store.setup(database_url=pg_database_url)
        assert result.success
        assert len(result.tables_created) > 0

    def test_setup_requires_database_url(self) -> None:
        store = HttpxSupabaseStore(url="http://localhost", key="x")
        with pytest.raises(RuntimeError, match="requires database_url"):
            store.setup(database_url=None)

    # -- Mocked HTTP: runtime operations ------------------------------------

    @pytest.fixture
    def store(self) -> HttpxSupabaseStore:
        return HttpxSupabaseStore(url="https://test.supabase.co", key="test-key")

    def _mock_post(self, store: HttpxSupabaseStore, return_value: object) -> MagicMock:
        """Patch ``httpx.Client.post`` and return the mock."""
        patcher = patch.object(store._http, "post")
        mock = patcher.start()
        resp = MagicMock()
        resp.json.return_value = return_value
        mock.return_value = resp
        self._patchers.append(patcher)
        return mock

    @pytest.fixture(autouse=True)
    def _cleanup_patches(self):
        self._patchers = []
        yield
        for p in self._patchers:
            p.stop()

    def test_rpc_correct_headers(self, store: HttpxSupabaseStore) -> None:
        """Verifies the exact HTTP call structure."""
        mock = self._mock_post(store, {"balance": 0, "user_id": "u1", "lifetime_purchased": 0})
        store.get_balance("u1")

        mock.assert_called_once_with(
            "https://test.supabase.co/rest/v1/rpc/get_credits_balance",
            json={"p_user_id": "u1"},
            headers={
                "apikey": "test-key",
                "authorization": "Bearer test-key",
                "content-type": "application/json",
            },
        )

    def test_get_balance(self, store: HttpxSupabaseStore) -> None:
        self._mock_post(store, {"user_id": "u1", "balance": 100, "lifetime_purchased": 50})
        result = store.get_balance("u1")
        assert result.balance == 100
        assert result.lifetime_purchased == 50

    def test_add_credits(self, store: HttpxSupabaseStore) -> None:
        self._mock_post(
            store,
            {"id": "tx_1", "user_id": "u1", "amount": 50, "new_balance": 150, "lifetime_purchased": 50},
        )
        result = store.add_credits("u1", 50)
        assert result.transaction_id == "tx_1"
        assert result.new_balance == 150

    def test_reserve_credits(self, store: HttpxSupabaseStore) -> None:
        self._mock_post(
            store,
            {"reservation_id": "res_1", "user_id": "u1", "amount": 30, "balance": 70, "reserved": 30},
        )
        result = store.reserve_credits("u1", 30, operation_type="usage")
        assert result.reservation_id == "res_1"
        assert result.amount == 30

    def test_reserve_credits_error(self, store: HttpxSupabaseStore) -> None:
        self._mock_post(store, {"error": "insufficient_credits"})
        result = store.reserve_credits("u1", 999, operation_type="usage")
        assert result.error == "insufficient_credits"
        assert result.amount == 0

    def test_deduct_credits(self, store: HttpxSupabaseStore) -> None:
        self._mock_post(
            store,
            {"id": "tx_2", "user_id": "u1", "amount": -10, "new_balance": 90, "idempotent": False},
        )
        result = store.deduct_credits("u1", "res_1", 10)
        assert result.transaction_id == "tx_2"
        assert result.balance_after == 90
        assert not result.idempotent

    def test_get_active_pricing(self, store: HttpxSupabaseStore) -> None:
        self._mock_post(
            store,
            {"id": "1", "config": {"version": 1, "models": {"a": "b"}}, "is_active": True},
        )
        result = store.get_active_pricing()
        assert result is not None
        assert result.config.models == {"a": "b"}
        assert result.id == "1"

    def test_get_active_pricing_none(self, store: HttpxSupabaseStore) -> None:
        self._mock_post(store, None)
        result = store.get_active_pricing()
        assert result is None

    def test_set_active_pricing(self, store: HttpxSupabaseStore) -> None:
        from ducto.interface.models import PricingConfigData

        self._mock_post(store, {"id": "cfg_1"})
        config = PricingConfigData(models={"_default": "1"})
        result = store.set_active_pricing(config, label="v1")
        assert result == "cfg_1"
