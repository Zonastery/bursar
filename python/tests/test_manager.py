"""Tests for CreditManager orchestration layer."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from ducto import CreditManager, UsageMetrics
from ducto.events import CreditEvent, CreditEventEmitter
from ducto.interface.memory import MemoryStore
from ducto.interface.models import PlanDefinition, PricingConfigData, SpendCap
from ducto.manager import InsufficientCreditsError, PricingNotLoadedError


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore()


@pytest.fixture
def manager(store: MemoryStore) -> CreditManager:
    m = CreditManager(store=store)
    m.publish_pricing_from_dict(
        {
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

        result = manager.add_credits("user_1", 50, tx_type="purchase")

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
        v2 = PricingConfigData(
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
        v2 = PricingConfigData(
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

    def test_full_refund_through_manager(self, manager: CreditManager) -> None:
        manager.add_credits("user_1", 100)
        deduct = manager.deduct(
            user_id="user_1",
            metrics=UsageMetrics(model="gpt-4", input_tokens=100, output_tokens=0),
        )
        assert deduct.amount == -1

        refund = manager.refund_credits(deduct.transaction_id)
        assert refund.error is None
        assert refund.amount == 1

        balance = manager.get_balance("user_1")
        assert balance.balance == 100

    def test_partial_refund_through_manager_fixed(self, manager: CreditManager) -> None:
        """Refund via deduct_fixed path."""
        manager.add_credits("user_1", 100)
        deduct = manager.deduct_fixed(user_id="user_1", job_name="batch_job")
        assert deduct.amount == -20

        refund = manager.refund_credits(deduct.transaction_id, amount=10)
        assert refund.error is None
        assert refund.amount == 10
        assert manager.get_balance("user_1").balance == 90  # 100 - 20 + 10

    def test_no_plan_uses_balance_only(self) -> None:
        """Without plan, existing deduct flow works unchanged."""
        store = MemoryStore()
        mgr = CreditManager(store=store)
        mgr.publish_pricing_from_dict(
            {
                "models": {"_default": "input_tokens * 1"},
            }
        )
        mgr.add_credits("user_1", 50)

        result = mgr.deduct("user_1", UsageMetrics(input_tokens=10))
        assert result.amount == -10
        assert result.balance_after == 40


class TestUsageAnalytics:
    def test_aggregate_stats_through_manager(self, manager: CreditManager) -> None:
        manager.add_credits("user_1", 100)
        manager.add_credits("user_2", 100)
        manager.deduct(
            user_id="user_1",
            metrics=UsageMetrics(model="gpt-4", input_tokens=100, output_tokens=0),
        )
        now = datetime.now()
        stats = manager.aggregate_stats(now - timedelta(seconds=10), now + timedelta(seconds=10))
        assert stats.total_credits_consumed >= 1
        assert stats.active_users >= 1
        assert stats.top_model != ""
        assert stats.top_user != ""

    def test_aggregate_stats_empty_window(self, manager: CreditManager) -> None:
        stats = manager.aggregate_stats(datetime(2020, 1, 1), datetime(2020, 1, 2))
        assert stats.total_credits_consumed == 0
        assert stats.active_users == 0
        assert stats.top_model == ""
        assert stats.top_user == ""

    def test_spend_by_user_through_manager(self, manager: CreditManager) -> None:
        manager.add_credits("user_1", 100)
        manager.deduct(
            user_id="user_1",
            metrics=UsageMetrics(model="gpt-4", input_tokens=100, output_tokens=0),
        )
        now = datetime.now()
        rows = manager.spend_by_user(now - timedelta(seconds=10), now + timedelta(seconds=10))
        assert len(rows) >= 1
        assert rows[0].user_id == "user_1"
        assert rows[0].total_spend > 0

    def test_spend_by_model_through_manager(self, manager: CreditManager) -> None:
        manager.add_credits("user_1", 100)
        manager.deduct(
            user_id="user_1",
            metrics=UsageMetrics(model="gpt-4", input_tokens=100, output_tokens=0),
        )
        now = datetime.now()
        rows = manager.spend_by_model(now - timedelta(seconds=10), now + timedelta(seconds=10))
        assert len(rows) >= 1

    def test_top_users_through_manager(self, manager: CreditManager) -> None:
        manager.add_credits("user_1", 100)
        manager.deduct(
            user_id="user_1",
            metrics=UsageMetrics(model="gpt-4", input_tokens=100, output_tokens=0),
        )
        now = datetime.now()
        rows = manager.top_users(5, now - timedelta(seconds=10), now + timedelta(seconds=10))
        assert len(rows) >= 1

    def test_daily_spend_through_manager(self, manager: CreditManager) -> None:
        manager.add_credits("user_1", 100)
        manager.deduct(
            user_id="user_1",
            metrics=UsageMetrics(model="gpt-4", input_tokens=100, output_tokens=0),
        )
        now = datetime.now()
        rows = manager.daily_spend(now - timedelta(days=1), now + timedelta(days=1))
        assert len(rows) >= 1
        assert rows[0].total_spend > 0


class TestCreditExpiry:
    def test_sweep_expired_through_manager(self) -> None:
        store = MemoryStore()
        mgr = CreditManager(store=store)
        mgr.publish_pricing_from_dict(
            {
                "models": {"_default": "input_tokens * 1"},
            }
        )
        expires_at = datetime.now().replace(second=0)
        mgr.add_credits("user_1", 100, "purchase", expires_at=expires_at)

        result = mgr.sweep_expired_credits()
        assert result.expired_count == 1
        assert result.expired_amount == 100
        assert mgr.get_balance("user_1").balance == 0

    def test_dry_run_through_manager(self) -> None:
        store = MemoryStore()
        mgr = CreditManager(store=store)
        mgr.publish_pricing_from_dict(
            {
                "models": {"_default": "input_tokens * 1"},
            }
        )
        expires_at = datetime.now().replace(second=0)
        mgr.add_credits("user_1", 100, "purchase", expires_at=expires_at)

        result = mgr.sweep_expired_credits(dry_run=True)
        assert result.expired_count == 1
        assert result.dry_run is True
        assert mgr.get_balance("user_1").balance == 100


class TestTeamDeduct:
    def test_deduct_team_calculates_cost_and_debits_team(self) -> None:
        store = MemoryStore()
        mgr = CreditManager(store=store)
        mgr.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}})
        team = store.create_team("Team", 500)
        store.add_team_member(team.team_id, "user-1")

        result = mgr.deduct_team(team.team_id, "user-1", UsageMetrics(input_tokens=100))
        assert result.amount == -100
        assert result.team_balance_after == 400
        assert result.transaction_id != ""

    def test_deduct_team_zero_cost_noop(self) -> None:
        store = MemoryStore()
        mgr = CreditManager(store=store)
        mgr.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}})
        team = store.create_team("Team", 500)
        store.add_team_member(team.team_id, "user-1")

        result = mgr.deduct_team(team.team_id, "user-1", UsageMetrics(input_tokens=0))
        assert result.amount == 0
        assert result.team_balance_after == 500

    def test_deduct_team_requires_pricing_loaded(self) -> None:
        store = MemoryStore()
        mgr = CreditManager(store=store)
        with pytest.raises(PricingNotLoadedError):
            mgr.deduct_team("team-1", "user-1", UsageMetrics(input_tokens=100))

    def test_deduct_team_insufficient_balance(self) -> None:
        store = MemoryStore()
        mgr = CreditManager(store=store)
        mgr.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}})
        team = store.create_team("Poor Team", 10)
        store.add_team_member(team.team_id, "user-1")

        result = mgr.deduct_team(team.team_id, "user-1", UsageMetrics(input_tokens=100))
        assert result.error == "insufficient_team_balance"

    def test_deduct_team_user_not_in_team(self) -> None:
        store = MemoryStore()
        mgr = CreditManager(store=store)
        mgr.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}})
        team = store.create_team("Closed Team", 500)
        result = mgr.deduct_team(team.team_id, "user-1", UsageMetrics(input_tokens=10))
        assert result.error == "user_not_in_team"


class TestSpendCapsManager:
    def test_daily_deny_cap_blocks_deduction(self) -> None:
        store = MemoryStore()
        mgr = CreditManager(store=store)
        mgr.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}})
        store.add_credits("user-1", 1000)
        store.set_spend_cap(SpendCap(user_id="user-1", cap_type="daily", limit=10, action="deny"))

        with pytest.raises(InsufficientCreditsError, match="Spend cap exceeded"):
            mgr.deduct("user-1", UsageMetrics(model="gpt-4", input_tokens=11))

    def test_warn_action_allows_deduction(self) -> None:
        store = MemoryStore()
        mgr = CreditManager(store=store)
        mgr.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}})
        store.add_credits("user-1", 1000)
        store.set_spend_cap(SpendCap(user_id="user-1", cap_type="daily", limit=10, action="warn"))

        result = mgr.deduct("user-1", UsageMetrics(model="gpt-4", input_tokens=11))
        assert result.transaction_id != ""

    def test_notify_action_allows_deduction(self) -> None:
        store = MemoryStore()
        mgr = CreditManager(store=store)
        mgr.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}})
        store.add_credits("user-1", 1000)
        store.set_spend_cap(SpendCap(user_id="user-1", cap_type="daily", limit=10, action="notify"))

        result = mgr.deduct("user-1", UsageMetrics(model="gpt-4", input_tokens=11))
        assert result.transaction_id != ""

    def test_cap_within_limit_allows_deduction(self) -> None:
        store = MemoryStore()
        mgr = CreditManager(store=store)
        mgr.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}})
        store.add_credits("user-1", 1000)
        store.set_spend_cap(SpendCap(user_id="user-1", cap_type="daily", limit=100, action="deny"))

        result = mgr.deduct("user-1", UsageMetrics(model="gpt-4", input_tokens=5))
        assert result.transaction_id != ""


class TestEventSystem:
    """Tests for CreditManager event emission."""

    def test_emits_deducted_event(self) -> None:
        store = MemoryStore()
        emitter = CreditEventEmitter()
        mgr = CreditManager(store=store, emitter=emitter)
        mgr.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}})
        mgr.add_credits("user-1", 100)

        events: list[CreditEvent] = []
        emitter.on("credits.deducted", lambda e: events.append(e))

        mgr.deduct("user-1", UsageMetrics(input_tokens=10))
        assert len(events) == 1
        assert events[0].type == "credits.deducted"
        assert events[0].user_id == "user-1"

    def test_emits_added_event(self) -> None:
        emitter = CreditEventEmitter()
        mgr = CreditManager(store=MemoryStore(), emitter=emitter)
        mgr.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}})

        events: list[CreditEvent] = []
        emitter.on("credits.added", lambda e: events.append(e))

        mgr.add_credits("user-1", 50)
        assert len(events) == 1
        assert events[0].data is not None
        assert events[0].data["amount"] == 50

    def test_emits_refunded_event(self) -> None:
        store = MemoryStore()
        emitter = CreditEventEmitter()
        mgr = CreditManager(store=store, emitter=emitter)
        mgr.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}})
        mgr.add_credits("user-1", 100)

        events: list[CreditEvent] = []
        emitter.on("credits.refunded", lambda e: events.append(e))

        deduct = mgr.deduct("user-1", UsageMetrics(input_tokens=10))
        mgr.refund_credits(deduct.transaction_id)
        assert len(events) == 1

    def test_emits_cap_reached_on_deny(self) -> None:
        store = MemoryStore()
        emitter = CreditEventEmitter()
        mgr = CreditManager(store=store, emitter=emitter)
        mgr.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}})
        mgr.add_credits("user-1", 100)
        store.set_spend_cap(SpendCap(user_id="user-1", cap_type="daily", limit=5, action="deny"))

        events: list[CreditEvent] = []
        emitter.on("credits.cap_reached", lambda e: events.append(e))

        with pytest.raises(InsufficientCreditsError):
            mgr.deduct("user-1", UsageMetrics(input_tokens=10))
        assert len(events) == 1
        assert events[0].type == "credits.cap_reached"

    def test_emits_expired_event_on_sweep(self) -> None:
        store = MemoryStore()
        emitter = CreditEventEmitter()
        mgr = CreditManager(store=store, emitter=emitter)
        mgr.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}})

        events: list[CreditEvent] = []
        emitter.on("credits.expired", lambda e: events.append(e))

        from datetime import timedelta

        mgr.add_credits("user-1", 100, expires_at=datetime.now() - timedelta(seconds=1))
        mgr.sweep_expired_credits()
        assert len(events) == 1
        assert events[0].data is not None
        assert events[0].data["expired_count"] == 1

    def test_multiple_handlers_all_fire(self) -> None:
        store = MemoryStore()
        emitter = CreditEventEmitter()
        mgr = CreditManager(store=store, emitter=emitter)
        mgr.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}})
        mgr.add_credits("user-1", 100)

        called: list[int] = []
        emitter.on("credits.deducted", lambda _: called.append(1))
        emitter.on("credits.deducted", lambda _: called.append(2))

        mgr.deduct("user-1", UsageMetrics(input_tokens=10))
        assert len(called) == 2

    def test_unregistered_event_noop(self) -> None:
        emitter = CreditEventEmitter()
        # Should not raise
        emitter.emit(CreditEvent(type="credits.deducted", timestamp=datetime.now(), user_id="u1"))

    def test_off_removes_handler(self) -> None:
        emitter = CreditEventEmitter()
        store = MemoryStore()
        mgr = CreditManager(store=store, emitter=emitter)
        mgr.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}})
        mgr.add_credits("user-1", 100)

        called: list[str] = []

        def handler(e: CreditEvent) -> None:
            called.append(e.type)

        emitter.on("credits.deducted", handler)
        emitter.off("credits.deducted", handler)

        mgr.deduct("user-1", UsageMetrics(input_tokens=10))
        assert len(called) == 0

    def test_emits_cap_warning_on_warn(self) -> None:
        store = MemoryStore()
        emitter = CreditEventEmitter()
        mgr = CreditManager(store=store, emitter=emitter)
        mgr.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}})
        mgr.add_credits("user-1", 100)
        store.set_spend_cap(SpendCap(user_id="user-1", cap_type="daily", limit=5, action="warn"))

        events: list[CreditEvent] = []
        emitter.on("credits.cap_warning", lambda e: events.append(e))

        mgr.deduct("user-1", UsageMetrics(input_tokens=10))
        assert len(events) == 1
        assert events[0].type == "credits.cap_warning"
