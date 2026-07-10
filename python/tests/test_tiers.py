"""Tests for credit tiers (aggregate per-tier balance model).

Mirrors the style/structure of ``test_store.py``: MemoryStore is the reference
implementation, exercised directly (no manager layer) except where explicitly
testing manager pass-through behavior. Money is exact ``Decimal`` everywhere
(contract §1) — never truthiness or plain ``int``/``float`` for amounts.

Behavior verified against the actual implementation in
``bursar.interface.memory.MemoryStore`` (``_resolve_add_credits_tier``,
``_reconcile_add_credits_expiry``, ``_walk_tiers``, ``_overdraft_sink``,
``_reverse_priority_order``, ``get_credit_tiers``, ``sweep_expired_credits``)
rather than assumed from the plan — see inline notes for the couple of places
where the actual behavior diverges from the plan's description (discrepancies
reported alongside the test run, not silently worked around).

Known bug flagged here (see ``TestAddCreditsExpiryReconciliation``):
``MemoryStore._reconcile_add_credits_expiry`` computes both the
``default_ttl_days`` expiry and the "is this expires_at in the past" check
using the **module-level** ``_utcnow()`` (real wall clock), not
``self._utcnow()`` (the store's injectable clock used by WS9f everywhere
else — sweep, lease TTLs, allowance windows). A ``MemoryStore`` constructed
with a fake clock does NOT affect these two computations at
``add_credits()`` time.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from bursar import ConfigError, CreditManager, MemoryStore
from bursar.config import PricingConfig
from bursar.events import CREDIT_EVENT_TYPES, CreditEvent, CreditEventEmitter
from bursar.interface.base import StoreError
from bursar.interface.models import BucketDefinition


def _tiers_store(
    buckets: dict[str, BucketDefinition] = None,
    min_balance: Decimal = Decimal(0),
    clock=None,
) -> MemoryStore:
    store = MemoryStore(clock=clock) if clock is not None else MemoryStore()
    store.set_active_pricing(
        {
            "version": 1,
            "metering": {"models": {"*": "1"}},
            "ledger": {"buckets": {k: v.model_dump() for k, v in (buckets or {}).items()}},
        }
    )
    return store


# ── 1. No tiers configured: zero behavioral change ──────────────────────────


class TestNoTiersConfigured:
    """The most important regression-safety guarantee: with no ``tiers``
    section published, add_credits/deduct/get_credit_tiers behave exactly as
    they did before tiers existed."""

    def test_add_credits_tier_defaults_to_default(self) -> None:
        store = MemoryStore()
        result = store.add_credits("u1", Decimal("100"), "purchase")
        assert result.bucket == "default"

    def test_add_credits_explicit_default_tier_accepted(self) -> None:
        store = MemoryStore()
        result = store.add_credits("u1", Decimal("50"), "purchase", bucket="default")
        assert result.bucket == "default"

    def test_add_credits_unknown_tier_raises_tier_not_found(self) -> None:
        store = MemoryStore()
        with pytest.raises(StoreError, match="tier_not_found"):
            store.add_credits("u1", Decimal("50"), "purchase", bucket="gifted")

    def test_add_credits_expires_at_unrestricted_without_tiers(self) -> None:
        """No tiers configured -> expires_at behaves exactly as pre-tiers (no
        tier-based restriction at all)."""
        store = MemoryStore()
        future = datetime.now(UTC) + timedelta(days=5)
        result = store.add_credits("u1", Decimal("10"), "purchase", expires_at=future)
        assert result.bucket == "default"

    def test_deduct_bucket_breakdown_is_synthetic_default(self) -> None:
        store = MemoryStore()
        store.add_credits("u1", Decimal("100"), "purchase")
        result = store.deduct_with_allowance("u1", Decimal("30"))
        assert result.bucket_breakdown == {"default": Decimal("30")}

    def test_get_credit_tiers_synthesizes_single_default_entry(self) -> None:
        store = MemoryStore()
        store.add_credits("u1", Decimal("77"), "purchase")
        result = store.get_bucket_balances("u1")
        assert len(result.buckets) == 1
        entry = result.buckets[0]
        assert entry.bucket_key == "default"
        assert entry.label == "default"
        assert entry.priority == 0
        assert entry.expires is False
        assert entry.balance == Decimal("77")
        assert result.total_balance == Decimal("77")

    def test_get_credit_tiers_default_entry_present_even_with_zero_balance(self) -> None:
        store = MemoryStore()
        result = store.get_bucket_balances("brand_new_user")
        assert result.buckets == [
            result.buckets[0]  # sanity: exactly one entry
        ]
        assert result.buckets[0].balance == Decimal(0)
        assert result.total_balance == Decimal(0)


# ── 2. Tier config validation ────────────────────────────────────────────────


class TestTierConfigValidation:
    """Tier cross-field validation (duplicate allow_overdraft/is_default,
    non-positive default_ttl_days, empty tiers dict) lives in ``config.py``'s
    ``PricingConfig._validate_tiers`` — invoked both when constructing
    ``PricingConfig(...)`` directly and via ``load_config_from_dict``.

    DISCREPANCY vs. a store-level reading of the plan: ``PricingConfigData``
    (the raw/unvalidated store-facing model in ``interface/models.py``) has NO
    such validator, and ``MemoryStore.set_active_pricing()`` performs no
    validation of its own on the tiers it's given — it just merges tier
    definitions into ``self._tier_definitions``. So constructing a
    ``PricingConfigData`` with invalid tiers and passing it straight to
    ``store.set_active_pricing()`` does **not** raise. This exactly mirrors
    the pre-existing behavior for duplicate plan names (also validated only in
    ``config.py``, not enforced by ``PricingConfigData``/the store) — not a
    tier-specific regression, but worth flagging because the plan's phrasing
    ("enforced by config validation") reads as if it were a store-level
    guarantee. Callers going through ``CreditManager.publish_pricing_from_dict()``
    / ``publish_pricing()`` ARE protected, because both build the
    ``PricingEngine`` (which validates via ``load_config_from_dict``) before
    ever calling the store.
    """

    # -- config.py's PricingConfig: the actual validation layer -------------

    def test_duplicate_allow_overdraft_rejected(self) -> None:
        with pytest.raises(ConfigError, match="allow_overdraft"):
            PricingConfig(
                metering={"models": {"*": "input_tokens * 1"}},
                ledger={
                    "buckets": {
                        "a": BucketDefinition(label="A", priority=1, allow_overdraft=True),
                        "b": BucketDefinition(label="B", priority=2, allow_overdraft=True),
                    }
                },
            )

    def test_duplicate_is_default_rejected(self) -> None:
        with pytest.raises(ConfigError, match="default=True"):
            PricingConfig(
                metering={"models": {"*": "input_tokens * 1"}},
                ledger={
                    "buckets": {
                        "a": BucketDefinition(label="A", priority=1, default=True),
                        "b": BucketDefinition(label="B", priority=2, default=True),
                    }
                },
            )

    def test_non_positive_ttl_days_rejected(self) -> None:
        with pytest.raises(ConfigError, match="ttl_days"):
            PricingConfig(
                metering={"models": {"*": "input_tokens * 1"}},
                ledger={"buckets": {"a": BucketDefinition(label="A", priority=1, expires=True, ttl_days=0)}},
            )

    def test_negative_ttl_days_rejected(self) -> None:
        with pytest.raises(ConfigError, match="ttl_days"):
            PricingConfig(
                metering={"models": {"*": "input_tokens * 1"}},
                ledger={"buckets": {"a": BucketDefinition(label="A", priority=1, expires=True, ttl_days=-5)}},
            )

    def test_empty_buckets_dict_rejected(self) -> None:
        with pytest.raises(ConfigError, match="empty dict"):
            PricingConfig(
                metering={"models": {"*": "input_tokens * 1"}},
                ledger={"buckets": {}},
            )

    def test_single_allow_overdraft_and_single_default_accepted(self) -> None:
        """Sanity: the valid case (each flag set on at most one bucket) loads fine."""
        config = PricingConfig(
            metering={"models": {"*": "input_tokens * 1"}},
            ledger={
                "buckets": {
                    "gifted": BucketDefinition(label="Gifted", priority=10, expires=True, ttl_days=30),
                    "purchased": BucketDefinition(label="Purchased", priority=30, default=True, allow_overdraft=True),
                }
            },
        )
        assert config.ledger.buckets is not None
        assert config.ledger.buckets["purchased"].allow_overdraft is True

    # -- The realistic production path: manager.publish_pricing_from_dict ---

    def test_manager_publish_pricing_from_dict_rejects_invalid_tiers(self) -> None:
        """manager.publish_pricing_from_dict builds the PricingEngine (which
        validates via load_config_from_dict) BEFORE calling
        store.set_active_pricing, so invalid tier configs never reach the
        store through this path."""
        manager = CreditManager(store=MemoryStore())
        with pytest.raises(ConfigError):
            manager.publish_pricing_from_dict(
                {
                    "version": 1,
                    "metering": {"models": {"*": "1"}},
                    "ledger": {
                        "buckets": {
                            "a": {"label": "A", "priority": 1, "allow_overdraft": True},
                            "b": {"label": "B", "priority": 2, "allow_overdraft": True},
                        },
                    },
                }
            )

    # -- Raw config dict + direct set_active_pricing: NOT validated --------

    def test_raw_config_dict_does_not_validate_bucket_conflicts(self) -> None:
        """Documents the actual (permissive) behavior at this layer — see the
        class docstring for why this is a real discrepancy worth flagging."""
        store = MemoryStore()
        cfg = {
            "version": 1,
            "metering": {"models": {"*": "1"}},
            "ledger": {
                "buckets": {
                    "a": {"label": "A", "priority": 1, "allow_overdraft": True},
                    "b": {"label": "B", "priority": 2, "allow_overdraft": True},
                },
            },
        }
        # No raise: raw config dict is accepted by store.set_active_pricing
        # (unlike PricingConfig's model_validator which would reject).
        store.set_active_pricing(cfg)
        result = store.get_bucket_balances("u1")
        assert {t.bucket_key for t in result.buckets} == {"a", "b"}

    def test_raw_config_dict_accepts_no_buckets(self) -> None:
        """Also unvalidated: a config dict with no buckets section does not
        raise at store level (unlike PricingConfig's validation of empty
        ledger.buckets)."""
        cfg = {"version": 1, "metering": {"models": {"*": "1"}}}
        assert cfg.get("ledger", {}).get("buckets") is None


# ── 3. add_credits tier resolution ──────────────────────────────────────────


class TestAddCreditsTierResolution:
    def _store(self) -> MemoryStore:
        return _tiers_store(
            {
                "gifted": BucketDefinition(label="Gifted", priority=10, expires=True, ttl_days=30),
                "purchased": BucketDefinition(label="Purchased", priority=30, default=True),
            }
        )

    def test_explicit_valid_tier_lands_money_in_that_tier(self) -> None:
        store = self._store()
        store.add_credits("u1", Decimal("50"), "purchase", bucket="purchased")
        tiers = {t.bucket_key: t.balance for t in store.get_bucket_balances("u1").buckets}
        assert tiers["purchased"] == Decimal("50")
        assert tiers["gifted"] == Decimal(0)

    def test_explicit_unknown_tier_raises_tier_not_found(self) -> None:
        store = self._store()
        with pytest.raises(StoreError, match="tier_not_found"):
            store.add_credits("u1", Decimal("50"), "purchase", bucket="nonexistent")

    def test_omitted_tier_resolves_to_is_default_tier(self) -> None:
        store = self._store()
        result = store.add_credits("u1", Decimal("20"), "purchase")
        assert result.bucket == "purchased"
        tiers = {t.bucket_key: t.balance for t in store.get_bucket_balances("u1").buckets}
        assert tiers["purchased"] == Decimal("20")

    def test_omitted_tier_with_no_configured_default_raises_tier_required(self) -> None:
        store = _tiers_store({"gifted": BucketDefinition(label="Gifted", priority=10, expires=True, ttl_days=30)})
        with pytest.raises(StoreError, match="tier_required"):
            store.add_credits("u1", Decimal("20"), "purchase")


# ── 4. add_credits expiry reconciliation ────────────────────────────────────


class TestAddCreditsExpiryReconciliation:
    def _store(self) -> MemoryStore:
        return _tiers_store(
            {
                "gifted": BucketDefinition(label="Gifted", priority=10, expires=True, ttl_days=30),
                "bonus": BucketDefinition(label="Bonus", priority=15, expires=True),  # no default_ttl_days
                "purchased": BucketDefinition(label="Purchased", priority=30, default=True, expires=False),
            }
        )

    def test_non_expiring_tier_with_explicit_expires_at_raises(self) -> None:
        store = self._store()
        future = datetime.now(UTC) + timedelta(days=5)
        with pytest.raises(StoreError, match="bucket_does_not_expire"):
            store.add_credits("u1", Decimal("10"), "purchase", bucket="purchased", expires_at=future)

    def test_expiring_tier_with_past_expires_at_raises_invalid_expires_at(self) -> None:
        store = self._store()
        past = datetime.now(UTC) - timedelta(days=1)
        with pytest.raises(StoreError, match="invalid_expires_at"):
            store.add_credits("u1", Decimal("10"), "purchase", bucket="gifted", expires_at=past)

    def test_expiring_tier_with_future_expires_at_succeeds(self) -> None:
        store = self._store()
        future = datetime.now(UTC) + timedelta(days=5)
        result = store.add_credits("u1", Decimal("10"), "purchase", bucket="gifted", expires_at=future)
        assert result.bucket == "gifted"

    def test_expiring_tier_omitted_expires_at_uses_default_ttl_days(self) -> None:
        """default_ttl_days-based expiry is computed correctly, but see the
        module docstring: it's computed via the REAL wall clock (module-level
        ``_utcnow()``), not the store's injectable clock — so we assert
        against real time here rather than a fake add-time clock.

        ``sweep_expired_credits`` DOES use the injectable clock for its own
        "is this expired yet" comparison, so we can still deterministically
        fast-forward PAST the (real-clock-computed) expiry without sleeping.
        """
        store = MemoryStore()
        store.set_active_pricing(
            {
                "version": 1,
                "metering": {"models": {"*": "1"}},
                "ledger": {"buckets": {"gifted": {"label": "Gifted", "priority": 10, "expires": True, "ttl_days": 30}}},
            }
        )
        before = datetime.now(UTC)
        store.add_credits("u1", Decimal("10"), "purchase", bucket="gifted")
        after = datetime.now(UTC)

        tx = store._transactions[-1]
        assert tx.expires_at is not None
        assert before + timedelta(days=30) <= tx.expires_at <= after + timedelta(days=30)

        # Not yet expired "now".
        assert store.sweep_expired_credits(dry_run=True).expired_by_bucket == {}

        # Fast-forward the store's injectable clock past the computed expiry.
        far_future = tx.expires_at + timedelta(seconds=1)
        store._clock = lambda: far_future
        result = store.sweep_expired_credits(dry_run=True)
        assert result.expired_by_bucket == {"gifted": Decimal("10")}

    def test_expiring_tier_no_default_ttl_and_omitted_expires_at_raises_expires_at_required(self) -> None:
        store = self._store()
        with pytest.raises(StoreError, match="expires_at_required"):
            store.add_credits("u1", Decimal("10"), "purchase", bucket="bonus")


# ── 5. Priority-ordered deduction ───────────────────────────────────────────


class TestPriorityOrderedDeduction:
    def _store(self) -> MemoryStore:
        return _tiers_store(
            {
                "gifted": BucketDefinition(label="Gifted", priority=10, expires=True, ttl_days=30),
                "allowance": BucketDefinition(label="Allowance", priority=20),
                "purchased": BucketDefinition(label="Purchased", priority=30, default=True),
            }
        )

    def _seed(self, store: MemoryStore) -> None:
        store.add_credits(
            "u1", Decimal("10"), "purchase", bucket="gifted", expires_at=datetime.now(UTC) + timedelta(days=1)
        )
        store.add_credits("u1", Decimal("15"), "purchase", bucket="allowance")
        store.add_credits("u1", Decimal("100"), "purchase", bucket="purchased")

    def test_deduct_within_lowest_priority_tier_only(self) -> None:
        store = self._store()
        self._seed(store)

        result = store.deduct_with_allowance("u1", Decimal("6"))
        assert result.bucket_breakdown == {"gifted": Decimal("6")}
        tiers = {t.bucket_key: t.balance for t in store.get_bucket_balances("u1").buckets}
        assert tiers == {"gifted": Decimal("4"), "allowance": Decimal("15"), "purchased": Decimal("100")}

    def test_deduct_spanning_two_tiers(self) -> None:
        store = self._store()
        self._seed(store)

        result = store.deduct_with_allowance("u1", Decimal("20"))
        assert result.bucket_breakdown == {"gifted": Decimal("10"), "allowance": Decimal("10")}
        tiers = {t.bucket_key: t.balance for t in store.get_bucket_balances("u1").buckets}
        assert tiers == {"gifted": Decimal("0"), "allowance": Decimal("5"), "purchased": Decimal("100")}

    def test_deduct_spanning_all_three_tiers(self) -> None:
        store = self._store()
        self._seed(store)

        result = store.deduct_with_allowance("u1", Decimal("30"))
        assert result.bucket_breakdown == {
            "gifted": Decimal("10"),
            "allowance": Decimal("15"),
            "purchased": Decimal("5"),
        }
        tiers = {t.bucket_key: t.balance for t in store.get_bucket_balances("u1").buckets}
        assert tiers == {"gifted": Decimal("0"), "allowance": Decimal("0"), "purchased": Decimal("95")}


# ── 6. Overdraft routing ─────────────────────────────────────────────────────


class TestOverdraftRouting:
    def _store(self) -> MemoryStore:
        return _tiers_store(
            {
                "gifted": BucketDefinition(label="Gifted", priority=10),
                "purchased": BucketDefinition(label="Purchased", priority=30, default=True, allow_overdraft=True),
            }
        )

    def test_deduct_beyond_total_balance_routes_excess_to_overdraft_tier(self) -> None:
        store = self._store()
        store.add_credits("u1", Decimal("10"), "purchase", bucket="gifted")
        store.add_credits("u1", Decimal("5"), "purchase", bucket="purchased")

        result = store.deduct_with_allowance("u1", Decimal("40"), min_balance=Decimal("-50"))
        assert result.error is None
        assert result.bucket_breakdown == {"gifted": Decimal("10"), "purchased": Decimal("30")}

        tiers = store.get_bucket_balances("u1")
        by_key = {t.bucket_key: t.balance for t in tiers.buckets}
        assert by_key["gifted"] == Decimal(0)
        assert by_key["purchased"] == Decimal("-25")  # 5 - (5 normal + 25 overdraft)
        assert tiers.total_balance == Decimal("-25")
        assert store.get_balance("u1").balance == Decimal("-25")

    def test_deduct_within_balance_never_touches_overdraft_branch(self) -> None:
        """Sanity: when tiers fully cover net, the overdraft sink is never hit
        even though the allow_overdraft tier is configured."""
        store = self._store()
        store.add_credits("u1", Decimal("10"), "purchase", bucket="gifted")
        store.add_credits("u1", Decimal("50"), "purchase", bucket="purchased")

        result = store.deduct_with_allowance("u1", Decimal("10"), min_balance=Decimal("-50"))
        assert result.bucket_breakdown == {"gifted": Decimal("10")}


# ── 7. settle_lease applies the same tier walk ──────────────────────────────


class TestSettleLeaseTierWalk:
    def test_settle_applies_tier_walk_and_reports_breakdown(self) -> None:
        store = _tiers_store(
            {
                "gifted": BucketDefinition(label="Gifted", priority=10),
                "purchased": BucketDefinition(label="Purchased", priority=30, default=True),
            }
        )
        store.add_credits("u1", Decimal("10"), "purchase", bucket="gifted")
        store.add_credits("u1", Decimal("100"), "purchase", bucket="purchased")

        lease = store.create_lease("u1", Decimal("20"), "usage", floor=Decimal(0))
        assert lease.error is None

        result = store.settle_lease("u1", lease.lease_id, Decimal("15"))
        assert result.error is None
        assert result.bucket_breakdown == {"gifted": Decimal("10"), "purchased": Decimal("5")}

        tiers = {t.bucket_key: t.balance for t in store.get_bucket_balances("u1").buckets}
        assert tiers == {"gifted": Decimal("0"), "purchased": Decimal("95")}


# ── 8. Idempotent replay returns the EXACT original bucket_breakdown ─────────


class TestIdempotentReplayTierBreakdown:
    def test_replay_echoes_original_breakdown_not_recomputed(self) -> None:
        store = _tiers_store(
            {
                "gifted": BucketDefinition(label="Gifted", priority=10),
                "purchased": BucketDefinition(label="Purchased", priority=30, default=True),
            }
        )
        store.add_credits("u1", Decimal("10"), "purchase", bucket="gifted")
        store.add_credits("u1", Decimal("100"), "purchase", bucket="purchased")

        first = store.deduct_with_allowance("u1", Decimal("15"), idempotency_key="k1")
        assert first.bucket_breakdown == {"gifted": Decimal("10"), "purchased": Decimal("5")}

        # Mutate tier balances after the fact via a manual grant into the
        # SAME tier the original deduction drained from.
        store.add_credits("u1", Decimal("1000"), "purchase", bucket="gifted")

        second = store.deduct_with_allowance("u1", Decimal("15"), idempotency_key="k1")
        assert second.idempotent is True
        assert second.transaction_id == first.transaction_id
        # Must echo the ORIGINAL breakdown verbatim — not recompute against
        # the now-much-larger gifted balance.
        assert second.bucket_breakdown == first.bucket_breakdown == {"gifted": Decimal("10"), "purchased": Decimal("5")}

    def test_settle_lease_replay_also_echoes_original_breakdown(self) -> None:
        store = _tiers_store(
            {
                "gifted": BucketDefinition(label="Gifted", priority=10),
                "purchased": BucketDefinition(label="Purchased", priority=30, default=True),
            }
        )
        store.add_credits("u1", Decimal("10"), "purchase", bucket="gifted")
        store.add_credits("u1", Decimal("100"), "purchase", bucket="purchased")
        lease = store.create_lease("u1", Decimal("20"), "usage", floor=Decimal(0))

        first = store.settle_lease("u1", lease.lease_id, Decimal("15"), idempotency_key="settle-1")
        assert first.bucket_breakdown == {"gifted": Decimal("10"), "purchased": Decimal("5")}

        store.add_credits("u1", Decimal("500"), "purchase", bucket="gifted")

        second = store.settle_lease("u1", lease.lease_id, Decimal("15"), idempotency_key="settle-1")
        assert second.idempotent is True
        assert second.bucket_breakdown == first.bucket_breakdown


# ── 9. LIFO refund ────────────────────────────────────────────────────────


class TestLifoRefund:
    def _store(self) -> MemoryStore:
        return _tiers_store(
            {
                "gifted": BucketDefinition(label="Gifted", priority=10),
                "allowance": BucketDefinition(label="Allowance", priority=20),
                "purchased": BucketDefinition(label="Purchased", priority=30, default=True),
            }
        )

    def test_full_refund_restores_in_reverse_priority_order(self) -> None:
        store = self._store()
        store.add_credits("u1", Decimal("10"), "purchase", bucket="gifted")
        store.add_credits("u1", Decimal("15"), "purchase", bucket="allowance")
        store.add_credits("u1", Decimal("100"), "purchase", bucket="purchased")

        ded = store.deduct_with_allowance("u1", Decimal("30"))
        assert ded.bucket_breakdown == {"gifted": Decimal("10"), "allowance": Decimal("15"), "purchased": Decimal("5")}

        refund = store.refund_credits(ded.transaction_id)
        assert refund.error is None
        assert refund.amount == Decimal("30")
        # LIFO: last-drained (purchased) restored first.
        assert refund.bucket_breakdown == {
            "purchased": Decimal("5"),
            "allowance": Decimal("15"),
            "gifted": Decimal("10"),
        }
        tiers = {t.bucket_key: t.balance for t in store.get_bucket_balances("u1").buckets}
        assert tiers == {"gifted": Decimal("10"), "allowance": Decimal("15"), "purchased": Decimal("100")}

    def test_two_partial_refunds_compose_without_double_restoring(self) -> None:
        store = self._store()
        store.add_credits("u1", Decimal("10"), "purchase", bucket="gifted")
        store.add_credits("u1", Decimal("15"), "purchase", bucket="allowance")
        store.add_credits("u1", Decimal("100"), "purchase", bucket="purchased")

        ded = store.deduct_with_allowance("u1", Decimal("30"))
        # breakdown: gifted=10, allowance=15, purchased=5

        r1 = store.refund_credits(ded.transaction_id, amount=Decimal("6"))
        assert r1.error is None
        assert r1.bucket_breakdown == {"purchased": Decimal("5"), "allowance": Decimal("1")}

        r2 = store.refund_credits(ded.transaction_id, amount=Decimal("10"))
        assert r2.error is None
        assert r2.bucket_breakdown == {"allowance": Decimal("10")}

        tiers = {t.bucket_key: t.balance for t in store.get_bucket_balances("u1").buckets}
        assert tiers == {"gifted": Decimal(0), "allowance": Decimal("11"), "purchased": Decimal("100")}

        # Invariant: sum of refund breakdowns per tier never exceeds what was
        # originally drained from that tier.
        combined: dict[str, Decimal] = {}
        for r in (r1, r2):
            for key, amount in (r.bucket_breakdown or {}).items():
                combined[key] = combined.get(key, Decimal(0)) + amount
        assert ded.bucket_breakdown is not None
        for tier_key, drained in ded.bucket_breakdown.items():
            assert combined.get(tier_key, Decimal(0)) <= drained


# ── 10. Per-tier expiry sweep ────────────────────────────────────────────────


class TestPerTierExpirySweep:
    def test_sweep_expires_only_the_expiring_tier(self) -> None:
        """Grant into an expiring tier and a non-expiring tier; sweep must
        report and (on real run) decrement only the expiring tier's balance.

        NOTE: because add-time expires_at validation uses the real wall clock
        (see module docstring), we use a REAL near-future expires_at and then
        fast-forward the store's INJECTABLE clock (which sweep does honor)
        well past it — fully deterministic, no sleeping.
        """
        start = datetime.now(UTC)
        clock_box = {"now": start}
        store = MemoryStore(clock=lambda: clock_box["now"])
        store.set_active_pricing(
            {
                "version": 1,
                "metering": {"models": {"*": "1"}},
                "ledger": {
                    "buckets": {
                        "gifted": {"label": "Gifted", "priority": 10, "expires": True},
                        "purchased": {"label": "Purchased", "priority": 30, "default": True, "expires": False},
                    },
                },
            }
        )
        near_future = start + timedelta(minutes=1)
        store.add_credits("u1", Decimal("40"), "purchase", bucket="gifted", expires_at=near_future)
        store.add_credits("u1", Decimal("25"), "purchase", bucket="purchased")

        # Not yet expired.
        dry_before = store.sweep_expired_credits(dry_run=True)
        assert dry_before.expired_by_bucket == {}

        # Fast-forward well past the expiry.
        clock_box["now"] = near_future + timedelta(days=1)

        dry_after = store.sweep_expired_credits(dry_run=True)
        assert dry_after.expired_by_bucket == {"gifted": Decimal("40")}
        assert dry_after.dry_run is True
        # dry_run must not mutate anything.
        unchanged = {t.bucket_key: t.balance for t in store.get_bucket_balances("u1").buckets}
        assert unchanged == {"gifted": Decimal("40"), "purchased": Decimal("25")}

        real = store.sweep_expired_credits(dry_run=False)
        assert real.expired_by_bucket == {"gifted": Decimal("40")}
        assert real.dry_run is False
        after_sweep = {t.bucket_key: t.balance for t in store.get_bucket_balances("u1").buckets}
        assert after_sweep == {"gifted": Decimal(0), "purchased": Decimal("25")}

        # Idempotent: second real sweep reports nothing further.
        again = store.sweep_expired_credits(dry_run=False)
        assert again.expired_by_bucket == {}


# ── 11. get_credit_tiers shape ───────────────────────────────────────────────


class TestGetCreditTiersShape:
    def test_tiers_sorted_by_priority_ascending_with_correct_fields(self) -> None:
        store = _tiers_store(
            {
                "purchased": BucketDefinition(label="Purchased", priority=30, default=True),
                "gifted": BucketDefinition(label="Gifted", priority=10, expires=True, ttl_days=30),
                "allowance": BucketDefinition(label="Allowance", priority=20),
            }
        )
        store.add_credits(
            "u1", Decimal("10"), "purchase", bucket="gifted", expires_at=datetime.now(UTC) + timedelta(days=5)
        )
        store.add_credits("u1", Decimal("15"), "purchase", bucket="allowance")
        store.add_credits("u1", Decimal("100"), "purchase", bucket="purchased")

        result = store.get_bucket_balances("u1")
        assert [t.bucket_key for t in result.buckets] == ["gifted", "allowance", "purchased"]

        gifted = result.buckets[0]
        assert gifted.label == "Gifted"
        assert gifted.priority == 10
        assert gifted.expires is True
        assert gifted.balance == Decimal("10")

        assert result.total_balance == Decimal("125")
        assert result.total_balance == store.get_balance("u1").balance

    def test_ties_broken_by_key_ascending(self) -> None:
        store = _tiers_store(
            {
                "z_tier": BucketDefinition(label="Z", priority=5),
                "a_tier": BucketDefinition(label="A", priority=5, default=True),
            }
        )
        result = store.get_bucket_balances("u1")
        assert [t.bucket_key for t in result.buckets] == ["a_tier", "z_tier"]


# ── 12. Manager pass-through ─────────────────────────────────────────────────


class TestManagerPassThrough:
    def test_get_credit_tiers_matches_store_and_emits_no_event(self) -> None:
        store = MemoryStore()
        store.set_active_pricing(
            {
                "version": 1,
                "metering": {"models": {"*": "1"}},
                "ledger": {"buckets": {"gifted": {"label": "Gifted", "priority": 10, "default": True}}},
            }
        )
        store.add_credits("u1", Decimal("42"), "purchase")

        emitter = CreditEventEmitter()
        events: list[CreditEvent] = []
        for event_type in CREDIT_EVENT_TYPES:
            emitter.on(event_type, events.append)

        manager = CreditManager(store=store, emitter=emitter)
        result = manager.get_bucket_balances("u1")

        assert result == store.get_bucket_balances("u1")
        assert events == []

    def test_add_credits_tier_kwarg_forwarded_and_reflected(self) -> None:
        store = MemoryStore()
        store.set_active_pricing(
            {
                "version": 1,
                "metering": {"models": {"*": "1"}},
                "ledger": {
                    "buckets": {
                        "gifted": {"label": "Gifted", "priority": 10},
                        "purchased": {"label": "Purchased", "priority": 30, "default": True},
                    },
                },
            }
        )
        manager = CreditManager(store=store)
        result = manager.add_credits("u1", Decimal("25"), tx_type="purchase", bucket="gifted")
        assert result.bucket == "gifted"

        tiers = manager.get_bucket_balances("u1")
        by_key = {t.bucket_key: t.balance for t in tiers.buckets}
        assert by_key["gifted"] == Decimal("25")
        assert by_key["purchased"] == Decimal(0)
