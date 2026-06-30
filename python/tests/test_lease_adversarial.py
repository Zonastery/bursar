"""Adversarial / financial-safety tests for the lease lifecycle (MemoryStore).

This module attacks the money invariants from every angle a billing system must
survive: concurrency races (no over-admission, no double-charge), exact Decimal
precision (no binary-float drift), invalid inputs, floor-boundary exactness, the
full lease state machine, user-scoped idempotency-key collisions, allowance
consumption at settle, and advisory spend caps at settle.

The central invariant under test (interface plan, Guarantees §1/§2):
    in strict mode, ``balance`` never drops below the floor, and the sum of
    active holds never exceeds ``balance − floor`` — even under arbitrary
    concurrency — because admission is a single atomic lease.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ducto import (
    CapReachedError,
    ConcurrencyLimitError,
    CreditManager,
    InsufficientCreditsError,
)
from ducto.events import CreditEvent, CreditEventEmitter
from ducto.interface.memory import MemoryStore
from ducto.interface.models import OperationPolicy, PlanDefinition, PricingConfigData, SpendCap


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore()


def _manager(store: MemoryStore, min_balance: Decimal = Decimal(0), **kwargs) -> CreditManager:
    m = CreditManager(store=store, **kwargs)
    m.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}, "min_balance": min_balance})
    return m


# ── Concurrency: atomic admission never over-admits ────────────────────────


class TestConcurrencyAdmission:
    def test_no_over_admission_under_thread_storm(self, store: MemoryStore) -> None:
        store.add_credits("u1", Decimal(100))  # floor 0 ⇒ at most 3 holds of 30

        def attempt() -> str | None:
            return store.create_lease("u1", Decimal(30), "usage", floor=Decimal(0)).error

        with ThreadPoolExecutor(max_workers=16) as ex:
            errors = list(ex.map(lambda _: attempt(), range(40)))

        successes = sum(1 for e in errors if e is None)
        assert successes == 3  # 3*30 = 90 ≤ 100; a 4th (120) breaches floor 0
        avail = store.get_available("u1")
        assert avail.reserved == Decimal(90)
        assert avail.available == Decimal(10)
        assert avail.balance == Decimal(100)  # nothing charged yet — only held

    def test_max_concurrent_holds_under_threads(self, store: MemoryStore) -> None:
        store.add_credits("u1", Decimal(10_000))  # plenty of balance; cap is on COUNT

        def attempt() -> str | None:
            return store.create_lease("u1", Decimal(1), "chat", floor=Decimal(0), max_concurrent=5).error

        with ThreadPoolExecutor(max_workers=16) as ex:
            errors = list(ex.map(lambda _: attempt(), range(40)))

        assert sum(1 for e in errors if e is None) == 5
        assert all(e in (None, "concurrency_limit") for e in errors)

    def test_concurrent_settle_same_key_charges_once(self, store: MemoryStore) -> None:
        store.add_credits("u1", Decimal(100))
        lease = store.create_lease("u1", Decimal(50), "usage", floor=Decimal(0))

        def attempt() -> None:
            store.settle_lease("u1", lease.lease_id, Decimal(50), idempotency_key="k")

        with ThreadPoolExecutor(max_workers=12) as ex:
            list(ex.map(lambda _: attempt(), range(12)))

        # Exactly one debit of 50 — no double-charge under the race.
        assert store.get_balance("u1").balance == Decimal(50)

    def test_pipeline_invariant_balance_never_below_floor(self, store: MemoryStore) -> None:
        store.add_credits("u1", Decimal(1000))

        def run_one() -> None:
            lease = store.create_lease("u1", Decimal(20), "usage", floor=Decimal(0))
            assert lease.error is None  # 50*20 == 1000 exactly ⇒ all admit
            store.settle_lease("u1", lease.lease_id, Decimal(7))

        with ThreadPoolExecutor(max_workers=16) as ex:
            list(ex.map(lambda _: run_one(), range(50)))

        bal = store.get_balance("u1")
        assert bal.balance == Decimal(1000) - Decimal(50) * Decimal(7)  # 650, exact
        assert bal.balance >= Decimal(0)
        assert store.get_available("u1").reserved == Decimal(0)


# ── Idempotency-key collisions (user-scoped) ───────────────────────────────


class TestIdempotency:
    def test_same_key_across_two_leases_charges_once(self, store: MemoryStore) -> None:
        store.add_credits("u1", Decimal(200))
        l1 = store.create_lease("u1", Decimal(50), "usage", floor=Decimal(0))
        l2 = store.create_lease("u1", Decimal(50), "usage", floor=Decimal(0))

        d1 = store.settle_lease("u1", l1.lease_id, Decimal(50), idempotency_key="dup")
        d2 = store.settle_lease("u1", l2.lease_id, Decimal(50), idempotency_key="dup")
        # Second settle replays the first's result — the shared pool is debited once.
        assert d2.idempotent is True
        assert d2.transaction_id == d1.transaction_id
        assert store.get_balance("u1").balance == Decimal(150)

    def test_resettle_with_different_amount_replays_original(self, store: MemoryStore) -> None:
        store.add_credits("u1", Decimal(100))
        lease = store.create_lease("u1", Decimal(50), "usage", floor=Decimal(0))
        first = store.settle_lease("u1", lease.lease_id, Decimal(20))
        # A re-settle (even with a different amount) must NOT charge again.
        second = store.settle_lease("u1", lease.lease_id, Decimal(999))
        assert second.idempotent is True
        assert second.amount == first.amount
        assert store.get_balance("u1").balance == Decimal(80)


# ── Exact Decimal precision (no binary-float drift) ────────────────────────


class TestPrecision:
    def test_fractional_reserve_settle_is_exact(self, store: MemoryStore) -> None:
        store.add_credits("u1", Decimal(1))
        for _ in range(3):
            lease = store.create_lease("u1", Decimal("0.0001"), "usage", floor=Decimal(0))
            assert lease.error is None
            store.settle_lease("u1", lease.lease_id, Decimal("0.0001"))
        assert store.get_balance("u1").balance == Decimal("0.9997")

    def test_settle_smaller_than_fractional_hold(self, store: MemoryStore) -> None:
        store.add_credits("u1", Decimal("10.5"))
        lease = store.create_lease("u1", Decimal("3.3333"), "usage", floor=Decimal(0))
        ded = store.settle_lease("u1", lease.lease_id, Decimal("1.1111"))
        assert ded.balance_after == Decimal("9.3889")


# ── Invalid inputs ─────────────────────────────────────────────────────────


class TestInvalidInputs:
    @pytest.mark.parametrize("bad", [Decimal(0), Decimal(-5), Decimal("NaN"), Decimal("Infinity")])
    def test_create_lease_rejects_bad_amount(self, store: MemoryStore, bad: Decimal) -> None:
        store.add_credits("u1", Decimal(100))
        assert store.create_lease("u1", bad, "usage", floor=Decimal(0)).error == "invalid_amount"

    @pytest.mark.parametrize("bad", [Decimal(-1), Decimal("NaN"), Decimal("-Infinity")])
    def test_settle_rejects_bad_amount(self, store: MemoryStore, bad: Decimal) -> None:
        store.add_credits("u1", Decimal(100))
        lease = store.create_lease("u1", Decimal(20), "usage", floor=Decimal(0))
        assert store.settle_lease("u1", lease.lease_id, bad).error == "invalid_amount"

    def test_manager_reserve_zero_raises_value_error(self, store: MemoryStore) -> None:
        m = _manager(store)
        store.add_credits("u1", Decimal(100))
        with pytest.raises(ValueError):
            m.reserve("u1", Decimal(0))


# ── Floor-boundary exactness ───────────────────────────────────────────────


class TestFloorBoundary:
    def test_strict_floor_inclusive_boundary(self, store: MemoryStore) -> None:
        store.add_credits("u1", Decimal(100))
        # available - amount == floor is allowed (>= floor).
        assert store.create_lease("u1", Decimal(95), "usage", floor=Decimal(5)).error is None

    def test_strict_floor_just_below_rejected(self, store: MemoryStore) -> None:
        store.add_credits("u2", Decimal(100))
        # available - amount == 4 < floor 5 → rejected.
        assert store.create_lease("u2", Decimal(96), "usage", floor=Decimal(5)).error == "insufficient_credits"

    def test_overdraft_floor_boundary(self, store: MemoryStore) -> None:
        store.add_credits("u1", Decimal(0))
        # 0 - 50 == -50 == floor → allowed.
        ok = store.create_lease("u1", Decimal(50), "usage", billing_mode="overdraft", floor=Decimal(-50))
        assert ok.error is None
        # A fresh hold of 1 more would be -51 < -50 → rejected.
        bad = store.create_lease("u1", Decimal(1), "usage", billing_mode="overdraft", floor=Decimal(-50))
        assert bad.error == "insufficient_credits"


# ── Lease state machine exhaustiveness ─────────────────────────────────────


class TestStateMachine:
    def test_renew_after_settle_is_not_found(self, store: MemoryStore) -> None:
        store.add_credits("u1", Decimal(100))
        lease = store.create_lease("u1", Decimal(20), "usage", floor=Decimal(0))
        store.settle_lease("u1", lease.lease_id, Decimal(20))
        assert store.renew_lease("u1", lease.lease_id, 600).error == "lease_not_found"

    def test_renew_after_release_is_not_found(self, store: MemoryStore) -> None:
        store.add_credits("u1", Decimal(100))
        lease = store.create_lease("u1", Decimal(20), "usage", floor=Decimal(0))
        store.release_lease("u1", lease.lease_id)
        assert store.renew_lease("u1", lease.lease_id, 600).error == "lease_not_found"

    def test_expired_lease_can_be_released(self, store: MemoryStore) -> None:
        store.add_credits("u1", Decimal(100))
        lease = store.create_lease("u1", Decimal(20), "usage", floor=Decimal(0))
        store._reservations[lease.lease_id].expires_at = datetime.now(UTC) - timedelta(seconds=1)
        r = store.release_lease("u1", lease.lease_id)
        assert r.released is True

    def test_other_users_lease_is_not_found(self, store: MemoryStore) -> None:
        store.add_credits("u1", Decimal(100))
        lease = store.create_lease("u1", Decimal(20), "usage", floor=Decimal(0))
        assert store.settle_lease("u2", lease.lease_id, Decimal(20)).error == "lease_not_found"
        assert store.release_lease("u2", lease.lease_id).reason == "not_found"

    def test_get_available_ignores_non_active_holds(self, store: MemoryStore) -> None:
        store.add_credits("u1", Decimal(100))
        settled = store.create_lease("u1", Decimal(10), "usage", floor=Decimal(0))
        released = store.create_lease("u1", Decimal(10), "usage", floor=Decimal(0))
        active = store.create_lease("u1", Decimal(10), "usage", floor=Decimal(0))
        store.settle_lease("u1", settled.lease_id, Decimal(10))
        store.release_lease("u1", released.lease_id)
        avail = store.get_available("u1")
        # Only the one still-active hold (10) counts as reserved; settled debited 10.
        assert avail.balance == Decimal(90)
        assert avail.reserved == Decimal(10)
        assert avail.available == Decimal(80)
        assert active.lease_id  # referenced


# ── Allowance consumed at settle ───────────────────────────────────────────


class TestAllowanceAtSettle:
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

    def test_allowance_offsets_settle_then_depletes(self, store: MemoryStore) -> None:
        m = self._manager_with_allowance(store, Decimal(10))
        store.add_credits("u1", Decimal(100))
        store.set_user_plan("u1", "free")

        l1 = m.reserve("u1", Decimal(20))
        d1 = m.settle("u1", l1.lease_id, Decimal(8))  # fully covered by allowance
        assert d1.allowance_consumed == Decimal(8)
        assert d1.amount == Decimal(0)
        assert store.get_balance("u1").balance == Decimal(100)

        l2 = m.reserve("u1", Decimal(20))
        d2 = m.settle("u1", l2.lease_id, Decimal(8))  # only 2 allowance left → 6 net
        assert d2.allowance_consumed == Decimal(2)
        assert d2.amount == Decimal(6)
        assert store.get_balance("u1").balance == Decimal(94)


# ── Spend caps: deny at admission, advisory at settle ──────────────────────


class TestSpendCaps:
    def test_deny_cap_blocks_admission(self, store: MemoryStore) -> None:
        m = _manager(store)
        store.add_credits("u1", Decimal(1000))
        store.set_spend_cap(SpendCap(user_id="u1", cap_type="monthly", limit=Decimal(10), action="deny"))
        with pytest.raises(CapReachedError):
            m.reserve("u1", Decimal(20))

    def test_warn_cap_does_not_block_settle_but_signals(self, store: MemoryStore) -> None:
        emitter = CreditEventEmitter()
        warnings: list[CreditEvent] = []
        emitter.on("credits.cap_warning", warnings.append)
        m = _manager(store, emitter=emitter)
        store.add_credits("u1", Decimal(1000))
        store.set_spend_cap(SpendCap(user_id="u1", cap_type="monthly", limit=Decimal(10), action="warn"))

        lease = m.reserve("u1", Decimal(20))
        ded = m.settle("u1", lease.lease_id, Decimal(15))  # 15 > 10 warn cap
        assert ded.balance_after == Decimal(985)  # charged in full (advisory)
        assert ded.cap_warning == "warn"
        assert len(warnings) == 1

    def test_deny_cap_at_settle_is_non_blocking_signal(self, store: MemoryStore) -> None:
        emitter = CreditEventEmitter()
        cap_reached: list[CreditEvent] = []
        emitter.on("credits.cap_reached", cap_reached.append)
        m = CreditManager(store=store, emitter=emitter, policy="overdraft", overdraft_floor=Decimal(-500))
        m.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}, "min_balance": 0})
        store.add_credits("u1", Decimal(200))
        # Deny cap 100; admit a small hold (under cap), then settle past the cap.
        store.set_spend_cap(SpendCap(user_id="u1", cap_type="monthly", limit=Decimal(100), action="deny"))

        lease = m.reserve("u1", Decimal(50))  # admission: 0 + 50 ≤ 100 ✓
        ded = m.settle("u1", lease.lease_id, Decimal(120))  # de-clamped, breaches cap
        assert ded.balance_after == Decimal(80)  # work is done → charged in full
        assert ded.cap_warning == "deny"
        assert len(cap_reached) == 1
        assert cap_reached[0].data["blocking"] is False


# ── Overdraft reconciliation + low_balance re-arm ──────────────────────────


class TestOverdraftReconcile:
    def test_debt_then_topup_rearms_low_balance(self, store: MemoryStore) -> None:
        emitter = CreditEventEmitter()
        fired: list[Decimal] = []
        emitter.on("credits.low_balance", lambda e: fired.append(e.data["threshold"]))
        m = CreditManager(
            store=store,
            emitter=emitter,
            policy="overdraft",
            overdraft_floor=Decimal(-100),
            low_balance_thresholds=[Decimal(20)],
        )
        m.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}, "min_balance": 0})
        store.add_credits("u1", Decimal(50))

        l1 = m.reserve("u1", Decimal(40))
        m.settle("u1", l1.lease_id, Decimal(40))  # 50 → 10, crosses 20
        assert fired == [Decimal(20)]

        # Drop further: still below, must NOT re-fire (no re-arm without top-up).
        l2 = m.reserve("u1", Decimal(5))
        m.settle("u1", l2.lease_id, Decimal(5))  # 10 → 5
        assert fired == [Decimal(20)]

        # Top up above the level, then descend again → fires once more.
        m.add_credits("u1", Decimal(95))  # 5 → 100
        l3 = m.reserve("u1", Decimal(85))
        m.settle("u1", l3.lease_id, Decimal(85))  # 100 → 15, crosses 20 again
        assert fired == [Decimal(20), Decimal(20)]


# ── Per-operation policy isolation (mixed chat + batch) ────────────────────


class TestMixedOperations:
    def test_chat_and_batch_share_one_available(self, store: MemoryStore) -> None:
        m = CreditManager(store=store, policy="strict_prepaid")
        m.publish_pricing(
            PricingConfigData(
                models={"_default": "input_tokens * 1"},
                min_balance=Decimal(0),
                plans={
                    "pro": PlanDefinition(
                        id="pro",
                        name="Pro",
                        per_operation={
                            "chat": OperationPolicy(billing_mode="strict", max_concurrent=1),
                            "batch": OperationPolicy(billing_mode="strict", max_concurrent=5),
                        },
                    )
                },
            )
        )
        store.add_credits("u1", Decimal(100))
        store.set_user_plan("u1", "pro")

        # Both operation types lease against the SAME available pool under one lock.
        m.reserve("u1", Decimal(60), operation_type="chat")
        # Only 40 available now; a 60-credit batch can't fit even though its own
        # concurrency slot is free → cross-operation overspend is impossible.
        with pytest.raises(InsufficientCreditsError):
            m.reserve("u1", Decimal(60), operation_type="batch")
        # A 40 batch fits exactly.
        assert m.reserve("u1", Decimal(40), operation_type="batch").lease_id

        # And chat's max_concurrent=1 still blocks a second chat.
        with pytest.raises(ConcurrencyLimitError):
            m.reserve("u1", Decimal(1), operation_type="chat")


# ── Randomized property invariant ──────────────────────────────────────────


class TestPropertyInvariant:
    """Fuzz interleaved reserve/settle/release and assert the money ledger
    invariant holds at every step: ``balance == initial − Σ settled actuals``,
    ``reserved == Σ active holds``, ``available == balance − reserved``, and (in
    strict mode, with actual ≤ hold) ``balance`` never drops below the floor."""

    def test_randomized_ledger_invariant(self, store: MemoryStore) -> None:
        import random

        rng = random.Random(1729)  # fixed seed → deterministic
        initial = Decimal(10_000)
        store.add_credits("u1", initial)

        open_holds: dict[str, Decimal] = {}  # lease_id → hold amount (active only)
        expected_balance = initial

        for _ in range(400):
            roll = rng.random()
            if roll < 0.5 or not open_holds:
                # Reserve a fresh worst-case hold (strict, floor 0).
                amount = Decimal(rng.randint(1, 40))
                res = store.create_lease("u1", amount, "usage", floor=Decimal(0))
                if res.error is None:
                    open_holds[res.lease_id] = amount
                # else: legitimately rejected (would breach floor) — no state change.
            elif roll < 0.8:
                # Settle a random open lease with actual ≤ hold (strict discipline).
                lease_id = rng.choice(list(open_holds))
                hold = open_holds.pop(lease_id)
                actual = Decimal(rng.randint(0, int(hold)))
                ded = store.settle_lease("u1", lease_id, actual)
                assert ded.error is None
                expected_balance -= actual
            else:
                # Release a random open lease (no charge).
                lease_id = rng.choice(list(open_holds))
                open_holds.pop(lease_id)
                store.release_lease("u1", lease_id)

            avail = store.get_available("u1")
            expected_reserved = sum(open_holds.values(), Decimal(0))
            assert avail.balance == expected_balance
            assert avail.reserved == expected_reserved
            assert avail.available == expected_balance - expected_reserved
            assert avail.balance >= Decimal(0)  # strict floor never breached
