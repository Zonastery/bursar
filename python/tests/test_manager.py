"""Tests for CreditManager orchestration layer."""

from __future__ import annotations

import pytest

from ducto import CreditManager, UsageMetrics
from ducto.interface.memory import MemoryStore
from ducto.interface.models import PlanDefinition, PricingConfigV2
from ducto.manager import InsufficientCreditsError, PricingNotLoadedError


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore()


@pytest.fixture
def manager(store: MemoryStore) -> CreditManager:
    m = CreditManager(store=store)
    m.publish_pricing_from_dict(
        {
            "version": 1,
            "models": {
                "gpt-4": "input_tokens * 0.01 + output_tokens * 0.03",
                "_default": "input_tokens * 0.001 + output_tokens * 0.003",
            },
            "tools": {"_default": "tool_calls * 0"},
            "fixed": {"batch_job": 20},
            "min_balance": 5,
        }
    )
    return m


class TestSetup:
    def test_setup_runs_idempotently(self, store: MemoryStore) -> None:
        manager = CreditManager(store=store)
        result = manager.setup()
        assert result.success
        assert len(result.tables_created) > 0

        # Second call should also succeed
        result2 = manager.setup()
        assert result2.success


class TestPricingLoading:
    def test_deduct_without_pricing_raises_error(self, store: MemoryStore) -> None:
        manager = CreditManager(store=store)
        with pytest.raises(PricingNotLoadedError, match="PricingEngine not loaded"):
            manager.deduct("user_1", UsageMetrics(model="gpt-4"))

    def test_load_from_store(self, store: MemoryStore) -> None:
        manager = CreditManager(store=store)
        manager.publish_pricing_from_dict(
            {
                "version": 1,
                "models": {"_default": "input_tokens * 1"},
            }
        )
        assert manager.engine is not None

        # Second manager loading from store
        manager2 = CreditManager(store=store)
        manager2.load_pricing_from_store()
        assert manager2.engine is not None


class TestDeduct:
    def test_deduct_basic(self, manager: CreditManager) -> None:
        manager.add_credits("user_1", 100)

        result = manager.deduct(
            user_id="user_1",
            metrics=UsageMetrics(model="gpt-4", input_tokens=100, output_tokens=50),
            idempotency_key="test_1",
        )

        # Cost: 100*0.01 + 50*0.03 = 1 + 1.5 = 2.5 → int(2.5) = 2
        expected_cost = 2
        assert result.amount == -expected_cost
        assert result.balance_after == 100 - expected_cost
        assert not result.idempotent
        assert result.transaction_id != ""

    def test_deduct_idempotent(self, manager: CreditManager) -> None:
        manager.add_credits("user_1", 100)

        result1 = manager.deduct(
            user_id="user_1",
            metrics=UsageMetrics(model="gpt-4", input_tokens=100, output_tokens=0),
            idempotency_key="same_key",
        )

        result2 = manager.deduct(
            user_id="user_1",
            metrics=UsageMetrics(model="gpt-4", input_tokens=100, output_tokens=0),
            idempotency_key="same_key",
        )

        assert result2.idempotent
        assert result2.transaction_id == result1.transaction_id
        assert result2.balance_after == result1.balance_after

    def test_deduct_insufficient_credits(self, manager: CreditManager) -> None:
        manager.add_credits("user_1", 2)  # Not enough for gpt-4 model (min 1 + min_balance 5)

        with pytest.raises(InsufficientCreditsError, match="Credit reservation failed"):
            manager.deduct(
                user_id="user_1",
                metrics=UsageMetrics(model="gpt-4", input_tokens=100, output_tokens=100),
            )

    def test_deduct_zero_cost_noop(self, manager: CreditManager) -> None:
        """Zero-cost operations deduct nothing."""
        manager.add_credits("user_1", 100)

        result = manager.deduct(
            user_id="user_1",
            metrics=UsageMetrics(model="gpt-4", input_tokens=0, output_tokens=0),
        )

        assert result.amount == 0

    def test_deduct_fixed_job(self, manager: CreditManager) -> None:
        manager.add_credits("user_1", 100)

        result = manager.deduct_fixed(
            user_id="user_1",
            job_name="batch_job",
            idempotency_key="roadmap_1",
        )

        assert abs(result.amount) == 20
        assert result.balance_after == 80

    def test_multiple_deductions(self, manager: CreditManager) -> None:
        manager.add_credits("user_1", 50)

        for i in range(3):
            result = manager.deduct(
                user_id="user_1",
                metrics=UsageMetrics(model="gpt-4", input_tokens=100, output_tokens=0),
                idempotency_key=f"tx_{i}",
            )
            assert result.balance_after == 50 - ((i + 1) * 1)


class TestAddCredits:
    def test_add_credits_increases_balance(self, manager: CreditManager) -> None:
        balance_before = manager.get_balance("user_1").balance

        result = manager.add_credits("user_1", 50, type="purchase")

        assert result.new_balance == balance_before + 50
        assert result.lifetime_purchased == 50

    def test_multiple_adds_accumulate(self, manager: CreditManager) -> None:
        manager.add_credits("user_1", 30)
        manager.add_credits("user_1", 20)

        balance = manager.get_balance("user_1")
        assert balance.balance == 50


class TestGetBalance:
    def test_new_user_has_zero_balance(self, store: MemoryStore) -> None:
        manager = CreditManager(store=store)
        balance = manager.get_balance("new_user")
        assert balance.balance == 0
        assert balance.lifetime_purchased == 0

    def test_balance_after_deductions(self, manager: CreditManager) -> None:
        manager.add_credits("user_1", 100)
        manager.deduct(
            user_id="user_1",
            metrics=UsageMetrics(model="gpt-4", input_tokens=100, output_tokens=0),
        )

        balance = manager.get_balance("user_1")
        assert balance.balance == 99  # 100 - 1


class TestReserve:
    def test_reserve_reduces_available(self, manager: CreditManager) -> None:
        manager.add_credits("user_1", 100)

        r1 = manager.reserve_credits("user_1", 30)
        assert r1.error is None
        assert r1.amount == 30

        # Requesting more than available returns an error
        r2 = manager.reserve_credits("user_1", 80)  # 80 > 70 → insufficient credits
        assert r2.error == "insufficient_credits"
        assert r2.amount == 0


class TestPlanAllowance:
    def test_full_allowance_covers_cost(self) -> None:
        """Deduct with full plan allowance skips balance deduction."""
        store = MemoryStore()
        v2 = PricingConfigV2(
            version=2,
            models={"_default": "input_tokens * 1"},
            plans={"free": PlanDefinition(id="free", name="Free", free_allowance=100)},
        )
        store.set_active_pricing(v2)
        store.set_user_plan("user_1", "free")
        store.add_credits("user_1", 10)

        mgr = CreditManager(store=store)
        mgr.publish_pricing_from_dict(v2)

        result = mgr.deduct("user_1", UsageMetrics(input_tokens=5))
        assert result.amount == 0
        assert result.transaction_id == ""  # no actual transaction
        assert result.balance_after == 10  # balance unchanged

        allowance = store.check_allowance("user_1")
        assert allowance.allowance_remaining == 95

    def test_partial_allowance_with_balance_deduct(self) -> None:
        """Plan covers part, remaining deducted from balance."""
        store = MemoryStore()
        v2 = PricingConfigV2(
            version=2,
            models={"_default": "input_tokens * 1"},
            plans={"starter": PlanDefinition(id="starter", name="Starter", free_allowance=10)},
        )
        store.set_active_pricing(v2)
        store.set_user_plan("user_1", "starter")
        store.add_credits("user_1", 100)

        mgr = CreditManager(store=store)
        mgr.publish_pricing_from_dict(v2)

        result = mgr.deduct("user_1", UsageMetrics(input_tokens=25))
        assert result.amount == -15  # 10 from allowance, 15 from balance
        assert result.balance_after == 85
        assert result.transaction_id != ""

        allowance = store.check_allowance("user_1")
        assert allowance.allowance_remaining == 0

    def test_no_plan_uses_balance_only(self) -> None:
        """Without plan, existing deduct flow works unchanged."""
        store = MemoryStore()
        mgr = CreditManager(store=store)
        mgr.publish_pricing_from_dict(
            {
                "version": 1,
                "models": {"_default": "input_tokens * 1"},
            }
        )
        mgr.add_credits("user_1", 50)

        result = mgr.deduct("user_1", UsageMetrics(input_tokens=10))
        assert result.amount == -10
        assert result.balance_after == 40
