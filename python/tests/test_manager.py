"""Tests for CreditManager orchestration layer.

Money is exact ``Decimal`` everywhere (contract §1): assertions use exact
Decimal values, never truthiness or ``> 0`` where the value is deterministic.
Time-dependent tests use timezone-aware UTC datetimes (the store windows are
tz-aware UTC, M9) — see ``_TZ_FIXES`` note below.

``deduct()`` now charges through the atomic ``store.deduct_with_allowance`` RPC
(contract §2, C1). Its ``DeductionResult.amount`` is the **net positive** charge
to the balance (gross minus free allowance), which is the sign convention of the
new RPC — the legacy negative-amount convention is gone.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ducto import CreditManager, UsageMetrics
from ducto.events import CreditEvent, CreditEventEmitter
from ducto.interface.base import CapReachedError
from ducto.interface.memory import MemoryStore
from ducto.interface.models import PlanDefinition, PricingConfigData, SpendCap
from ducto.manager import InsufficientCreditsError, PricingNotLoadedError

# ──────────────────────────────────────────────────────────────────────────
# TZ FIXES (Phase-2a follow-up): the 7 pre-existing failures were tests that
# passed *naive local* ``datetime.now()`` into the now-tz-aware-UTC MemoryStore
# windows. The store interprets a naive bound as UTC, so a naive *local* clock
# that differs from UTC silently fell outside the window (analytics) or was read
# as a future/past expiry (sweep). Each such test below now uses ``_utcnow()``
# (tz-aware UTC) so the bound matches the store's ``_utcnow()``-stamped rows.
# ──────────────────────────────────────────────────────────────────────────


def _utcnow() -> datetime:
    """Timezone-aware UTC now, matching the store's internal clock (M9)."""
    return datetime.now(UTC)


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

        # Cost: 100*0.01 + 50*0.03 = 1 + 1.5 = 2.5 — charged EXACTLY (no truncation).
        assert result.amount == Decimal("2.5")
        assert result.balance_after == Decimal("97.5")
        assert not result.idempotent
        assert result.transaction_id != ""

    def test_deduct_fractional_cost_no_truncation(self, store: MemoryStore) -> None:
        """A sub-1-credit op (0.4) is charged 0.4, not 0 (H1 revenue leak)."""
        mgr = CreditManager(store=store)
        mgr.publish_pricing_from_dict({"models": {"_default": "input_tokens * 0.4"}, "min_balance": 0})
        mgr.add_credits("user_1", 100)

        result = mgr.deduct("user_1", UsageMetrics(input_tokens=1))
        assert result.amount == Decimal("0.4")
        assert result.balance_after == Decimal("99.6")

    def test_deduct_idempotent_replay_returns_original(self, manager: CreditManager) -> None:
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
        assert result2.amount == result1.amount
        assert result2.balance_after == result1.balance_after
        # Only one real debit happened.
        assert manager.get_balance("user_1").balance == Decimal("99")

    def test_deduct_idempotent_replay_ignores_different_amount(self, manager: CreditManager) -> None:
        """Same key + different amount → original result, no extra debit."""
        manager.add_credits("user_1", 100)

        first = manager.deduct(
            user_id="user_1",
            metrics=UsageMetrics(model="gpt-4", input_tokens=100, output_tokens=0),
            idempotency_key="dup",
        )
        assert first.amount == Decimal("1")

        replay = manager.deduct(
            user_id="user_1",
            metrics=UsageMetrics(model="gpt-4", input_tokens=900, output_tokens=0),  # would be 9
            idempotency_key="dup",
        )
        assert replay.idempotent
        assert replay.transaction_id == first.transaction_id
        assert replay.amount == Decimal("1")  # original, not 9
        assert manager.get_balance("user_1").balance == Decimal("99")

    def test_deduct_idempotent_no_cross_user_collision(self, manager: CreditManager) -> None:
        """Same key for two users → two independent debits (user-scoped key)."""
        manager.add_credits("user_a", 100)
        manager.add_credits("user_b", 100)

        ra = manager.deduct(
            user_id="user_a",
            metrics=UsageMetrics(model="gpt-4", input_tokens=100, output_tokens=0),
            idempotency_key="shared",
        )
        rb = manager.deduct(
            user_id="user_b",
            metrics=UsageMetrics(model="gpt-4", input_tokens=100, output_tokens=0),
            idempotency_key="shared",
        )

        assert not ra.idempotent
        assert not rb.idempotent
        assert ra.transaction_id != rb.transaction_id
        assert manager.get_balance("user_a").balance == Decimal("99")
        assert manager.get_balance("user_b").balance == Decimal("99")

    def test_deduct_insufficient_credits_raises_and_emits_failure(self, store: MemoryStore) -> None:
        emitter = CreditEventEmitter()
        mgr = CreditManager(store=store, emitter=emitter)
        mgr.publish_pricing_from_dict({"models": {"_default": "input_tokens * 0.01"}, "min_balance": 5})
        mgr.add_credits("user_1", 2)  # below min_balance floor

        failures: list[CreditEvent] = []
        successes: list[CreditEvent] = []
        emitter.on("credits.deduct_failed", failures.append)
        emitter.on("credits.deducted", successes.append)

        with pytest.raises(InsufficientCreditsError, match="Insufficient credits"):
            mgr.deduct("user_1", UsageMetrics(input_tokens=100))

        assert len(failures) == 1
        assert failures[0].data is not None
        assert failures[0].data["error"] == "insufficient_credits"
        # NEVER a success event on the error path.
        assert successes == []

    def test_deduct_zero_cost_noop(self, manager: CreditManager) -> None:
        """Zero-cost operations deduct nothing and short-circuit."""
        manager.add_credits("user_1", 100)

        result = manager.deduct(
            user_id="user_1",
            metrics=UsageMetrics(model="gpt-4", input_tokens=0, output_tokens=0),
        )

        assert result.amount == Decimal(0)
        assert result.transaction_id == ""
        assert result.balance_after == Decimal("100")

    def test_deduct_fixed_job(self, manager: CreditManager) -> None:
        manager.add_credits("user_1", 100)

        result = manager.deduct_fixed(
            user_id="user_1",
            job_name="batch_job",
            idempotency_key="roadmap_1",
        )

        assert result.amount == Decimal("20")
        assert result.balance_after == Decimal("80")

    def test_deduct_fixed_unknown_job_rejected(self, manager: CreditManager) -> None:
        """Unknown fixed job is rejected, not silently charged 0 (L1)."""
        manager.add_credits("user_1", 100)
        with pytest.raises(ValueError, match="Unknown fixed-cost job"):
            manager.deduct_fixed(user_id="user_1", job_name="does_not_exist")
        # Balance untouched.
        assert manager.get_balance("user_1").balance == Decimal("100")

    def test_multiple_deductions(self, manager: CreditManager) -> None:
        manager.add_credits("user_1", 50)

        for i in range(3):
            result = manager.deduct(
                user_id="user_1",
                metrics=UsageMetrics(model="gpt-4", input_tokens=100, output_tokens=0),
                idempotency_key=f"tx_{i}",
            )
            assert result.balance_after == Decimal(50 - ((i + 1) * 1))


class TestAddCredits:
    def test_add_credits_increases_balance(self, manager: CreditManager) -> None:
        balance_before = manager.get_balance("user_1").balance

        result = manager.add_credits("user_1", 50, tx_type="purchase")

        assert result.new_balance == balance_before + 50
        assert result.lifetime_purchased == Decimal("50")

    def test_multiple_adds_accumulate(self, manager: CreditManager) -> None:
        manager.add_credits("user_1", 30)
        manager.add_credits("user_1", 20)

        balance = manager.get_balance("user_1")
        assert balance.balance == Decimal("50")


class TestGetBalance:
    def test_new_user_has_zero_balance(self, store: MemoryStore) -> None:
        manager = CreditManager(store=store)
        balance = manager.get_balance("new_user")
        assert balance.balance == Decimal(0)
        assert balance.lifetime_purchased == Decimal(0)

    def test_balance_after_deductions(self, manager: CreditManager) -> None:
        manager.add_credits("user_1", 100)
        manager.deduct(
            user_id="user_1",
            metrics=UsageMetrics(model="gpt-4", input_tokens=100, output_tokens=0),
        )

        balance = manager.get_balance("user_1")
        assert balance.balance == Decimal("99")  # 100 - 1


class TestReserve:
    def test_reserve_reduces_available(self, manager: CreditManager) -> None:
        manager.add_credits("user_1", 100)

        r1 = manager.reserve_credits("user_1", 30)
        assert r1.error is None
        assert r1.amount == Decimal("30")

        # Requesting more than available (minus min_balance) returns an error.
        r2 = manager.reserve_credits("user_1", 80)  # 80 > 70-5 → insufficient credits
        assert r2.error == "insufficient_credits"
        assert r2.amount == Decimal(0)


class TestPlanAllowance:
    def test_full_allowance_covers_cost(self) -> None:
        """Deduct with full plan allowance skips balance deduction."""
        store = MemoryStore()
        v2 = PricingConfigData(
            models={"_default": "input_tokens * 1"},
            plans={"free": PlanDefinition(id="free", name="Free", free_allowance=Decimal(100))},
            min_balance=Decimal(0),
        )
        store.set_active_pricing(v2)
        store.set_user_plan("user_1", "free")
        store.add_credits("user_1", Decimal(10))

        mgr = CreditManager(store=store)
        mgr.publish_pricing_from_dict(v2)

        result = mgr.deduct("user_1", UsageMetrics(input_tokens=5))
        # Fully covered by allowance: net 0 charged, but a real (zero-net) tx row.
        assert result.amount == Decimal(0)
        assert result.allowance_consumed == Decimal("5")
        assert result.balance_after == Decimal("10")  # balance unchanged

        allowance = store.check_allowance("user_1")
        assert allowance.allowance_remaining == Decimal("95")

    def test_partial_allowance_with_balance_deduct(self) -> None:
        """Plan covers part, remaining deducted from balance."""
        store = MemoryStore()
        v2 = PricingConfigData(
            models={"_default": "input_tokens * 1"},
            plans={"starter": PlanDefinition(id="starter", name="Starter", free_allowance=Decimal(10))},
            min_balance=Decimal(0),
        )
        store.set_active_pricing(v2)
        store.set_user_plan("user_1", "starter")
        store.add_credits("user_1", Decimal(100))

        mgr = CreditManager(store=store)
        mgr.publish_pricing_from_dict(v2)

        result = mgr.deduct("user_1", UsageMetrics(input_tokens=25))
        # 10 from allowance, 15 net from balance.
        assert result.amount == Decimal("15")
        assert result.allowance_consumed == Decimal("10")
        assert result.balance_after == Decimal("85")
        assert result.transaction_id != ""

        allowance = store.check_allowance("user_1")
        assert allowance.allowance_remaining == Decimal(0)

    def test_full_refund_through_manager(self, manager: CreditManager) -> None:
        manager.add_credits("user_1", 100)
        deduct = manager.deduct(
            user_id="user_1",
            metrics=UsageMetrics(model="gpt-4", input_tokens=100, output_tokens=0),
        )
        assert deduct.amount == Decimal("1")

        refund = manager.refund_credits(deduct.transaction_id)
        assert refund.error is None
        assert refund.amount == Decimal("1")

        balance = manager.get_balance("user_1")
        assert balance.balance == Decimal("100")

    def test_partial_refund_through_manager_fixed(self, manager: CreditManager) -> None:
        """Refund via deduct_fixed path."""
        manager.add_credits("user_1", 100)
        deduct = manager.deduct_fixed(user_id="user_1", job_name="batch_job")
        assert deduct.amount == Decimal("20")

        refund = manager.refund_credits(deduct.transaction_id, amount=10)
        assert refund.error is None
        assert refund.amount == Decimal("10")
        assert manager.get_balance("user_1").balance == Decimal("90")  # 100 - 20 + 10

    def test_no_plan_uses_balance_only(self) -> None:
        """Without a plan, the deduct charges the full cost from balance."""
        store = MemoryStore()
        mgr = CreditManager(store=store)
        mgr.publish_pricing_from_dict(
            {
                "models": {"_default": "input_tokens * 1"},
                "min_balance": 0,
            }
        )
        mgr.add_credits("user_1", 50)

        result = mgr.deduct("user_1", UsageMetrics(input_tokens=10))
        assert result.amount == Decimal("10")
        assert result.balance_after == Decimal("40")


class TestRefundFailures:
    """Refund error paths reject and emit NO success event (H3/§4)."""

    def test_over_refund_rejected_no_success_event(self, store: MemoryStore) -> None:
        emitter = CreditEventEmitter()
        mgr = CreditManager(store=store, emitter=emitter)
        mgr.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}, "min_balance": 0})
        mgr.add_credits("user_1", 100)
        deduct = mgr.deduct("user_1", UsageMetrics(input_tokens=10))  # charges 10

        successes: list[CreditEvent] = []
        failures: list[CreditEvent] = []
        emitter.on("credits.refunded", successes.append)
        emitter.on("credits.refund_failed", failures.append)

        refund = mgr.refund_credits(deduct.transaction_id, amount=50)  # > 10
        assert refund.error == "over_refund"
        assert successes == []
        assert len(failures) == 1
        assert failures[0].data is not None
        assert failures[0].data["error"] == "over_refund"
        # Balance unchanged by the failed refund.
        assert mgr.get_balance("user_1").balance == Decimal("90")

    def test_duplicate_full_refund_rejected(self, store: MemoryStore) -> None:
        emitter = CreditEventEmitter()
        mgr = CreditManager(store=store, emitter=emitter)
        mgr.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}, "min_balance": 0})
        mgr.add_credits("user_1", 100)
        deduct = mgr.deduct("user_1", UsageMetrics(input_tokens=10))

        successes: list[CreditEvent] = []
        failures: list[CreditEvent] = []
        emitter.on("credits.refunded", successes.append)
        emitter.on("credits.refund_failed", failures.append)

        first = mgr.refund_credits(deduct.transaction_id)
        assert first.error is None
        second = mgr.refund_credits(deduct.transaction_id)  # duplicate
        assert second.error == "already_refunded"

        assert len(successes) == 1  # only the first emitted success
        assert len(failures) == 1
        assert failures[0].data is not None
        assert failures[0].data["error"] == "already_refunded"

    def test_refund_of_purchase_rejected(self, store: MemoryStore) -> None:
        emitter = CreditEventEmitter()
        mgr = CreditManager(store=store, emitter=emitter)
        mgr.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}})
        purchase = mgr.add_credits("user_1", 100, tx_type="purchase")

        successes: list[CreditEvent] = []
        failures: list[CreditEvent] = []
        emitter.on("credits.refunded", successes.append)
        emitter.on("credits.refund_failed", failures.append)

        refund = mgr.refund_credits(purchase.transaction_id)
        assert refund.error == "over_refund"  # nothing to give back from a credit
        assert successes == []
        assert len(failures) == 1


class TestUsageAnalytics:
    def test_aggregate_stats_through_manager(self, manager: CreditManager) -> None:
        manager.add_credits("user_1", 100)
        manager.add_credits("user_2", 100)
        manager.deduct(
            user_id="user_1",
            metrics=UsageMetrics(model="gpt-4", input_tokens=100, output_tokens=0),
        )
        now = _utcnow()  # TZ FIX: tz-aware UTC bound matches the store's UTC rows.
        stats = manager.aggregate_stats(now - timedelta(seconds=10), now + timedelta(seconds=10))
        assert stats.total_credits_consumed >= Decimal("1")
        assert stats.active_users >= 1
        assert stats.top_model != ""
        assert stats.top_user != ""

    def test_aggregate_stats_empty_window(self, manager: CreditManager) -> None:
        # TZ FIX: tz-aware UTC bounds (a window that contains no rows either way).
        stats = manager.aggregate_stats(datetime(2020, 1, 1, tzinfo=UTC), datetime(2020, 1, 2, tzinfo=UTC))
        assert stats.total_credits_consumed == Decimal(0)
        assert stats.active_users == 0
        assert stats.top_model == ""
        assert stats.top_user == ""

    def test_spend_by_user_through_manager(self, manager: CreditManager) -> None:
        manager.add_credits("user_1", 100)
        manager.deduct(
            user_id="user_1",
            metrics=UsageMetrics(model="gpt-4", input_tokens=100, output_tokens=0),
        )
        now = _utcnow()  # TZ FIX
        rows = manager.spend_by_user(now - timedelta(seconds=10), now + timedelta(seconds=10))
        assert len(rows) >= 1
        assert rows[0].user_id == "user_1"
        assert rows[0].total_spend == Decimal("1")

    def test_spend_by_model_through_manager(self, manager: CreditManager) -> None:
        manager.add_credits("user_1", 100)
        manager.deduct(
            user_id="user_1",
            metrics=UsageMetrics(model="gpt-4", input_tokens=100, output_tokens=0),
        )
        now = _utcnow()  # TZ FIX
        rows = manager.spend_by_model(now - timedelta(seconds=10), now + timedelta(seconds=10))
        assert len(rows) >= 1

    def test_top_users_through_manager(self, manager: CreditManager) -> None:
        manager.add_credits("user_1", 100)
        manager.deduct(
            user_id="user_1",
            metrics=UsageMetrics(model="gpt-4", input_tokens=100, output_tokens=0),
        )
        now = _utcnow()  # TZ FIX
        rows = manager.top_users(5, now - timedelta(seconds=10), now + timedelta(seconds=10))
        assert len(rows) >= 1

    def test_daily_spend_through_manager(self, manager: CreditManager) -> None:
        manager.add_credits("user_1", 100)
        manager.deduct(
            user_id="user_1",
            metrics=UsageMetrics(model="gpt-4", input_tokens=100, output_tokens=0),
        )
        now = _utcnow()  # (already passing, kept tz-aware for consistency)
        rows = manager.daily_spend(now - timedelta(days=1), now + timedelta(days=1))
        assert len(rows) >= 1
        assert rows[0].total_spend == Decimal("1")


class TestCreditExpiry:
    def test_sweep_expired_through_manager(self) -> None:
        store = MemoryStore()
        mgr = CreditManager(store=store)
        mgr.publish_pricing_from_dict(
            {
                "models": {"_default": "input_tokens * 1"},
            }
        )
        # TZ FIX: tz-aware UTC expiry in the past so the sweep (which compares
        # against the store's UTC ``now``) sees it as expired.
        expires_at = _utcnow() - timedelta(seconds=1)
        mgr.add_credits("user_1", 100, "purchase", expires_at=expires_at)

        result = mgr.sweep_expired_credits()
        assert result.expired_count == 1
        assert result.expired_amount == Decimal("100")
        assert mgr.get_balance("user_1").balance == Decimal(0)

    def test_dry_run_through_manager(self) -> None:
        store = MemoryStore()
        mgr = CreditManager(store=store)
        mgr.publish_pricing_from_dict(
            {
                "models": {"_default": "input_tokens * 1"},
            }
        )
        # TZ FIX: tz-aware UTC expiry in the past.
        expires_at = _utcnow() - timedelta(seconds=1)
        mgr.add_credits("user_1", 100, "purchase", expires_at=expires_at)

        result = mgr.sweep_expired_credits(dry_run=True)
        assert result.expired_count == 1
        assert result.dry_run is True
        assert mgr.get_balance("user_1").balance == Decimal("100")


class TestTeamDeduct:
    def test_deduct_team_calculates_cost_and_debits_team(self) -> None:
        store = MemoryStore()
        mgr = CreditManager(store=store)
        mgr.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}})
        team = store.create_team("Team", Decimal(500))
        store.add_team_member(team.team_id, "user-1")

        result = mgr.deduct_team(team.team_id, "user-1", UsageMetrics(input_tokens=100))
        # deduct_team returns the store's negative-amount convention.
        assert result.amount == Decimal("-100")
        assert result.team_balance_after == Decimal("400")
        assert result.transaction_id != ""

    def test_deduct_team_zero_cost_noop(self) -> None:
        store = MemoryStore()
        mgr = CreditManager(store=store)
        mgr.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}})
        team = store.create_team("Team", Decimal(500))
        store.add_team_member(team.team_id, "user-1")

        result = mgr.deduct_team(team.team_id, "user-1", UsageMetrics(input_tokens=0))
        assert result.amount == Decimal(0)
        assert result.team_balance_after == Decimal("500")

    def test_deduct_team_idempotent_replay(self) -> None:
        """Same key → original team tx, no second pool charge (H12)."""
        store = MemoryStore()
        mgr = CreditManager(store=store)
        mgr.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}})
        team = store.create_team("Team", Decimal(500))
        store.add_team_member(team.team_id, "user-1")

        first = mgr.deduct_team(team.team_id, "user-1", UsageMetrics(input_tokens=100), idempotency_key="t1")
        replay = mgr.deduct_team(team.team_id, "user-1", UsageMetrics(input_tokens=100), idempotency_key="t1")
        assert replay.transaction_id == first.transaction_id
        # Pool charged only once.
        assert store.get_team_balance(team.team_id).balance == Decimal("400")

    def test_deduct_team_requires_pricing_loaded(self) -> None:
        store = MemoryStore()
        mgr = CreditManager(store=store)
        with pytest.raises(PricingNotLoadedError):
            mgr.deduct_team("team-1", "user-1", UsageMetrics(input_tokens=100))

    def test_deduct_team_insufficient_balance_raises(self) -> None:
        """deduct_team now raises on error, consistent with deduct (H3)."""
        store = MemoryStore()
        mgr = CreditManager(store=store)
        mgr.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}})
        team = store.create_team("Poor Team", Decimal(10))
        store.add_team_member(team.team_id, "user-1")

        with pytest.raises(InsufficientCreditsError, match="insufficient_team_balance"):
            mgr.deduct_team(team.team_id, "user-1", UsageMetrics(input_tokens=100))

    def test_deduct_team_user_not_in_team_raises(self) -> None:
        store = MemoryStore()
        mgr = CreditManager(store=store)
        mgr.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}})
        team = store.create_team("Closed Team", Decimal(500))
        with pytest.raises(InsufficientCreditsError, match="user_not_in_team"):
            mgr.deduct_team(team.team_id, "user-1", UsageMetrics(input_tokens=10))


class TestSpendCapsManager:
    def test_daily_deny_cap_blocks_deduction(self) -> None:
        store = MemoryStore()
        mgr = CreditManager(store=store)
        mgr.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}, "min_balance": 0})
        store.add_credits("user-1", Decimal(1000))
        store.set_spend_cap(SpendCap(user_id="user-1", type="daily", limit=Decimal(10), action="deny"))

        with pytest.raises(CapReachedError, match="Spend cap exceeded"):
            mgr.deduct("user-1", UsageMetrics(model="gpt-4", input_tokens=11))
        # Denied: no debit.
        assert mgr.get_balance("user-1").balance == Decimal("1000")

    def test_warn_action_allows_deduction(self) -> None:
        store = MemoryStore()
        mgr = CreditManager(store=store)
        mgr.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}, "min_balance": 0})
        store.add_credits("user-1", Decimal(1000))
        store.set_spend_cap(SpendCap(user_id="user-1", type="daily", limit=Decimal(10), action="warn"))

        result = mgr.deduct("user-1", UsageMetrics(model="gpt-4", input_tokens=11))
        assert result.transaction_id != ""
        assert result.cap_warning == "warn"
        assert mgr.get_balance("user-1").balance == Decimal("989")

    def test_notify_action_allows_deduction(self) -> None:
        store = MemoryStore()
        mgr = CreditManager(store=store)
        mgr.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}, "min_balance": 0})
        store.add_credits("user-1", Decimal(1000))
        store.set_spend_cap(SpendCap(user_id="user-1", type="daily", limit=Decimal(10), action="notify"))

        result = mgr.deduct("user-1", UsageMetrics(model="gpt-4", input_tokens=11))
        assert result.transaction_id != ""
        assert result.cap_warning == "notify"

    def test_cap_within_limit_allows_deduction(self) -> None:
        store = MemoryStore()
        mgr = CreditManager(store=store)
        mgr.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}, "min_balance": 0})
        store.add_credits("user-1", Decimal(1000))
        store.set_spend_cap(SpendCap(user_id="user-1", type="daily", limit=Decimal(100), action="deny"))

        result = mgr.deduct("user-1", UsageMetrics(model="gpt-4", input_tokens=5))
        assert result.transaction_id != ""
        assert result.cap_warning is None


class TestEventSystem:
    """Tests for CreditManager event emission."""

    def test_emits_deducted_event(self) -> None:
        store = MemoryStore()
        emitter = CreditEventEmitter()
        mgr = CreditManager(store=store, emitter=emitter)
        mgr.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}, "min_balance": 0})
        mgr.add_credits("user-1", 100)

        events: list[CreditEvent] = []
        emitter.on("credits.deducted", events.append)

        mgr.deduct("user-1", UsageMetrics(input_tokens=10))
        assert len(events) == 1
        assert events[0].type == "credits.deducted"
        assert events[0].user_id == "user-1"
        assert events[0].data is not None
        assert events[0].data["amount"] == Decimal("10")

    def test_emits_added_event(self) -> None:
        emitter = CreditEventEmitter()
        mgr = CreditManager(store=MemoryStore(), emitter=emitter)
        mgr.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}})

        events: list[CreditEvent] = []
        emitter.on("credits.added", events.append)

        mgr.add_credits("user-1", 50)
        assert len(events) == 1
        assert events[0].data is not None
        assert events[0].data["amount"] == Decimal("50")

    def test_emits_refunded_event(self) -> None:
        store = MemoryStore()
        emitter = CreditEventEmitter()
        mgr = CreditManager(store=store, emitter=emitter)
        mgr.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}, "min_balance": 0})
        mgr.add_credits("user-1", 100)

        events: list[CreditEvent] = []
        emitter.on("credits.refunded", events.append)

        deduct = mgr.deduct("user-1", UsageMetrics(input_tokens=10))
        mgr.refund_credits(deduct.transaction_id)
        assert len(events) == 1

    def test_emits_cap_reached_on_deny(self) -> None:
        store = MemoryStore()
        emitter = CreditEventEmitter()
        mgr = CreditManager(store=store, emitter=emitter)
        mgr.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}, "min_balance": 0})
        mgr.add_credits("user-1", 100)
        store.set_spend_cap(SpendCap(user_id="user-1", type="daily", limit=Decimal(5), action="deny"))

        cap_events: list[CreditEvent] = []
        fail_events: list[CreditEvent] = []
        emitter.on("credits.cap_reached", cap_events.append)
        emitter.on("credits.deduct_failed", fail_events.append)

        with pytest.raises(CapReachedError):
            mgr.deduct("user-1", UsageMetrics(input_tokens=10))
        assert len(cap_events) == 1
        assert cap_events[0].type == "credits.cap_reached"
        assert len(fail_events) == 1
        assert fail_events[0].data is not None
        assert fail_events[0].data["error"] == "cap_reached"

    def test_emits_expired_event_on_sweep(self) -> None:
        store = MemoryStore()
        emitter = CreditEventEmitter()
        mgr = CreditManager(store=store, emitter=emitter)
        mgr.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}})

        events: list[CreditEvent] = []
        emitter.on("credits.expired", events.append)

        # TZ FIX: tz-aware UTC expiry in the past.
        mgr.add_credits("user-1", 100, expires_at=_utcnow() - timedelta(seconds=1))
        mgr.sweep_expired_credits()
        assert len(events) == 1
        assert events[0].data is not None
        assert events[0].data["expired_count"] == 1

    def test_multiple_handlers_all_fire(self) -> None:
        store = MemoryStore()
        emitter = CreditEventEmitter()
        mgr = CreditManager(store=store, emitter=emitter)
        mgr.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}, "min_balance": 0})
        mgr.add_credits("user-1", 100)

        called: list[int] = []
        emitter.on("credits.deducted", lambda _: called.append(1))
        emitter.on("credits.deducted", lambda _: called.append(2))

        mgr.deduct("user-1", UsageMetrics(input_tokens=10))
        assert len(called) == 2

    def test_throwing_handler_does_not_break_flow(self) -> None:
        """A handler that raises must not break the main flow or sibling handlers."""
        store = MemoryStore()
        emitter = CreditEventEmitter()
        mgr = CreditManager(store=store, emitter=emitter)
        mgr.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}, "min_balance": 0})
        mgr.add_credits("user-1", 100)

        survivor: list[int] = []

        def boom(_: CreditEvent) -> None:
            raise RuntimeError("handler exploded")

        emitter.on("credits.deducted", boom)
        emitter.on("credits.deducted", lambda _: survivor.append(1))

        # The deduct still succeeds despite the throwing handler.
        result = mgr.deduct("user-1", UsageMetrics(input_tokens=10))
        assert result.amount == Decimal("10")
        assert survivor == [1]  # sibling handler still ran

    def test_unregistered_event_noop(self) -> None:
        emitter = CreditEventEmitter()
        # Should not raise
        emitter.emit(CreditEvent(type="credits.deducted", timestamp=_utcnow(), user_id="u1"))

    def test_off_removes_handler(self) -> None:
        emitter = CreditEventEmitter()
        store = MemoryStore()
        mgr = CreditManager(store=store, emitter=emitter)
        mgr.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}, "min_balance": 0})
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
        mgr.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}, "min_balance": 0})
        mgr.add_credits("user-1", 100)
        store.set_spend_cap(SpendCap(user_id="user-1", type="daily", limit=Decimal(5), action="warn"))

        events: list[CreditEvent] = []
        emitter.on("credits.cap_warning", events.append)

        mgr.deduct("user-1", UsageMetrics(input_tokens=10))
        assert len(events) == 1
        assert events[0].type == "credits.cap_warning"
        assert events[0].data is not None
        assert events[0].data["action"] == "warn"

    def test_event_timestamps_are_tz_aware_utc(self) -> None:
        """Event timestamps are tz-aware UTC (M9)."""
        store = MemoryStore()
        emitter = CreditEventEmitter()
        mgr = CreditManager(store=store, emitter=emitter)
        mgr.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}})

        events: list[CreditEvent] = []
        emitter.on("credits.added", events.append)
        mgr.add_credits("user-1", 50)

        assert events[0].timestamp.tzinfo is not None
        assert events[0].timestamp.utcoffset() == timedelta(0)


class TestLowBalanceEvent:
    """Edge-triggered, configurable low_balance threshold (M18/§6)."""

    def test_low_balance_default_threshold_edge_trigger(self) -> None:
        """Fires once on crossing min_balance*2 (default), not on every call."""
        store = MemoryStore()
        emitter = CreditEventEmitter()
        # min_balance 5 → default threshold 10. min_balance 0 here so the floor
        # never blocks; set min_balance explicitly to control the threshold.
        mgr = CreditManager(store=store, emitter=emitter)
        mgr.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}, "min_balance": 5})
        mgr.add_credits("user-1", 100)

        events: list[CreditEvent] = []
        emitter.on("credits.low_balance", events.append)

        # threshold = 5 * 2 = 10.
        # 100 → 95: above threshold, no event.
        mgr.deduct("user-1", UsageMetrics(input_tokens=5), idempotency_key="a")
        assert events == []

        # 95 → 8: crosses 10, fires once.
        mgr.deduct("user-1", UsageMetrics(input_tokens=87), idempotency_key="b")
        assert len(events) == 1
        assert events[0].data is not None
        assert events[0].data["balance"] == Decimal("8")
        assert events[0].data["threshold"] == Decimal("10")

        # 8 → 7: already below threshold, does NOT fire again (edge-triggered).
        mgr.deduct("user-1", UsageMetrics(input_tokens=1), idempotency_key="c")
        assert len(events) == 1

    def test_low_balance_custom_threshold(self) -> None:
        store = MemoryStore()
        emitter = CreditEventEmitter()
        mgr = CreditManager(store=store, emitter=emitter, low_balance_threshold=Decimal(50))
        mgr.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}, "min_balance": 0})
        mgr.add_credits("user-1", 100)

        events: list[CreditEvent] = []
        emitter.on("credits.low_balance", events.append)

        # 100 → 60: above 50, no event.
        mgr.deduct("user-1", UsageMetrics(input_tokens=40), idempotency_key="a")
        assert events == []
        # 60 → 50: crosses threshold (>= boundary), fires.
        mgr.deduct("user-1", UsageMetrics(input_tokens=10), idempotency_key="b")
        assert len(events) == 1
        assert events[0].data is not None
        assert events[0].data["balance"] == Decimal("50")
        assert events[0].data["threshold"] == Decimal("50")


class TestPlanFeatures:
    """Tests for plan management and feature gating."""

    def test_get_user_plan_through_manager(self, manager: CreditManager) -> None:
        store = manager._store
        store.set_user_plan("user-1", "pro")
        result = manager.get_user_plan("user-1")
        assert result.plan_id == "pro"

    def test_check_feature_through_manager(self, manager: CreditManager) -> None:
        store = manager._store
        v2 = PricingConfigData(
            models={"_default": "1"},
            plans={
                "premium": PlanDefinition(
                    id="premium",
                    name="Premium",
                    features={"ai_chat": True, "max_roadmaps": 20},
                ),
            },
        )
        store.set_active_pricing(v2)
        store.set_user_plan("user-1", "premium")
        result = manager.check_feature("user-1", "ai_chat")
        assert result.has_feature is True
        assert result.value is True
        result = manager.check_feature("user-1", "export_pdf")
        assert result.has_feature is False
        result = manager.check_feature("nobody", "ai_chat")
        assert result.has_feature is False

    def test_check_feature_numeric_zero_is_present(self) -> None:
        """Numeric 0 / "" entitlements are PRESENT (M6 presence-vs-truthiness)."""
        store = MemoryStore()
        mgr = CreditManager(store=store)
        v2 = PricingConfigData(
            models={"_default": "1"},
            plans={
                "tier": PlanDefinition(
                    id="tier",
                    name="Tier",
                    features={"quota": 0, "label": "", "disabled": False, "missing": None},
                ),
            },
        )
        store.set_active_pricing(v2)
        store.set_user_plan("user-1", "tier")

        assert mgr.check_feature("user-1", "quota").has_feature is True
        assert mgr.check_feature("user-1", "label").has_feature is True
        assert mgr.check_feature("user-1", "disabled").has_feature is False
        assert mgr.check_feature("user-1", "missing").has_feature is False


class TestMetadataMerge:
    """System fields win over caller metadata (M7/§5)."""

    def test_system_fields_win_over_caller(self, store: MemoryStore) -> None:
        from ducto.interface.models import CreditMetadata

        mgr = CreditManager(store=store)
        mgr.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}, "min_balance": 0})
        mgr.add_credits("user-1", 100)

        # Caller tries to overwrite reserved system keys.
        caller_meta = CreditMetadata(
            idempotency_key="caller_key",
            model="caller-model",
            reference_id="ref-99",
        )
        result = mgr.deduct(
            "user-1",
            UsageMetrics(model="real-model", input_tokens=10),
            idempotency_key="system_key",
            metadata=caller_meta,
        )

        # Find the persisted tx and inspect its metadata.
        txs = store.list_user_transactions("user-1", types=["usage"])
        assert len(txs) == 1
        meta = txs[0].metadata or {}
        # System values win.
        assert meta["idempotency_key"] == "system_key"
        assert meta["model"] == "real-model"
        # Caller's non-reserved key survives.
        assert meta["reference_id"] == "ref-99"
        # Replay with the SYSTEM key is honored (proves system key was stored).
        replay = mgr.deduct(
            "user-1",
            UsageMetrics(model="real-model", input_tokens=10),
            idempotency_key="system_key",
        )
        assert replay.idempotent
        assert replay.transaction_id == result.transaction_id
