"""Adversarial / financial-safety tests for credit buckets (MemoryStore).

Mirrors ``test_lease_adversarial.py``'s style: concurrency, fuzz/precision,
config-drift, and multi-bucket refund edge cases the aggregate per-bucket balance
model must survive without ever losing or double-counting a cent.

Central invariants under test:
    - Money is conserved: Σ(bucket balances) + Σ(consumed) == Σ(granted), always.
    - No bucket balance is ever silently orphaned/stuck (config drift).
    - No rounding drift: bucket_breakdown always sums EXACTLY to the net charged.
    - Repeated partial refunds never restore more to a bucket than was drained
      from it.
    - Team pools are completely unaffected by bucket configuration (out of
      scope per the plan).

See ``test_tiers.py``'s module docstring for the two behavior discrepancies
already flagged (add-time expiry validation uses the real wall clock, not the
injectable one; ``PricingConfigData``/``MemoryStore.set_active_pricing`` don't
validate bucket conflicts). This module adds one more: ``set_active_pricing``
MERGES bucket definitions rather than replacing them, so the plan's "operator
renames/removes a bucket" config-drift scenario is not actually reachable
through the public API — see ``TestConfigDriftSafetyNet``.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from bursar.interface.memory import MemoryStore


def _buckets_store(buckets: dict[str, dict], min_balance: Decimal = Decimal(0)) -> MemoryStore:
    store = MemoryStore()
    store.set_active_pricing(
        {
            "version": 1,
            "metering": {"models": {"*": "1"}},
            "ledger": {"buckets": buckets, "min_balance": str(min_balance)},
        }
    )
    return store


# ── 1. Config-drift safety net ──────────────────────────────────────────────


class TestConfigDriftSafetyNet:
    def _store(self) -> MemoryStore:
        return _buckets_store(
            {
                "gifted": {"label": "Gifted", "priority": 10},
                "purchased": {"label": "Purchased", "priority": 30, "default": True},
            }
        )

    def test_set_active_pricing_does_not_actually_remove_stale_buckets(self) -> None:
        """DISCREPANCY: MemoryStore.set_active_pricing() merges bucket
        definitions into self._bucket_definitions (one assignment per key
        present in the NEW config) rather than replacing the dict wholesale —
        exactly mirroring the pre-existing merge behavior for
        self._plan_definitions. So republishing a config whose ``buckets``
        section omits a previously-configured bucket key does NOT remove or
        orphan it; the old BucketDefinition simply remains active forever.

        This means the "operator renames/removes a bucket" scenario the plan's
        config-drift safety net exists to handle is not actually reachable
        through the public set_active_pricing() API on MemoryStore today — the
        drift/orphan branch in _walk_tiers/_reverse_priority_order can only be
        hit if a user holds a bucket balance under a key that has NEVER been
        published in any config (demonstrated via white-box state surgery in
        test_orphaned_bucket_balance_still_drained_last below, since there is no
        other way to construct that state).
        """
        store = self._store()
        store.add_credits("u1", Decimal("10"), "purchase", tier="gifted")

        # Republish a config that omits "gifted" entirely.
        store.set_active_pricing(
            {
                "metering": {"models": {"*": "1"}},
                "buckets": {"purchased": {"label": "Purchased", "priority": 30, "default": True}},
            }
        )

        # "gifted" is STILL a fully configured bucket — a fresh grant into it
        # still succeeds, and get_bucket_balances still lists it.
        result = store.add_credits("u1", Decimal("1"), "purchase", tier="gifted")
        assert result.bucket == "gifted"
        assert "gifted" in {b.bucket_key for b in store.get_bucket_balances("u1").buckets}

    def test_orphaned_bucket_balance_still_drained_last_not_stuck(self) -> None:
        """The _walk_tiers/_reverse_priority_order drift-safety-net logic
        itself is correct in isolation: a bucket balance held under a key with
        NO current BucketDefinition is walked LAST on deduct (after all
        configured buckets) and restored FIRST on refund — never permanently
        stuck. Constructed via white-box removal of the BucketDefinition (see
        the discrepancy above: there is no public API path that produces this
        state today)."""
        store = self._store()
        store.add_credits("u1", Decimal("10"), "purchase", tier="gifted")
        store.add_credits("u1", Decimal("5"), "purchase", tier="purchased")

        # Simulate the bucket being fully retired from config (white-box).
        del store._bucket_definitions["gifted"]

        result = store.deduct_with_allowance("u1", Decimal("12"))
        assert result.error is None
        # "purchased" (still configured) drains FIRST; the orphaned "gifted"
        # balance is appended LAST and covers the remainder.
        assert result.bucket_breakdown == {"purchased": Decimal("5"), "gifted": Decimal("7")}

        # get_bucket_balances only reports currently-configured buckets, but the
        # orphaned balance is still tracked internally (not lost) — the
        # aggregate total_balance still accounts for it correctly.
        buckets = store.get_bucket_balances("u1")
        assert {b.bucket_key for b in buckets.buckets} == {"purchased"}
        assert store._bucket_balances[("u1", "gifted")] == Decimal("3")  # 10 - 7
        assert buckets.total_balance == store.get_balance("u1").balance == Decimal("3")

        # Refund the full amount: LIFO restoration puts the orphaned bucket
        # (drained last) FIRST, ahead of the still-configured "purchased".
        refund = store.refund_credits(result.transaction_id, amount=Decimal("12"))
        assert refund.error is None
        assert refund.bucket_breakdown == {"gifted": Decimal("7"), "purchased": Decimal("5")}
        assert store._bucket_balances[("u1", "gifted")] == Decimal("10")
        assert store.get_bucket_balances("u1").buckets[0].balance == Decimal("5")  # purchased restored too


# ── 2. No rounding drift ─────────────────────────────────────────────────────


class TestNoRoundingDrift:
    def test_fractional_bucket_balances_sum_exactly_no_float_drift(self) -> None:
        store = _buckets_store(
            {
                "a": {"label": "A", "priority": 1},
                "b": {"label": "B", "priority": 2},
                "c": {"label": "C", "priority": 3, "default": True},
            }
        )
        store.add_credits("u1", Decimal("0.3333"), "purchase", tier="a")
        store.add_credits("u1", Decimal("0.3333"), "purchase", tier="b")
        store.add_credits("u1", Decimal("0.3334"), "purchase", tier="c")

        net = Decimal("1.0000")
        result = store.deduct_with_allowance("u1", net)
        assert result.error is None
        assert result.bucket_breakdown is not None
        assert sum(result.bucket_breakdown.values(), Decimal(0)) == net
        assert result.bucket_breakdown == {
            "a": Decimal("0.3333"),
            "b": Decimal("0.3333"),
            "c": Decimal("0.3334"),
        }
        # Exact Decimal equality — not "close enough" / float tolerance.
        assert all(isinstance(v, Decimal) for v in result.bucket_breakdown.values())

    def test_many_fractional_grants_and_partial_deduct_sums_exactly(self) -> None:
        store = _buckets_store(
            {
                "a": {"label": "A", "priority": 1},
                "b": {"label": "B", "priority": 2},
                "c": {"label": "C", "priority": 3},
                "d": {"label": "D", "priority": 4, "default": True},
            }
        )
        amounts = [Decimal("0.0001"), Decimal("0.0002"), Decimal("0.0003"), Decimal("0.0004")]
        for bucket_key, amount in zip(("a", "b", "c", "d"), amounts, strict=True):
            store.add_credits("u1", amount, "purchase", tier=bucket_key)

        net = Decimal("0.0007")  # spans a, b, c fully and part of d
        result = store.deduct_with_allowance("u1", net)
        assert result.bucket_breakdown is not None
        assert sum(result.bucket_breakdown.values(), Decimal(0)) == net
        assert result.bucket_breakdown == {
            "a": Decimal("0.0001"),
            "b": Decimal("0.0002"),
            "c": Decimal("0.0003"),
            "d": Decimal("0.0001"),
        }


# ── 3. Concurrent deducts across buckets ────────────────────────────────────


class TestConcurrentDeductsAcrossBuckets:
    """MemoryStore serializes every mutating call under a single per-store
    RLock (contract §3, C2), so this is real thread concurrency (mirroring
    test_lease_adversarial.py's TestConcurrencyAdmission), not a simulated
    single-threaded interleaving. Regardless of scheduling, the outcome is
    deterministic here because each individual deduct always fully drains the
    lower-priority bucket before touching the higher-priority one."""

    def test_conservation_of_money_under_concurrent_deducts(self) -> None:
        store = _buckets_store(
            {
                "gifted": {"label": "Gifted", "priority": 10},
                "purchased": {"label": "Purchased", "priority": 30, "default": True},
            }
        )
        store.add_credits("u1", Decimal("1000"), "purchase", tier="gifted")
        store.add_credits("u1", Decimal("1000"), "purchase", tier="purchased")
        total_granted = Decimal("2000")

        attempts = 200
        per_attempt = Decimal("3")

        def attempt() -> None:
            store.deduct_with_allowance("u1", per_attempt)

        with ThreadPoolExecutor(max_workers=16) as ex:
            list(ex.map(lambda _: attempt(), range(attempts)))

        consumed = Decimal(attempts) * per_attempt  # 600; floor 0, 600 <= 2000 so every attempt succeeds
        buckets = store.get_bucket_balances("u1")
        remaining = sum((b.balance for b in buckets.buckets), Decimal(0))

        # Conservation of money: nothing lost or double-spent under the race.
        assert remaining == total_granted - consumed
        assert buckets.total_balance == remaining
        assert store.get_balance("u1").balance == remaining

        # Priority ordering held even under concurrency: "gifted" (priority
        # 10) is fully drained before "purchased is ever touched, regardless
        # of thread interleaving, since each call's bucket walk is atomic.
        by_key = {b.bucket_key: b.balance for b in buckets.buckets}
        assert by_key["gifted"] == Decimal("400")  # 1000 - 600
        assert by_key["purchased"] == Decimal("1000")  # untouched

    def test_concurrent_deducts_never_over_drain_a_bucket(self) -> None:
        """Fuzz variant: interleaved deducts of varying size never push any
        single bucket's balance negative when floor 0 legitimately blocks
        overspend (no allow_overdraft bucket configured)."""
        store = _buckets_store(
            {
                "gifted": {"label": "Gifted", "priority": 10},
                "purchased": {"label": "Purchased", "priority": 30, "default": True},
            }
        )
        store.add_credits("u1", Decimal("500"), "purchase", tier="gifted")
        store.add_credits("u1", Decimal("500"), "purchase", tier="purchased")

        def attempt(amount: Decimal) -> None:
            store.deduct_with_allowance("u1", amount, min_balance=Decimal(0))

        amounts = [Decimal(n % 7 + 1) for n in range(100)]  # 1..7, deterministic
        with ThreadPoolExecutor(max_workers=16) as ex:
            list(ex.map(attempt, amounts))

        buckets = store.get_bucket_balances("u1")
        for b in buckets.buckets:
            assert b.balance >= Decimal(0)
        total_consumed = Decimal(1000) - sum((b.balance for b in buckets.buckets), Decimal(0))
        assert total_consumed >= Decimal(0)
        assert store.get_balance("u1").balance == sum((b.balance for b in buckets.buckets), Decimal(0))


# ── 4. Team pools untouched by bucket configuration ─────────────────────────


class TestTeamPoolsUntouchedByBuckets:
    def test_deduct_team_ignores_bucket_config_entirely(self) -> None:
        store = _buckets_store(
            {
                "gifted": {"label": "Gifted", "priority": 10},
                "purchased": {"label": "Purchased", "priority": 30, "default": True},
            }
        )
        # The user's OWN bucket balance, separate from the team pool.
        store.add_credits("u1", Decimal("100"), "purchase", tier="gifted")
        team = store.create_team("Pool", Decimal("500"))
        store.add_team_member(team.team_id, "u1")

        result = store.deduct_team(team.team_id, "u1", Decimal("50"))
        assert result.error is None
        assert result.team_balance_after == Decimal("450")

        # Team deduction must not touch the user's own bucket balance at all.
        buckets = {b.bucket_key: b.balance for b in store.get_bucket_balances("u1").buckets}
        assert buckets["gifted"] == Decimal("100")
        assert buckets["purchased"] == Decimal(0)
        assert store.get_balance("u1").balance == Decimal("100")

    def test_team_deduction_result_has_no_bucket_fields(self) -> None:
        """Out of scope per the plan: TeamDeductionResult carries no bucket
        concept at all (no bucket_breakdown field)."""
        store = _buckets_store({"gifted": {"label": "Gifted", "priority": 10, "default": True}})
        team = store.create_team("Pool", Decimal("100"))
        store.add_team_member(team.team_id, "u1")
        result = store.deduct_team(team.team_id, "u1", Decimal("10"))
        assert not hasattr(result, "bucket_breakdown")


# ── 5. Multiple partial refunds across 3+ buckets ───────────────────────────


class TestMultiBucketPartialRefunds:
    def test_three_partial_refunds_across_four_buckets_never_over_restore(self) -> None:
        store = _buckets_store(
            {
                "a": {"label": "A", "priority": 10},
                "b": {"label": "B", "priority": 20},
                "c": {"label": "C", "priority": 30},
                "d": {"label": "D", "priority": 40, "default": True},
            }
        )
        store.add_credits("u1", Decimal("10"), "purchase", tier="a")
        store.add_credits("u1", Decimal("10"), "purchase", tier="b")
        store.add_credits("u1", Decimal("10"), "purchase", tier="c")
        store.add_credits("u1", Decimal("10"), "purchase", tier="d")

        ded = store.deduct_with_allowance("u1", Decimal("35"))
        assert ded.bucket_breakdown == {
            "a": Decimal("10"),
            "b": Decimal("10"),
            "c": Decimal("10"),
            "d": Decimal("5"),
        }

        refunds = []
        for amount in (Decimal("8"), Decimal("12"), Decimal("15")):
            r = store.refund_credits(ded.transaction_id, amount=amount)
            assert r.error is None
            refunds.append(r)

        # LIFO composition, verified exactly (highest priority number first:
        # d, then c, then b, then a).
        assert refunds[0].bucket_breakdown == {"d": Decimal("5"), "c": Decimal("3")}
        assert refunds[1].bucket_breakdown == {"c": Decimal("7"), "b": Decimal("5")}
        assert refunds[2].bucket_breakdown == {"b": Decimal("5"), "a": Decimal("10")}

        combined: dict[str, Decimal] = {}
        for r in refunds:
            for bucket_key, amount in (r.bucket_breakdown or {}).items():
                combined[bucket_key] = combined.get(bucket_key, Decimal(0)) + amount

        # Invariant: no bucket ever restored more than was originally drained.
        assert ded.bucket_breakdown is not None
        for bucket_key, drained in ded.bucket_breakdown.items():
            assert combined.get(bucket_key, Decimal(0)) <= drained

        # Fully refunded (8 + 12 + 15 == 35 == the original net charge).
        assert sum(combined.values(), Decimal(0)) == Decimal("35")
        assert combined == {"d": Decimal("5"), "c": Decimal("10"), "b": Decimal("10"), "a": Decimal("10")}

        buckets = {b.bucket_key: b.balance for b in store.get_bucket_balances("u1").buckets}
        assert buckets == {"a": Decimal("10"), "b": Decimal("10"), "c": Decimal("10"), "d": Decimal("10")}

    def test_over_refund_across_buckets_still_rejected(self) -> None:
        """Even with multiple buckets involved, the aggregate over-refund guard
        still holds: total refunds can never exceed the original debit."""
        store = _buckets_store(
            {
                "a": {"label": "A", "priority": 10},
                "b": {"label": "B", "priority": 20, "default": True},
            }
        )
        store.add_credits("u1", Decimal("10"), "purchase", tier="a")
        store.add_credits("u1", Decimal("10"), "purchase", tier="b")

        ded = store.deduct_with_allowance("u1", Decimal("15"))
        r1 = store.refund_credits(ded.transaction_id, amount=Decimal("10"))
        assert r1.error is None

        # Only 5 remains refundable (15 - 10); requesting 6 must be rejected.
        r2 = store.refund_credits(ded.transaction_id, amount=Decimal("6"))
        assert r2.error == "over_refund"

        r3 = store.refund_credits(ded.transaction_id, amount=Decimal("5"))
        assert r3.error is None

        buckets = {b.bucket_key: b.balance for b in store.get_bucket_balances("u1").buckets}
        assert buckets == {"a": Decimal("10"), "b": Decimal("10")}
