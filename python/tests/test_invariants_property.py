"""Property-based ledger invariants (P0.4), MemoryStore.

Replaces the fixed-seed fuzz loop that used to live in
``test_lease_adversarial.py::TestPropertyInvariant`` (a single hand-rolled
``random.Random(1729)`` sequence of 400 steps). A fixed seed only ever
explores the one sequence of operations it happened to draw; if that
sequence doesn't happen to hit a boundary condition, the bug it would have
caught goes undetected forever. Hypothesis's stateful testing explores many
different operation sequences across runs, and — critically — automatically
shrinks any failure to the smallest reproducing sequence, which a hand-rolled
loop cannot do at all (you'd be stuck reading a 400-step fixed trace).

The state machine below models the ledger across the two spending paths
(immediate ``deduct_with_allowance`` and the reserve/settle/release lease
lifecycle) plus refunds, and asserts the money invariants that must hold
after every single step:

  - ``balance`` matches an independently-tracked expected balance (ledger
    conservation: every grant/deduct/refund is accounted for exactly once).
  - ``balance`` never drops below the floor (0 strict / a negative overdraft
    floor).
  - ``available == balance - reserved`` (the sum of currently-active holds).
  - Idempotent replay of a deduction never double-charges.
  - Cumulative refunds against a transaction never exceed its original
    amount (over-refund is always rejected, never silently clamped).
  - Settle is de-clamped (bills the actual cost, never limited by the
    original hold), floor-clamped only to the account's floor.

Run twice with different floors/billing modes (see the two ``TestCase``
subclasses at the bottom) so both of bursar's financial-safety presets are
exercised, not just strict-prepaid.
"""

from __future__ import annotations

from decimal import Decimal

from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from bursar.interface.memory import MemoryStore

_USER = "u1"
_STARTING_BALANCE = Decimal(10_000)

# NUMERIC(18,4): every amount is exactly representable at 4 decimal places.
# NaN/Infinity are handled by the existing invalid-input tests, not here —
# this machine models the valid-input state space.
_AMOUNTS = st.decimals(min_value="0.0001", max_value="500", places=4, allow_nan=False, allow_infinity=False).map(
    Decimal
)


class LedgerInvariantMachine(RuleBasedStateMachine):
    """Shared rules; concrete subclasses fix ``billing_mode``/``floor``.

    ``floor`` is the account's minimum permitted balance — 0 for
    ``strict_prepaid`` (the default preset), a negative number for
    ``overdraft``. It's threaded through both entry points that enforce it:
    ``deduct_with_allowance(min_balance=...)`` (immediate path, all-or-
    nothing) and ``create_lease(floor=..., overdraft_floor=...)`` (lease
    admission; ``overdraft_floor`` is what ``settle_lease`` later reads back
    off the persisted lease to floor-clamp the de-clamped actual charge).
    """

    billing_mode: str = "strict"
    floor: Decimal = Decimal(0)
    starting_balance: Decimal = _STARTING_BALANCE

    def __init__(self) -> None:
        super().__init__()
        self.store = MemoryStore()
        self.store.add_credits(_USER, self.starting_balance)
        self.expected_balance = self.starting_balance
        # Keyed by our own sequential counters, NOT by the store's real
        # lease_id/transaction_id (which are uuid4() — real OS randomness,
        # not controlled by Hypothesis's PRNG). Sampling `sorted(dict_of_
        # uuids)` would pick a different *physical* hold/transaction across
        # what Hypothesis considers "identical" replays of the same
        # rule-call sequence (needed for shrinking), since the UUIDs'
        # alphabetical order has nothing to do with call order — Hypothesis
        # detects the resulting divergence as FlakyStrategyDefinition.
        # Sequential int keys are a pure function of the call sequence, so
        # replays are actually deterministic.
        self.open_holds: dict[int, tuple[str, Decimal]] = {}  # id -> (lease_id, hold amount)
        self.refundable: dict[int, tuple[str, Decimal]] = {}  # id -> (transaction_id, remaining)
        self.used_keys: list[str] = []
        self._next_id = 0

    def _fresh_key(self) -> str:
        self._next_id += 1
        return f"k{self._next_id}"

    # -- grant ---------------------------------------------------------

    @rule(amount=_AMOUNTS)
    def grant(self, amount: Decimal) -> None:
        self.store.add_credits(_USER, amount)
        self.expected_balance += amount

    # -- immediate deduct ------------------------------------------------

    @rule(amount=_AMOUNTS)
    def deduct(self, amount: Decimal) -> None:
        key = self._fresh_key()
        result = self.store.deduct_with_allowance(_USER, amount, idempotency_key=key, min_balance=self.floor)
        if result.error is not None:
            assert result.error == "insufficient_credits"
            # The rejection must be genuinely warranted — charging the full
            # amount would have breached the floor. Otherwise this masks a
            # "floor enforced too strictly" regression (e.g. the overdraft
            # floor silently ignored/clamped to 0) just as surely as a
            # too-loose floor would breach it; only checking the error CODE
            # (not whether rejecting was actually correct) would miss that.
            assert self.expected_balance - amount < self.floor, (
                f"deduct rejected but the floor ({self.floor}) would not have been "
                f"breached: balance={self.expected_balance} amount={amount}"
            )
            return
        self.used_keys.append(key)
        self.expected_balance -= result.amount
        if result.amount > 0:
            self._next_id += 1
            self.refundable[self._next_id] = (result.transaction_id, result.amount)

    @rule(data=st.data())
    def replay_deduct(self, data: st.DataObject) -> None:
        """Re-submitting a used idempotency key must never double-charge."""
        if not self.used_keys:
            return
        key = data.draw(st.sampled_from(self.used_keys))
        before = self.expected_balance
        result = self.store.deduct_with_allowance(_USER, Decimal("1"), idempotency_key=key, min_balance=self.floor)
        assert result.idempotent is True
        assert self.expected_balance == before  # replay must not touch the ledger

    # -- lease lifecycle -------------------------------------------------

    @rule(amount=_AMOUNTS)
    def reserve(self, amount: Decimal) -> None:
        lease = self.store.create_lease(
            _USER,
            amount,
            "usage",
            billing_mode=self.billing_mode,
            floor=self.floor,
            overdraft_floor=self.floor if self.billing_mode == "overdraft" else None,
        )
        if lease.error is not None:
            assert lease.error == "insufficient_credits"
            # Same "rejection must be warranted" check as `deduct` (admission
            # also accounts for currently-reserved holds).
            reserved = sum((hold for _lease_id, hold in self.open_holds.values()), Decimal(0))
            available = self.expected_balance - reserved
            assert available - amount < self.floor, (
                f"reserve rejected but the floor ({self.floor}) would not have been "
                f"breached: available={available} amount={amount}"
            )
            return
        self._next_id += 1
        self.open_holds[self._next_id] = (lease.lease_id, amount)

    @rule(data=st.data())
    def settle(self, data: st.DataObject) -> None:
        if not self.open_holds:
            return
        hold_id = data.draw(st.sampled_from(sorted(self.open_holds)))
        lease_id, hold = self.open_holds.pop(hold_id)
        # Settle is de-clamped (interface plan D5): it bills the ACTUAL cost,
        # never limited by the lease's original hold. Draw `actual` from a
        # range that regularly exceeds `hold` (up to 3x + 1) so this state
        # machine exercises that de-clamp path, not just actual <= hold.
        actual = data.draw(st.decimals(min_value="0", max_value=str(hold * 3 + 1), places=4).map(Decimal))
        result = self.store.settle_lease(_USER, lease_id, actual)
        assert result.error is None
        # The store floor-clamps net to the account's floor (never to
        # `hold`). Assert the exact charged amount, not just "no error", so
        # an accidental clamp-to-hold regression would be caught.
        expected_net = min(actual, max(Decimal(0), self.expected_balance - self.floor))
        assert result.amount == expected_net, (
            f"settle not de-clamped correctly: actual={actual} hold={hold} got={result.amount} expected={expected_net}"
        )
        self.expected_balance -= expected_net
        if expected_net > 0:
            self._next_id += 1
            self.refundable[self._next_id] = (result.transaction_id, expected_net)

    @rule(data=st.data())
    def release(self, data: st.DataObject) -> None:
        if not self.open_holds:
            return
        hold_id = data.draw(st.sampled_from(sorted(self.open_holds)))
        lease_id, _hold = self.open_holds.pop(hold_id)
        released = self.store.release_lease(_USER, lease_id)
        assert released.released is True

    # -- refunds -----------------------------------------------------------

    @rule(data=st.data())
    def refund(self, data: st.DataObject) -> None:
        candidates = [k for k, (_tx, amt) in self.refundable.items() if amt > 0]
        if not candidates:
            return
        ref_id = data.draw(st.sampled_from(sorted(candidates)))
        tx_id, remaining = self.refundable[ref_id]
        amount = data.draw(st.decimals(min_value="0.0001", max_value=str(remaining), places=4).map(Decimal))
        result = self.store.refund_credits(tx_id, amount=amount)
        assert result.error is None
        self.expected_balance += amount
        self.refundable[ref_id] = (tx_id, remaining - amount)

    @rule(data=st.data())
    def over_refund_is_rejected(self, data: st.DataObject) -> None:
        candidates = [k for k, (_tx, amt) in self.refundable.items() if amt > 0]
        if not candidates:
            return
        ref_id = data.draw(st.sampled_from(sorted(candidates)))
        tx_id, remaining = self.refundable[ref_id]
        before = self.expected_balance
        result = self.store.refund_credits(tx_id, amount=remaining + Decimal("1000"))
        assert result.error == "over_refund"
        assert self.expected_balance == before  # rejected attempt must not touch the ledger

    # -- invariants, checked after every rule above ----------------------

    @invariant()
    def ledger_is_conserved_and_never_below_floor(self) -> None:
        avail = self.store.get_available(_USER)
        assert avail.balance == self.expected_balance, (
            f"balance drifted from the ledger: store={avail.balance} expected={self.expected_balance}"
        )
        assert avail.balance >= self.floor, f"balance went below the floor ({self.floor}): {avail.balance}"

        expected_reserved = sum((hold for _lease_id, hold in self.open_holds.values()), Decimal(0))
        assert avail.reserved == expected_reserved
        assert avail.available == avail.balance - avail.reserved


class OverdraftLedgerInvariantMachine(LedgerInvariantMachine):
    """Same rules, run against the ``overdraft`` preset (negative floor).

    A small starting balance (vs. the strict variant's 10,000) so deduct/
    settle amounts (up to 500 each) regularly push the balance past zero and
    toward the floor — with the large starting balance, 40 steps essentially
    never touches negative territory at all, so the overdraft-specific floor
    behavior would go completely unexercised.
    """

    billing_mode = "overdraft"
    floor = Decimal(-500)
    starting_balance = Decimal(50)


# 250 examples (not the Hypothesis default of 100): with 8 competing rules,
# some scenarios only fire when both "settle is selected" AND "a specific
# condition holds" (e.g. actual > hold, to exercise the de-clamp path) —
# verified by injecting a real clamp-to-hold regression into
# MemoryStore.settle_lease and confirming 100 examples missed it in 1/1 try
# while 250 caught it in 3/3 tries. Still bounded so CI stays fast (~1.2s
# here) and still explores far more of the state space across many distinct
# random sequences than the single fixed-seed 400-step loop this replaces —
# and, unlike that loop, automatically shrinks any failure to a minimal
# reproducing sequence instead of leaving a 400-step trace to debug by hand.
_SETTINGS = settings(
    max_examples=250,
    stateful_step_count=40,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large, HealthCheck.filter_too_much],
)
LedgerInvariantMachine.TestCase.settings = _SETTINGS
OverdraftLedgerInvariantMachine.TestCase.settings = _SETTINGS
TestLedgerInvariant = LedgerInvariantMachine.TestCase
TestLedgerInvariantOverdraft = OverdraftLedgerInvariantMachine.TestCase
