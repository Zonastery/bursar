"""Adversarial / financial-safety tests for credit tiers (MemoryStore).

Mirrors ``test_lease_adversarial.py``'s style: concurrency, fuzz/precision,
config-drift, and multi-tier refund edge cases the aggregate per-tier balance
model must survive without ever losing or double-counting a cent.

Central invariants under test:
    - Money is conserved: Σ(tier balances) + Σ(consumed) == Σ(granted), always.
    - No tier balance is ever silently orphaned/stuck (config drift).
    - No rounding drift: tier_breakdown always sums EXACTLY to the net charged.
    - Repeated partial refunds never restore more to a tier than was drained
      from it.
    - Team pools are completely unaffected by tier configuration (out of
      scope per the plan).

See ``test_tiers.py``'s module docstring for the two behavior discrepancies
already flagged (add-time expiry validation uses the real wall clock, not the
injectable one; ``PricingConfigData``/``MemoryStore.set_active_pricing`` don't
validate tier conflicts). This module adds one more: ``set_active_pricing``
MERGES tier definitions rather than replacing them, so the plan's "operator
renames/removes a tier" config-drift scenario is not actually reachable
through the public API — see ``TestConfigDriftSafetyNet``.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from bursar.interface.memory import MemoryStore
from bursar.interface.models import PricingConfigData, TierDefinition


def _tiers_store(tiers: dict[str, TierDefinition], min_balance: Decimal = Decimal(0)) -> MemoryStore:
    store = MemoryStore()
    store.set_active_pricing(
        PricingConfigData(
            models={"_default": "1"},
            tiers=tiers,
            min_balance=min_balance,
        )
    )
    return store


# ── 1. Config-drift safety net ──────────────────────────────────────────────


class TestConfigDriftSafetyNet:
    def _store(self) -> MemoryStore:
        return _tiers_store(
            {
                "gifted": TierDefinition(name="Gifted", priority=10),
                "purchased": TierDefinition(name="Purchased", priority=30, is_default=True),
            }
        )

    def test_set_active_pricing_does_not_actually_remove_stale_tiers(self) -> None:
        """DISCREPANCY: MemoryStore.set_active_pricing() merges tier
        definitions into self._tier_definitions (one assignment per key
        present in the NEW config) rather than replacing the dict wholesale —
        exactly mirroring the pre-existing merge behavior for
        self._plan_definitions. So republishing a config whose ``tiers``
        section omits a previously-configured tier key does NOT remove or
        orphan it; the old TierDefinition simply remains active forever.

        This means the "operator renames/removes a tier" scenario the plan's
        config-drift safety net exists to handle is not actually reachable
        through the public set_active_pricing() API on MemoryStore today — the
        drift/orphan branch in _walk_tiers/_reverse_priority_order can only be
        hit if a user holds a tier balance under a key that has NEVER been
        published in any config (demonstrated via white-box state surgery in
        test_orphaned_tier_balance_still_drained_last below, since there is no
        other way to construct that state).
        """
        store = self._store()
        store.add_credits("u1", Decimal("10"), "purchase", tier="gifted")

        # Republish a config that omits "gifted" entirely.
        store.set_active_pricing(
            PricingConfigData(
                models={"_default": "1"},
                tiers={"purchased": TierDefinition(name="Purchased", priority=30, is_default=True)},
            )
        )

        # "gifted" is STILL a fully configured tier — a fresh grant into it
        # still succeeds, and get_credit_tiers still lists it.
        result = store.add_credits("u1", Decimal("1"), "purchase", tier="gifted")
        assert result.tier == "gifted"
        assert "gifted" in {t.tier_key for t in store.get_credit_tiers("u1").tiers}

    def test_orphaned_tier_balance_still_drained_last_not_stuck(self) -> None:
        """The _walk_tiers/_reverse_priority_order drift-safety-net logic
        itself is correct in isolation: a tier balance held under a key with
        NO current TierDefinition is walked LAST on deduct (after all
        configured tiers) and restored FIRST on refund — never permanently
        stuck. Constructed via white-box removal of the TierDefinition (see
        the discrepancy above: there is no public API path that produces this
        state today)."""
        store = self._store()
        store.add_credits("u1", Decimal("10"), "purchase", tier="gifted")
        store.add_credits("u1", Decimal("5"), "purchase", tier="purchased")

        # Simulate the tier being fully retired from config (white-box).
        del store._tier_definitions["gifted"]

        result = store.deduct_with_allowance("u1", Decimal("12"))
        assert result.error is None
        # "purchased" (still configured) drains FIRST; the orphaned "gifted"
        # balance is appended LAST and covers the remainder.
        assert result.tier_breakdown == {"purchased": Decimal("5"), "gifted": Decimal("7")}

        # get_credit_tiers only reports currently-configured tiers, but the
        # orphaned balance is still tracked internally (not lost) — the
        # aggregate total_balance still accounts for it correctly.
        tiers = store.get_credit_tiers("u1")
        assert {t.tier_key for t in tiers.tiers} == {"purchased"}
        assert store._tier_balances[("u1", "gifted")] == Decimal("3")  # 10 - 7
        assert tiers.total_balance == store.get_balance("u1").balance == Decimal("3")

        # Refund the full amount: LIFO restoration puts the orphaned tier
        # (drained last) FIRST, ahead of the still-configured "purchased".
        refund = store.refund_credits(result.transaction_id, amount=Decimal("12"))
        assert refund.error is None
        assert refund.tier_breakdown == {"gifted": Decimal("7"), "purchased": Decimal("5")}
        assert store._tier_balances[("u1", "gifted")] == Decimal("10")
        assert store.get_credit_tiers("u1").tiers[0].balance == Decimal("5")  # purchased restored too


# ── 2. No rounding drift ─────────────────────────────────────────────────────


class TestNoRoundingDrift:
    def test_fractional_tier_balances_sum_exactly_no_float_drift(self) -> None:
        store = _tiers_store(
            {
                "a": TierDefinition(name="A", priority=1),
                "b": TierDefinition(name="B", priority=2),
                "c": TierDefinition(name="C", priority=3, is_default=True),
            }
        )
        store.add_credits("u1", Decimal("0.3333"), "purchase", tier="a")
        store.add_credits("u1", Decimal("0.3333"), "purchase", tier="b")
        store.add_credits("u1", Decimal("0.3334"), "purchase", tier="c")

        net = Decimal("1.0000")
        result = store.deduct_with_allowance("u1", net)
        assert result.error is None
        assert result.tier_breakdown is not None
        assert sum(result.tier_breakdown.values(), Decimal(0)) == net
        assert result.tier_breakdown == {
            "a": Decimal("0.3333"),
            "b": Decimal("0.3333"),
            "c": Decimal("0.3334"),
        }
        # Exact Decimal equality — not "close enough" / float tolerance.
        assert all(isinstance(v, Decimal) for v in result.tier_breakdown.values())

    def test_many_fractional_grants_and_partial_deduct_sums_exactly(self) -> None:
        store = _tiers_store(
            {
                "a": TierDefinition(name="A", priority=1),
                "b": TierDefinition(name="B", priority=2),
                "c": TierDefinition(name="C", priority=3),
                "d": TierDefinition(name="D", priority=4, is_default=True),
            }
        )
        amounts = [Decimal("0.0001"), Decimal("0.0002"), Decimal("0.0003"), Decimal("0.0004")]
        for tier_key, amount in zip(("a", "b", "c", "d"), amounts, strict=True):
            store.add_credits("u1", amount, "purchase", tier=tier_key)

        net = Decimal("0.0007")  # spans a, b, c fully and part of d
        result = store.deduct_with_allowance("u1", net)
        assert result.tier_breakdown is not None
        assert sum(result.tier_breakdown.values(), Decimal(0)) == net
        assert result.tier_breakdown == {
            "a": Decimal("0.0001"),
            "b": Decimal("0.0002"),
            "c": Decimal("0.0003"),
            "d": Decimal("0.0001"),
        }


# ── 3. Concurrent deducts across tiers: conservation of money ───────────────


class TestConcurrentDeductsAcrossTiers:
    """MemoryStore serializes every mutating call under a single per-store
    RLock (contract §3, C2), so this is real thread concurrency (mirroring
    test_lease_adversarial.py's TestConcurrencyAdmission), not a simulated
    single-threaded interleaving. Regardless of scheduling, the outcome is
    deterministic here because each individual deduct always fully drains the
    lower-priority tier before touching the higher-priority one."""

    def test_conservation_of_money_under_concurrent_deducts(self) -> None:
        store = _tiers_store(
            {
                "gifted": TierDefinition(name="Gifted", priority=10),
                "purchased": TierDefinition(name="Purchased", priority=30, is_default=True),
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
        tiers = store.get_credit_tiers("u1")
        remaining = sum((t.balance for t in tiers.tiers), Decimal(0))

        # Conservation of money: nothing lost or double-spent under the race.
        assert remaining == total_granted - consumed
        assert tiers.total_balance == remaining
        assert store.get_balance("u1").balance == remaining

        # Priority ordering held even under concurrency: "gifted" (priority
        # 10) is fully drained before "purchased" is ever touched, regardless
        # of thread interleaving, since each call's tier walk is atomic.
        by_key = {t.tier_key: t.balance for t in tiers.tiers}
        assert by_key["gifted"] == Decimal("400")  # 1000 - 600
        assert by_key["purchased"] == Decimal("1000")  # untouched

    def test_concurrent_deducts_never_over_drain_a_tier(self) -> None:
        """Fuzz variant: interleaved deducts of varying size never push any
        single tier's balance negative when floor 0 legitimately blocks
        overspend (no allow_overdraft tier configured)."""
        store = _tiers_store(
            {
                "gifted": TierDefinition(name="Gifted", priority=10),
                "purchased": TierDefinition(name="Purchased", priority=30, is_default=True),
            }
        )
        store.add_credits("u1", Decimal("500"), "purchase", tier="gifted")
        store.add_credits("u1", Decimal("500"), "purchase", tier="purchased")

        def attempt(amount: Decimal) -> None:
            store.deduct_with_allowance("u1", amount, min_balance=Decimal(0))

        amounts = [Decimal(n % 7 + 1) for n in range(100)]  # 1..7, deterministic
        with ThreadPoolExecutor(max_workers=16) as ex:
            list(ex.map(attempt, amounts))

        tiers = store.get_credit_tiers("u1")
        for t in tiers.tiers:
            assert t.balance >= Decimal(0)
        total_consumed = Decimal(1000) - sum((t.balance for t in tiers.tiers), Decimal(0))
        assert total_consumed >= Decimal(0)
        assert store.get_balance("u1").balance == sum((t.balance for t in tiers.tiers), Decimal(0))


# ── 4. Team pools untouched by tier configuration ───────────────────────────


class TestTeamPoolsUntouchedByTiers:
    def test_deduct_team_ignores_tier_config_entirely(self) -> None:
        store = _tiers_store(
            {
                "gifted": TierDefinition(name="Gifted", priority=10),
                "purchased": TierDefinition(name="Purchased", priority=30, is_default=True),
            }
        )
        # The user's OWN tiered balance, separate from the team pool.
        store.add_credits("u1", Decimal("100"), "purchase", tier="gifted")
        team = store.create_team("Pool", Decimal("500"))
        store.add_team_member(team.team_id, "u1")

        result = store.deduct_team(team.team_id, "u1", Decimal("50"))
        assert result.error is None
        assert result.team_balance_after == Decimal("450")

        # Team deduction must not touch the user's own tiered balance at all.
        tiers = {t.tier_key: t.balance for t in store.get_credit_tiers("u1").tiers}
        assert tiers["gifted"] == Decimal("100")
        assert tiers["purchased"] == Decimal(0)
        assert store.get_balance("u1").balance == Decimal("100")

    def test_team_deduction_result_has_no_tier_fields(self) -> None:
        """Out of scope per the plan: TeamDeductionResult carries no tier
        concept at all (no tier_breakdown field)."""
        store = _tiers_store({"gifted": TierDefinition(name="Gifted", priority=10, is_default=True)})
        team = store.create_team("Pool", Decimal("100"))
        store.add_team_member(team.team_id, "u1")
        result = store.deduct_team(team.team_id, "u1", Decimal("10"))
        assert not hasattr(result, "tier_breakdown")


# ── 5. Multiple partial refunds across 3+ tiers ─────────────────────────────


class TestMultiTierPartialRefunds:
    def test_three_partial_refunds_across_four_tiers_never_over_restore(self) -> None:
        store = _tiers_store(
            {
                "a": TierDefinition(name="A", priority=10),
                "b": TierDefinition(name="B", priority=20),
                "c": TierDefinition(name="C", priority=30),
                "d": TierDefinition(name="D", priority=40, is_default=True),
            }
        )
        store.add_credits("u1", Decimal("10"), "purchase", tier="a")
        store.add_credits("u1", Decimal("10"), "purchase", tier="b")
        store.add_credits("u1", Decimal("10"), "purchase", tier="c")
        store.add_credits("u1", Decimal("10"), "purchase", tier="d")

        ded = store.deduct_with_allowance("u1", Decimal("35"))
        assert ded.tier_breakdown == {
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
        assert refunds[0].tier_breakdown == {"d": Decimal("5"), "c": Decimal("3")}
        assert refunds[1].tier_breakdown == {"c": Decimal("7"), "b": Decimal("5")}
        assert refunds[2].tier_breakdown == {"b": Decimal("5"), "a": Decimal("10")}

        combined: dict[str, Decimal] = {}
        for r in refunds:
            for tier_key, amount in (r.tier_breakdown or {}).items():
                combined[tier_key] = combined.get(tier_key, Decimal(0)) + amount

        # Invariant: no tier ever restored more than was originally drained.
        assert ded.tier_breakdown is not None
        for tier_key, drained in ded.tier_breakdown.items():
            assert combined.get(tier_key, Decimal(0)) <= drained

        # Fully refunded (8 + 12 + 15 == 35 == the original net charge).
        assert sum(combined.values(), Decimal(0)) == Decimal("35")
        assert combined == {"d": Decimal("5"), "c": Decimal("10"), "b": Decimal("10"), "a": Decimal("10")}

        tiers = {t.tier_key: t.balance for t in store.get_credit_tiers("u1").tiers}
        assert tiers == {"a": Decimal("10"), "b": Decimal("10"), "c": Decimal("10"), "d": Decimal("10")}

    def test_over_refund_across_tiers_still_rejected(self) -> None:
        """Even with multiple tiers involved, the aggregate over-refund guard
        still holds: total refunds can never exceed the original debit."""
        store = _tiers_store(
            {
                "a": TierDefinition(name="A", priority=10),
                "b": TierDefinition(name="B", priority=20, is_default=True),
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

        tiers = {t.tier_key: t.balance for t in store.get_credit_tiers("u1").tiers}
        assert tiers == {"a": Decimal("10"), "b": Decimal("10")}
