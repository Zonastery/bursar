"""Tests for the lease lifecycle (interface plan §3/§4) on MemoryStore + manager.

Covers the plan's acceptance criteria: atomic lease admission & double-submit,
strict zero-debt under concurrency, the agentic feature gate, overdraft
full-billing (D5), TTL / renewal, release idempotency, multi-level low_balance,
and presets / planless defaults.

Money is exact ``Decimal`` everywhere (contract §1).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from bursar import (
    ConcurrencyLimitError,
    CreditManager,
    FeatureNotEntitledError,
    InsufficientCreditsError,
    LeaseExpiredError,
    LeaseNotFoundError,
    LowBalanceConfig,
    UsageMetrics,
)
from bursar.events import CreditEvent, CreditEventEmitter
from bursar.interface.memory import MemoryStore
from bursar.interface.models import OperationPolicy, PlanDefinition, PricingConfigData


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore()


def _strict_manager(store: MemoryStore, min_balance: Decimal = Decimal(5), **kwargs) -> CreditManager:
    m = CreditManager(store=store, policy="strict_prepaid", **kwargs)
    m.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}, "min_balance": min_balance})
    return m


# ── 1. Lease admission / double-submit (maxConcurrent) ─────────────────────


class TestAdmissionConcurrency:
    def test_double_submit_blocked_with_max_concurrent_one(self, store: MemoryStore) -> None:
        m = _strict_manager(store, min_balance=Decimal(0), max_concurrent=1)
        store.add_credits("u1", Decimal(100))

        lease = m.reserve("u1", Decimal(10), operation_type="chat")
        assert lease.lease_id
        # A second concurrent op of the same type is rejected — not a balance leak.
        with pytest.raises(ConcurrencyLimitError):
            m.reserve("u1", Decimal(10), operation_type="chat")

        avail = m.get_available("u1")
        assert avail.reserved == Decimal(10)  # only the one live hold
        assert avail.available == Decimal(90)

    def test_releasing_frees_a_concurrency_slot(self, store: MemoryStore) -> None:
        m = _strict_manager(store, min_balance=Decimal(0), max_concurrent=1)
        store.add_credits("u1", Decimal(100))

        lease = m.reserve("u1", Decimal(10), operation_type="chat")
        m.release("u1", lease.lease_id)
        # Slot is free again.
        lease2 = m.reserve("u1", Decimal(10), operation_type="chat")
        assert lease2.lease_id

    def test_max_concurrent_is_per_operation_type(self, store: MemoryStore) -> None:
        m = _strict_manager(store, min_balance=Decimal(0), max_concurrent=1)
        store.add_credits("u1", Decimal(100))
        m.reserve("u1", Decimal(10), operation_type="chat")
        # A different op type has its own slot.
        other = m.reserve("u1", Decimal(10), operation_type="batch")
        assert other.lease_id


# ── 2. Strict zero-debt under concurrency ──────────────────────────────────


class TestStrictZeroDebt:
    def test_worst_case_leases_never_breach_floor(self, store: MemoryStore) -> None:
        m = _strict_manager(store, min_balance=Decimal(5))
        store.add_credits("u1", Decimal(100))  # floor 5 ⇒ 95 usable

        l1 = m.reserve("u1", Decimal(40))
        l2 = m.reserve("u1", Decimal(40))
        # Third worst-case lease would push available below the floor → rejected.
        with pytest.raises(InsufficientCreditsError):
            m.reserve("u1", Decimal(40))

        # Each settle charges the ACTUAL (≤ worst-case lease); balance stays ≥ floor.
        m.settle("u1", l1.lease_id, Decimal(30))
        m.settle("u1", l2.lease_id, Decimal(15))
        bal = m.get_balance("u1")
        assert bal.balance == Decimal(55)
        assert bal.balance >= Decimal(5)

    def test_available_accounts_for_active_holds(self, store: MemoryStore) -> None:
        m = _strict_manager(store, min_balance=Decimal(0))
        store.add_credits("u1", Decimal(100))
        m.reserve("u1", Decimal(30))
        avail = m.get_available("u1")
        assert avail.balance == Decimal(100)
        assert avail.reserved == Decimal(30)
        assert avail.available == Decimal(70)


# ── 3. Agentic feature gate ────────────────────────────────────────────────


class TestFeatureGate:
    def _manager_with_plans(self, store: MemoryStore) -> CreditManager:
        m = CreditManager(store=store, policy="strict_prepaid")
        m.publish_pricing(
            PricingConfigData(
                models={"_default": "input_tokens * 1"},
                min_balance=Decimal(0),
                plans={
                    "free": PlanDefinition(id="free", name="Free", features={"chat": True}),
                    "pro": PlanDefinition(id="pro", name="Pro", features={"chat": True, "agentic": True}),
                },
            )
        )
        return m

    def test_free_user_blocked_from_agentic(self, store: MemoryStore) -> None:
        m = self._manager_with_plans(store)
        store.add_credits("u1", Decimal(100))
        store.set_user_plan("u1", "free")
        with pytest.raises(FeatureNotEntitledError):
            m.reserve("u1", Decimal(10), required_feature="agentic")

    def test_pro_user_allowed_agentic(self, store: MemoryStore) -> None:
        m = self._manager_with_plans(store)
        store.add_credits("u1", Decimal(100))
        store.set_user_plan("u1", "pro")
        lease = m.reserve("u1", Decimal(10), required_feature="agentic")
        assert lease.lease_id

    def test_can_afford_reports_feature_gate(self, store: MemoryStore) -> None:
        m = self._manager_with_plans(store)
        store.add_credits("u1", Decimal(100))
        store.set_user_plan("u1", "free")
        res = m.can_afford("u1", Decimal(10), required_feature="agentic")
        assert res.affordable is False
        assert res.reason == "feature_not_entitled"


# ── 4. Overdraft full-billing (D5) ─────────────────────────────────────────


class TestOverdraft:
    def test_settle_clamps_to_overdraft_floor(self, store: MemoryStore) -> None:
        # planless user → constructor preset (overdraft, floor -50).
        m = CreditManager(store=store, policy="overdraft", overdraft_floor=Decimal(-50))
        m.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}, "min_balance": 0})
        store.add_credits("u1", Decimal(0))  # ensure a balance row at 0

        lease = m.reserve("u1", Decimal(10))  # small estimate
        # C1 fix: settle clamps the charge so balance stays ≥ overdraft_floor (-50).
        # actual=60 would take balance to -60; floor is -50, so max debit is 50.
        ded = m.settle("u1", lease.lease_id, Decimal(60))
        assert ded.balance_after == Decimal(-50)

        # A NEW admission is now rejected (available ≤ floor).
        with pytest.raises(InsufficientCreditsError):
            m.reserve("u1", Decimal(1))

        # add_credits reconciles the negative balance.
        res = m.add_credits("u1", Decimal(200))
        assert res.new_balance == Decimal(150)

    def test_overdraft_event_emitted_when_balance_negative(self, store: MemoryStore) -> None:
        emitter = CreditEventEmitter()
        events: list[CreditEvent] = []
        emitter.on("credits.overdraft", events.append)
        m = CreditManager(store=store, emitter=emitter, policy="overdraft", overdraft_floor=Decimal(-50))
        m.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}, "min_balance": 0})
        store.add_credits("u1", Decimal(0))

        lease = m.reserve("u1", Decimal(10))
        m.settle("u1", lease.lease_id, Decimal(30))
        assert len(events) == 1
        assert events[0].data["balance"] == Decimal(-30)

    def test_settle_within_overdraft_floor_is_unclamped(self, store: MemoryStore) -> None:
        # Actual cost ≤ balance - floor: charge goes through unchanged.
        m = CreditManager(store=store, policy="overdraft", overdraft_floor=Decimal(-50))
        m.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}, "min_balance": 0})
        store.add_credits("u1", Decimal(0))

        lease = m.reserve("u1", Decimal(10))
        ded = m.settle("u1", lease.lease_id, Decimal(40))
        assert ded.balance_after == Decimal(-40)


class TestStrictPrepaidFloor:
    def test_settle_clamps_to_min_balance(self, store: MemoryStore) -> None:
        # C1: strict_prepaid zero-debt guarantee enforced at settle time.
        # balance=20, min_balance=5, actual=80 → max debit = 15, balance_after = 5.
        m = CreditManager(store=store, policy="strict_prepaid")
        m.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}, "min_balance": 5})
        store.add_credits("u1", Decimal(20))

        lease = m.reserve("u1", Decimal(10))
        ded = m.settle("u1", lease.lease_id, Decimal(80))
        assert ded.balance_after >= Decimal(5)

    def test_settle_exact_min_balance(self, store: MemoryStore) -> None:
        # Actual cost exactly depletes to the floor: no over-clamp.
        m = CreditManager(store=store, policy="strict_prepaid")
        m.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}, "min_balance": 5})
        store.add_credits("u1", Decimal(20))

        lease = m.reserve("u1", Decimal(10))
        ded = m.settle("u1", lease.lease_id, Decimal(15))
        assert ded.balance_after == Decimal(5)


# ── 5. TTL / renewal ───────────────────────────────────────────────────────


class TestTtlRenewal:
    def test_settle_on_expired_lease_raises(self, store: MemoryStore) -> None:
        m = _strict_manager(store, min_balance=Decimal(0))
        store.add_credits("u1", Decimal(100))
        lease = m.reserve("u1", Decimal(20))
        # Force expiry (white-box) rather than sleeping.
        store._reservations[lease.lease_id].expires_at = datetime.now(UTC) - timedelta(seconds=1)
        with pytest.raises(LeaseExpiredError):
            m.settle("u1", lease.lease_id, Decimal(20))
        # The expired hold no longer counts against available.
        assert m.get_available("u1").available == Decimal(100)

    def test_renew_extends_ttl_and_allows_settle(self, store: MemoryStore) -> None:
        m = _strict_manager(store, min_balance=Decimal(0))
        store.add_credits("u1", Decimal(100))
        lease = m.reserve("u1", Decimal(20), ttl=1)
        # Almost-expired → renew pushes it out, then settle succeeds.
        store._reservations[lease.lease_id].expires_at = datetime.now(UTC) + timedelta(milliseconds=1)
        renewed = m.renew("u1", lease.lease_id, ttl=600)
        assert renewed.error is None
        ded = m.settle("u1", lease.lease_id, Decimal(20))
        assert ded.balance_after == Decimal(80)

    def test_renew_expired_lease_raises(self, store: MemoryStore) -> None:
        m = _strict_manager(store, min_balance=Decimal(0))
        store.add_credits("u1", Decimal(100))
        lease = m.reserve("u1", Decimal(20))
        store._reservations[lease.lease_id].expires_at = datetime.now(UTC) - timedelta(seconds=1)
        with pytest.raises(LeaseExpiredError):
            m.renew("u1", lease.lease_id, ttl=600)


# ── 6. release idempotency (H1) ────────────────────────────────────────────


class TestReleaseIdempotency:
    def test_double_release_is_typed_not_error(self, store: MemoryStore) -> None:
        m = _strict_manager(store, min_balance=Decimal(0))
        store.add_credits("u1", Decimal(100))
        lease = m.reserve("u1", Decimal(20))

        r1 = m.release("u1", lease.lease_id)
        assert r1.released is True and r1.reason == "released"
        r2 = m.release("u1", lease.lease_id)
        assert r2.released is False and r2.reason == "already_released"

    def test_settle_after_release_returns_lease_not_found(self, store: MemoryStore) -> None:
        m = _strict_manager(store, min_balance=Decimal(0))
        store.add_credits("u1", Decimal(100))
        lease = m.reserve("u1", Decimal(20))
        m.release("u1", lease.lease_id)
        with pytest.raises(LeaseNotFoundError):
            m.settle("u1", lease.lease_id, Decimal(20))

    def test_settle_after_settle_replays(self, store: MemoryStore) -> None:
        m = _strict_manager(store, min_balance=Decimal(0))
        store.add_credits("u1", Decimal(100))
        lease = m.reserve("u1", Decimal(20))
        first = m.settle("u1", lease.lease_id, Decimal(20))
        second = m.settle("u1", lease.lease_id, Decimal(20))
        assert second.idempotent is True
        assert second.amount == first.amount
        # Balance only moved once.
        assert m.get_balance("u1").balance == Decimal(80)

    def test_release_after_settle_reports_already_settled(self, store: MemoryStore) -> None:
        m = _strict_manager(store, min_balance=Decimal(0))
        store.add_credits("u1", Decimal(100))
        lease = m.reserve("u1", Decimal(20))
        m.settle("u1", lease.lease_id, Decimal(20))
        r = m.release("u1", lease.lease_id)
        assert r.released is False and r.reason == "already_settled"

    def test_release_unknown_lease(self, store: MemoryStore) -> None:
        m = _strict_manager(store, min_balance=Decimal(0))
        r = m.release("u1", "no-such-lease")
        assert r.released is False and r.reason == "not_found"


# ── 7. Multi-level low_balance (H4) ────────────────────────────────────────


class TestMultiLevelLowBalance:
    def _events(self, m: CreditManager, emitter: CreditEventEmitter) -> list[Decimal]:
        fired: list[Decimal] = []
        emitter.on("credits.low_balance", lambda e: fired.append(e.data["threshold"]))
        return fired

    def test_each_level_fires_once_per_descent_and_rearms(self, store: MemoryStore) -> None:
        emitter = CreditEventEmitter()
        m = CreditManager(
            store=store,
            emitter=emitter,
            policy="overdraft",
            overdraft_floor=Decimal(0),
            low_balance=LowBalanceConfig(thresholds=[Decimal(50), Decimal(20), Decimal(10)]),
        )
        m.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}, "min_balance": 0})
        fired = self._events(m, emitter)
        store.add_credits("u1", Decimal(100))

        def charge(amount: Decimal) -> None:
            lease = m.reserve("u1", amount)
            m.settle("u1", lease.lease_id, amount)

        charge(Decimal(55))  # 100 → 45 : crosses 50
        charge(Decimal(30))  # 45 → 15 : crosses 20
        charge(Decimal(7))  # 15 → 8  : crosses 10
        assert fired == [Decimal(50), Decimal(20), Decimal(10)]

        # Top-up re-arms; a single big charge crossing all levels fires once for
        # the lowest crossed.
        m.add_credits("u1", Decimal(92))  # 8 → 100
        charge(Decimal(95))  # 100 → 5 : crosses 50,20,10 → fire once @ 10
        assert fired == [Decimal(50), Decimal(20), Decimal(10), Decimal(10)]

    def test_on_low_balance_handler_failure_never_blocks(self, store: MemoryStore) -> None:
        def boom(_event: CreditEvent) -> None:
            raise RuntimeError("handler down")

        m = CreditManager(
            store=store,
            policy="overdraft",
            overdraft_floor=Decimal(0),
            low_balance=LowBalanceConfig(thresholds=[Decimal(20)], on_trigger=boom),
        )
        m.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}, "min_balance": 0})
        store.add_credits("u1", Decimal(100))
        lease = m.reserve("u1", Decimal(85))
        # The handler raises, but settle still completes normally.
        ded = m.settle("u1", lease.lease_id, Decimal(85))
        assert ded.balance_after == Decimal(15)


# ── 8. Presets / planless default (M1) ─────────────────────────────────────


class TestPresetsAndPlanless:
    def test_strict_prepaid_never_goes_negative(self, store: MemoryStore) -> None:
        m = _strict_manager(store, min_balance=Decimal(0))
        store.add_credits("u1", Decimal(50))
        # Cannot reserve beyond the balance under strict.
        with pytest.raises(InsufficientCreditsError):
            m.reserve("u1", Decimal(60))
        assert m.get_balance("u1").balance == Decimal(50)

    def test_planless_user_gets_constructor_default_not_unlimited(self, store: MemoryStore) -> None:
        # Overdraft preset with a bounded floor applies to a user with no plan.
        m = CreditManager(store=store, policy="overdraft", overdraft_floor=Decimal(-20))
        m.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}, "min_balance": 0})
        store.add_credits("u1", Decimal(0))
        m.reserve("u1", Decimal(20))  # down to the floor, ok
        with pytest.raises(InsufficientCreditsError):
            m.reserve("u1", Decimal(1))  # beyond the floor → bounded, not unlimited

    def test_plan_per_operation_overrides_preset(self, store: MemoryStore) -> None:
        # strict_prepaid preset, but the plan opts one op type into overdraft.
        m = CreditManager(store=store, policy="strict_prepaid")
        m.publish_pricing(
            PricingConfigData(
                models={"_default": "input_tokens * 1"},
                min_balance=Decimal(0),
                plans={
                    "pro": PlanDefinition(
                        id="pro",
                        name="Pro",
                        default_billing_mode="strict",
                        per_operation={
                            "agent": OperationPolicy(billing_mode="overdraft", overdraft_floor=Decimal(-30))
                        },
                    )
                },
            )
        )
        store.add_credits("u1", Decimal(0))
        store.set_user_plan("u1", "pro")

        # Default op stays strict (no debt allowed).
        with pytest.raises(InsufficientCreditsError):
            m.reserve("u1", Decimal(5), operation_type="chat")
        # The 'agent' op inherits the plan's overdraft policy → can go to -30.
        lease = m.reserve("u1", Decimal(25), operation_type="agent")
        assert lease.lease_id
        assert lease.billing_mode == "overdraft"


# ── run_billed shortcut (§4) ───────────────────────────────────────────────


class TestRunBilled:
    def test_run_billed_reserves_then_settles(self, store: MemoryStore) -> None:
        m = _strict_manager(store, min_balance=Decimal(0))
        store.add_credits("u1", Decimal(100))

        def do_work() -> tuple[str, Decimal]:
            return "answer", Decimal(30)

        out = m.run_billed("u1", estimate=Decimal(50), do_work=do_work)
        assert out["result"] == "answer"
        assert out["deduction"].balance_after == Decimal(70)

    def test_run_billed_releases_on_failure(self, store: MemoryStore) -> None:
        m = _strict_manager(store, min_balance=Decimal(0))
        store.add_credits("u1", Decimal(100))

        def do_work() -> tuple[str, Decimal]:
            raise RuntimeError("work failed")

        with pytest.raises(RuntimeError, match="work failed"):
            m.run_billed("u1", estimate=Decimal(50), do_work=do_work)
        # Lease was released — nothing held, nothing charged.
        avail = m.get_available("u1")
        assert avail.reserved == Decimal(0)
        assert avail.available == Decimal(100)


# ── zero-cost settle (M3) & metrics-based sizing ───────────────────────────


class TestMisc:
    def test_zero_cost_settle_releases_without_charge(self, store: MemoryStore) -> None:
        m = _strict_manager(store, min_balance=Decimal(0))
        store.add_credits("u1", Decimal(100))
        lease = m.reserve("u1", Decimal(20))
        ded = m.settle("u1", lease.lease_id, Decimal(0))
        assert ded.amount == Decimal(0)
        assert m.get_balance("u1").balance == Decimal(100)
        # Lease is finalized (settled), not still holding.
        assert m.get_available("u1").reserved == Decimal(0)

    def test_reserve_and_settle_with_metrics(self, store: MemoryStore) -> None:
        m = CreditManager(store=store, policy="strict_prepaid")
        m.publish_pricing_from_dict(
            {"models": {"gpt-4": "input_tokens * 0.01 + output_tokens * 0.03"}, "min_balance": 0}
        )
        store.add_credits("u1", Decimal(100))
        worst = UsageMetrics(model="gpt-4", input_tokens=1000, output_tokens=1000)  # cost 40
        lease = m.reserve("u1", worst)
        assert lease.amount == Decimal("40.00")
        actual = UsageMetrics(model="gpt-4", input_tokens=500, output_tokens=200)  # cost 11
        ded = m.settle("u1", lease.lease_id, actual)
        assert ded.balance_after == Decimal("89.00")

    def test_reserve_with_explicit_model_kwarg(self, store: MemoryStore) -> None:
        """reserve(user, Decimal, model='gpt-4') must capture model even without UsageMetrics (Fix 5)."""
        m = CreditManager(store=store, policy="strict_prepaid")
        m.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}, "min_balance": 0})
        store.add_credits("u1", Decimal(100))

        # Raw Decimal amount + explicit model= kwarg.
        lease = m.reserve("u1", Decimal(30), model="gpt-4")
        assert lease.amount == Decimal(30)
        # Settle returns valid result — model info was threaded through, no errors.
        ded = m.settle("u1", lease.lease_id, Decimal(10))
        assert ded.amount == Decimal(10)
        assert ded.balance_after == Decimal(90)


# ── Fix 1: allowance-aware admission ────────────────────────────────────────


class TestAllowanceAwareAdmission:
    """create_lease must count remaining free allowance toward available headroom
    so a free-tier user can hold a worst-case amount even when cash balance is
    below the hold amount (Fix 1)."""

    def _manager_with_allowance(self, store: MemoryStore, allowance: Decimal) -> CreditManager:
        m = CreditManager(store=store, policy="strict_prepaid")
        m.publish_pricing(
            PricingConfigData(
                models={"_default": "input_tokens * 1"},
                min_balance=Decimal(0),
                plans={"free": PlanDefinition(id="free", name="Free", free_allowance=allowance)},
            )
        )
        return m

    def test_admission_succeeds_when_allowance_covers_hold(self, store: MemoryStore) -> None:
        """Balance 20, allowance 100 → can hold 80 (20 + 100 > 80)."""
        m = self._manager_with_allowance(store, Decimal(100))
        store.add_credits("u1", Decimal(20))
        store.set_user_plan("u1", "free")

        lease = m.reserve("u1", Decimal(80))
        assert lease.amount == Decimal(80)

    def test_admission_fails_when_allowance_plus_balance_still_insufficient(self, store: MemoryStore) -> None:
        """Balance 10, allowance 20 → cannot hold 50 (10 + 20 = 30 < 50)."""
        m = self._manager_with_allowance(store, Decimal(20))
        store.add_credits("u1", Decimal(10))
        store.set_user_plan("u1", "free")

        with pytest.raises(InsufficientCreditsError):
            m.reserve("u1", Decimal(50))

    def test_admission_without_plan_uses_balance_only(self, store: MemoryStore) -> None:
        """Planless user: admission falls back to raw balance, no phantom allowance added."""
        m = _strict_manager(store, min_balance=Decimal(0))
        store.add_credits("u1", Decimal(20))

        # 20 available, asking for 30 — must fail (no plan, no allowance).
        with pytest.raises(InsufficientCreditsError):
            m.reserve("u1", Decimal(30))


# ── Fix 1 / Fix 6: can_afford includes allowance in the available figure ────


class TestCanAffordWithAllowance:
    """can_afford() must reflect the allowance headroom so UI advisories match
    what reserve() will actually admit (Fix 1 / Fix 6)."""

    def _manager_with_allowance(self, store: MemoryStore, allowance: Decimal) -> CreditManager:
        m = CreditManager(store=store, policy="strict_prepaid")
        m.publish_pricing(
            PricingConfigData(
                models={"_default": "input_tokens * 1"},
                min_balance=Decimal(0),
                plans={"free": PlanDefinition(id="free", name="Free", free_allowance=allowance)},
            )
        )
        return m

    def test_can_afford_includes_allowance_in_available(self, store: MemoryStore) -> None:
        m = self._manager_with_allowance(store, Decimal(100))
        store.add_credits("u1", Decimal(20))
        store.set_user_plan("u1", "free")

        # Effective available = 20 (balance) + 100 (allowance) = 120, hold 80 → affordable.
        result = m.can_afford("u1", Decimal(80))
        assert result.affordable is True

    def test_can_afford_returns_false_when_truly_insufficient(self, store: MemoryStore) -> None:
        m = self._manager_with_allowance(store, Decimal(20))
        store.add_credits("u1", Decimal(10))
        store.set_user_plan("u1", "free")

        # Effective available = 30, hold 50 → not affordable.
        result = m.can_afford("u1", Decimal(50))
        assert result.affordable is False
        assert result.reason == "insufficient_credits"
