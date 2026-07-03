"""Tests for ``CreditManager.grant_subscription_cycle`` (Task C).

This is the single call a payment-provider webhook handler makes for a
subscription renewal or signup grant. Requires a store with the target
``tier`` configured (tiers are what let a subscription grant coexist with,
and not clobber, credits from other sources).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from bursar import CreditManager
from bursar.events import CreditEventEmitter
from bursar.interface.memory import MemoryStore


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore()


@pytest.fixture
def manager(store: MemoryStore) -> CreditManager:
    mgr = CreditManager(store=store)
    mgr.publish_pricing_from_dict(
        {
            "models": {"_default": "input_tokens * 1"},
            "min_balance": 0,
            "tiers": {
                "subscription": {
                    "name": "Subscription",
                    "priority": 1,
                    "expires": True,
                    "is_default": True,
                    "default_ttl_days": 30,
                },
            },
        }
    )
    return mgr


def _tier_balance(manager: CreditManager, user_id: str, tier: str) -> Decimal:
    tiers = manager.get_credit_tiers(user_id)
    for t in tiers.tiers:
        if t.tier_key == tier:
            return t.balance
    return Decimal(0)


class TestIdempotency:
    def test_same_idempotency_key_twice_grants_once(self, manager: CreditManager) -> None:
        r1 = manager.grant_subscription_cycle("user_1", 100, ttl_days=30, idempotency_key="evt_1")
        r2 = manager.grant_subscription_cycle("user_1", 100, ttl_days=30, idempotency_key="evt_1")

        assert r1.transaction_id == r2.transaction_id
        assert manager.get_balance("user_1").balance == Decimal(100)
        assert _tier_balance(manager, "user_1", "subscription") == Decimal(100)

    def test_redelivery_does_not_double_grant_or_wipe_balance(self, manager: CreditManager) -> None:
        """Redelivery safety end-to-end: replace_prior defaults to True, so a
        naive implementation that re-runs the "expire prior cycle" step on
        every call (including replays) would incorrectly zero the balance
        that the first call just granted. The whole call must be a no-op."""
        manager.grant_subscription_cycle("user_1", 250, ttl_days=30, idempotency_key="evt_renewal")
        for _ in range(3):
            manager.grant_subscription_cycle("user_1", 250, ttl_days=30, idempotency_key="evt_renewal")

        assert manager.get_balance("user_1").balance == Decimal(250)


class TestReplacePrior:
    def test_replace_prior_true_zeroes_leftover_before_new_grant(self, manager: CreditManager) -> None:
        manager.grant_subscription_cycle("user_1", 100, ttl_days=30, idempotency_key="evt_1")
        manager.grant_subscription_cycle("user_1", 40, ttl_days=30, idempotency_key="evt_2", replace_prior=True)

        assert _tier_balance(manager, "user_1", "subscription") == Decimal(40)

    def test_replace_prior_false_adds_on_top_of_leftover(self, manager: CreditManager) -> None:
        manager.grant_subscription_cycle("user_1", 100, ttl_days=30, idempotency_key="evt_1")
        manager.grant_subscription_cycle("user_1", 40, ttl_days=30, idempotency_key="evt_2", replace_prior=False)

        assert _tier_balance(manager, "user_1", "subscription") == Decimal(140)

    def test_replace_prior_true_is_a_noop_when_leftover_already_zero(self, manager: CreditManager) -> None:
        """First-ever cycle: nothing to replace, so replace_prior=True must not
        error or affect the grant."""
        result = manager.grant_subscription_cycle(
            "user_1", 75, ttl_days=30, idempotency_key="evt_1", replace_prior=True
        )
        assert result.amount == Decimal(75)
        assert _tier_balance(manager, "user_1", "subscription") == Decimal(75)


class TestExpiry:
    def test_ttl_days_matches_equivalent_explicit_expires_at(self, manager: CreditManager, store: MemoryStore) -> None:
        before = datetime.now(UTC)
        manager.grant_subscription_cycle("user_ttl", 100, ttl_days=30, idempotency_key="evt_ttl")
        after = datetime.now(UTC)

        ttl_expires_at = store._transactions[-1].expires_at
        assert ttl_expires_at is not None
        assert before + timedelta(days=30) <= ttl_expires_at <= after + timedelta(days=30)

        explicit_expires_at = before + timedelta(days=30)
        manager.grant_subscription_cycle(
            "user_explicit", 100, expires_at=explicit_expires_at, idempotency_key="evt_explicit"
        )
        explicit_tx_expires_at = store._transactions[-1].expires_at
        assert explicit_tx_expires_at == explicit_expires_at

        # Both computed within the same tight call-time window -> ttl_days
        # produces the same expires_at (mod sub-second jitter) as an
        # equivalent explicit expires_at.
        assert abs((ttl_expires_at - explicit_tx_expires_at).total_seconds()) < 1

    def test_expires_at_and_ttl_days_both_set_raises_value_error(self, manager: CreditManager) -> None:
        with pytest.raises(ValueError, match="mutually exclusive"):
            manager.grant_subscription_cycle(
                "user_1",
                100,
                expires_at=datetime.now(UTC) + timedelta(days=10),
                ttl_days=10,
            )


class TestSideEffects:
    def test_plan_key_reassigns_plan(self, store: MemoryStore) -> None:
        mgr = CreditManager(store=store)
        mgr.publish_pricing_from_dict(
            {
                "models": {"_default": "input_tokens * 1"},
                "min_balance": 0,
                "tiers": {
                    "subscription": {
                        "name": "Subscription",
                        "priority": 1,
                        "expires": True,
                        "is_default": True,
                        "default_ttl_days": 30,
                    },
                },
                "plans": {
                    "pro": {"id": "pro", "name": "Pro"},
                },
            }
        )
        mgr.grant_subscription_cycle("user_1", 100, ttl_days=30, idempotency_key="evt_1", plan_key="pro")
        assert mgr.get_user_plan("user_1").plan_id == "pro"

    def test_emits_credits_cycle_renewed(self, store: MemoryStore) -> None:
        emitter = CreditEventEmitter()
        mgr = CreditManager(store=store, emitter=emitter)
        mgr.publish_pricing_from_dict(
            {
                "models": {"_default": "input_tokens * 1"},
                "min_balance": 0,
                "tiers": {
                    "subscription": {
                        "name": "Subscription",
                        "priority": 1,
                        "expires": True,
                        "is_default": True,
                        "default_ttl_days": 30,
                    },
                },
            }
        )
        events = []
        emitter.on("credits.cycle_renewed", events.append)

        mgr.grant_subscription_cycle("user_1", 100, ttl_days=30, idempotency_key="evt_1")

        assert len(events) == 1
        assert events[0].user_id == "user_1"
        assert events[0].data is not None
        assert events[0].data["amount"] == Decimal(100)
        assert events[0].data["tier"] == "subscription"
        assert events[0].data["idempotency_key"] == "evt_1"

    def test_returned_result_reflects_post_replace_balance(self, manager: CreditManager) -> None:
        manager.grant_subscription_cycle("user_1", 100, ttl_days=30, idempotency_key="evt_1")
        result = manager.grant_subscription_cycle(
            "user_1", 40, ttl_days=30, idempotency_key="evt_2", replace_prior=True
        )
        # The returned AddCreditsResult.new_balance must reflect the actual
        # post-replace balance (100 replaced by 0, then +40), not the
        # pre-replace intermediate (100 + 40 = 140).
        assert result.new_balance == Decimal(40)
