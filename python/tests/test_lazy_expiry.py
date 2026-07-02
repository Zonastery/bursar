"""Tests for ``CreditManager(lazy_expiry=...)`` (Task B).

``lazy_expiry`` is off by default (``False``): behavior is byte-for-byte
identical to before this feature existed — an expired grant stays in the
balance until an explicit ``sweep_expired_credits()`` call (the periodic cron
sweep). With ``lazy_expiry=True``, a per-user sweep runs inline as the first
line of every balance-authoritative read/write (``get_balance``,
``get_credit_tiers``, ``deduct``, ``deduct_fixed``, ``deduct_team``,
``reserve``, ``settle``), so an expired grant is invisible without any
explicit sweep call.

Uses MemoryStore's injectable clock (WS9f) to make a grant's ``expires_at``
already-in-the-past without any wall-clock sleep.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ducto import CreditManager, UsageMetrics
from ducto.events import CreditEventEmitter
from ducto.interface.memory import MemoryStore
from ducto.manager import InsufficientCreditsError

# Fixed store clock: grants below use `expires_at=FIXED_NOW - timedelta(...)`,
# i.e. already expired relative to the store's "now", with no sleeping needed.
FIXED_NOW = datetime(2030, 1, 1, tzinfo=UTC)
PAST_EXPIRY = FIXED_NOW - timedelta(days=1)


def _fixed_clock() -> datetime:
    return FIXED_NOW


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore(clock=_fixed_clock)


def _manager(store: MemoryStore, *, lazy_expiry: bool, **pricing_extra: object) -> CreditManager:
    mgr = CreditManager(store=store, lazy_expiry=lazy_expiry)
    pricing: dict[str, object] = {"models": {"_default": "input_tokens * 1"}, "min_balance": 0}
    pricing.update(pricing_extra)
    mgr.publish_pricing_from_dict(pricing)
    return mgr


class TestLazyExpiryDisabledByDefault:
    """``lazy_expiry=False`` (the default): exact current behavior, unchanged."""

    def test_get_balance_sees_expired_credits_until_explicit_sweep(self, store: MemoryStore) -> None:
        mgr = _manager(store, lazy_expiry=False)
        mgr.add_credits("user_1", 100, expires_at=PAST_EXPIRY)

        # Unswept: the already-expired grant is still counted.
        assert mgr.get_balance("user_1").balance == Decimal(100)

        result = mgr.sweep_expired_credits()
        assert result.expired_count == 1
        assert result.expired_amount == Decimal(100)
        assert mgr.get_balance("user_1").balance == Decimal(0)

    def test_deduct_sees_expired_credits_until_explicit_sweep(self, store: MemoryStore) -> None:
        mgr = _manager(store, lazy_expiry=False)
        mgr.add_credits("user_1", 100, expires_at=PAST_EXPIRY)

        result = mgr.deduct("user_1", UsageMetrics(input_tokens=50))
        assert result.amount == Decimal(50)
        assert result.balance_after == Decimal(50)

    def test_reserve_sees_expired_credits_until_explicit_sweep(self, store: MemoryStore) -> None:
        mgr = _manager(store, lazy_expiry=False)
        mgr.add_credits("user_1", 100, expires_at=PAST_EXPIRY)

        lease = mgr.reserve("user_1", Decimal(50))
        assert lease.amount == Decimal(50)
        assert lease.error is None

    def test_get_available_sees_expired_credits_until_explicit_sweep(self, store: MemoryStore) -> None:
        mgr = _manager(store, lazy_expiry=False)
        mgr.add_credits("user_1", 100, expires_at=PAST_EXPIRY)

        assert mgr.get_available("user_1").available == Decimal(100)


class TestLazyExpiryEnabled:
    """``lazy_expiry=True``: expired grants are invisible without an explicit sweep."""

    def test_get_balance_hides_expired_credits_without_explicit_sweep(self, store: MemoryStore) -> None:
        mgr = _manager(store, lazy_expiry=True)
        mgr.add_credits("user_1", 100, expires_at=PAST_EXPIRY)

        # No call to sweep_expired_credits() anywhere in this test.
        assert mgr.get_balance("user_1").balance == Decimal(0)

    def test_get_credit_tiers_hides_expired_credits(self, store: MemoryStore) -> None:
        mgr = _manager(store, lazy_expiry=True)
        mgr.add_credits("user_1", 100, expires_at=PAST_EXPIRY)

        tiers = mgr.get_credit_tiers("user_1")
        assert tiers.total_balance == Decimal(0)

    def test_get_available_hides_expired_credits(self, store: MemoryStore) -> None:
        """`get_available` is the documented "UI only" read a credits page would
        call to display the user's spendable balance — it must reflect true,
        non-expired credits without any other call having triggered a sweep."""
        mgr = _manager(store, lazy_expiry=True)
        mgr.add_credits("user_1", 100, expires_at=PAST_EXPIRY)

        assert mgr.get_available("user_1").available == Decimal(0)

    def test_can_afford_hides_expired_credits(self, store: MemoryStore) -> None:
        mgr = _manager(store, lazy_expiry=True)
        mgr.add_credits("user_1", 100, expires_at=PAST_EXPIRY)

        result = mgr.can_afford("user_1", Decimal(50))
        assert result.affordable is False
        assert result.spendable == Decimal(0)

    def test_deduct_does_not_see_expired_credits(self, store: MemoryStore) -> None:
        mgr = _manager(store, lazy_expiry=True)
        mgr.add_credits("user_1", 100, expires_at=PAST_EXPIRY)

        with pytest.raises(InsufficientCreditsError):
            mgr.deduct("user_1", UsageMetrics(input_tokens=50))

    def test_deduct_fixed_does_not_see_expired_credits(self, store: MemoryStore) -> None:
        mgr = _manager(store, lazy_expiry=True, fixed={"batch_job": 20})
        mgr.add_credits("user_1", 100, expires_at=PAST_EXPIRY)

        with pytest.raises(InsufficientCreditsError):
            mgr.deduct_fixed("user_1", "batch_job")

    def test_reserve_does_not_see_expired_credits(self, store: MemoryStore) -> None:
        mgr = _manager(store, lazy_expiry=True)
        mgr.add_credits("user_1", 100, expires_at=PAST_EXPIRY)

        with pytest.raises(InsufficientCreditsError):
            mgr.reserve("user_1", Decimal(50))

    def test_settle_triggers_lazy_sweep(self, store: MemoryStore) -> None:
        mgr = _manager(store, lazy_expiry=True)
        # Non-expiring baseline so reserve/settle can succeed at all.
        mgr.add_credits("user_1", 100)
        lease = mgr.reserve("user_1", Decimal(10))
        # A separate, already-expired grant that settle()'s lazy trigger should
        # sweep away before the settle itself runs.
        mgr.add_credits("user_1", 50, expires_at=PAST_EXPIRY)

        mgr.settle("user_1", lease.lease_id, Decimal(10))

        assert mgr.get_balance("user_1").balance == Decimal(90)

    def test_deduct_team_sweeps_the_individual_user_not_the_team(self, store: MemoryStore) -> None:
        mgr = _manager(store, lazy_expiry=True)
        team = store.create_team("Team", Decimal(500))
        store.add_team_member(team.team_id, "user_1")
        mgr.add_credits("user_1", 100, expires_at=PAST_EXPIRY)

        result = mgr.deduct_team(team.team_id, "user_1", UsageMetrics(input_tokens=10))

        # The team pool is unaffected by the user's personal-balance sweep.
        assert result.team_balance_after == Decimal(490)
        # But the side-effect lazy sweep ran against "user_1" (not team_id),
        # clearing their personal expired grant.
        assert mgr.get_balance("user_1").balance == Decimal(0)

    def test_lazy_sweep_emits_scoped_credits_expired_event(self, store: MemoryStore) -> None:
        emitter = CreditEventEmitter()
        mgr = CreditManager(store=store, emitter=emitter, lazy_expiry=True)
        mgr.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}, "min_balance": 0})
        mgr.add_credits("user_1", 100, expires_at=PAST_EXPIRY)

        events = []
        emitter.on("credits.expired", events.append)

        mgr.get_balance("user_1")

        assert len(events) == 1
        assert events[0].user_id == "user_1"
        assert events[0].data is not None
        assert events[0].data["user_id"] == "user_1"
        assert events[0].data["expired_count"] == 1
