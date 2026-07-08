/**
 * Property-based ledger invariants (P0.4), MemoryStore.
 *
 * Mirror of `python/tests/test_invariants_property.py` — see that file's
 * module docstring for the full rationale. In short: this replaces the
 * fixed-seed deterministic-LCG fuzz loop that used to live in
 * `lease-adversarial.test.ts` ("randomized ledger invariant") with
 * fast-check's model-based/stateful testing. A fixed seed only ever explores
 * the one sequence it happened to draw; fast-check explores many distinct
 * sequences across runs and — critically — shrinks any failure to a minimal
 * reproducing sequence, which a hand-rolled loop cannot do.
 *
 * The model below covers both spending paths (immediate
 * `deductWithAllowance` and the reserve/settle/release lease lifecycle) plus
 * refunds, asserting after every command:
 *   - `balance` matches an independently-tracked expected balance (ledger
 *     conservation).
 *   - `balance` never drops below the floor (0 strict / a negative overdraft
 *     floor).
 *   - `available == balance - reserved`.
 *   - Idempotent replay never double-charges.
 *   - Over-refund is always rejected, never silently clamped.
 *   - Settle is de-clamped (bills the actual cost, never limited by the
 *     original hold), floor-clamped only to the account's floor.
 *   - A rejection (insufficient_credits) is only ever returned when it's
 *     genuinely warranted by the floor — not just "any rejection is fine".
 *
 * Run twice with different floors/billing modes (see the two `it()` blocks
 * at the bottom) so both of bursar's financial-safety presets are exercised,
 * not just strict-prepaid.
 */
import { describe, it } from "vitest";
import fc from "fast-check";
import Decimal from "decimal.js";
import { MemoryStore } from "../src/stores/memory-store.js";

const D = (n: number | string) => new Decimal(n);
const USER = "u1";

// NUMERIC(18,4): every amount is exactly representable at 4 decimal places.
// Generated as an integer of 0.0001-units so it's always exact, matching the
// Python side's `st.decimals(places=4)`.
const AMOUNT_ARB: fc.Arbitrary<Decimal> = fc
  .integer({ min: 1, max: 5_000_000 })
  .map((n) => D(n).div(10_000));
// Refund amounts are capped at 1x the remaining refundable balance — a real
// business constraint, not something to de-clamp.
const REFUND_FRACTION_ARB: fc.Arbitrary<number> = fc.float({ min: 0, max: 1, noNaN: true });
// Settle is de-clamped (interface plan D5): it bills the ACTUAL cost, never
// limited by the lease's original hold. This multiplier regularly exceeds 1
// so SettleCommand exercises that de-clamp path, not just actual <= hold.
const SETTLE_MULTIPLIER_ARB: fc.Arbitrary<number> = fc.float({ min: 0, max: 3, noNaN: true });

interface Model {
  expectedBalance: Decimal;
  // Minimum permitted balance — 0 for strict_prepaid, negative for
  // overdraft. Threaded through both entry points that enforce it:
  // deductWithAllowance's minBalance (immediate path, all-or-nothing) and
  // createLease's floor/overdraftFloor (lease admission; overdraftFloor is
  // what settleLease later reads back off the persisted lease to
  // floor-clamp the de-clamped actual charge).
  floor: Decimal;
  billingMode: "strict" | "overdraft";
  // Keyed by our own sequential counter, not the store's real lease_id/
  // transaction_id (uuid-based) — see the Python file for why that matters
  // (there it causes real flakiness; here fast-check replays fixed command
  // objects rather than re-deriving values from a byte stream, so it's not
  // strictly required for correctness, but keeping the two suites' modeling
  // approach aligned makes them easier to maintain as a pair).
  openHolds: Map<number, { leaseId: string; hold: Decimal }>;
  refundable: Map<number, { txId: string; remaining: Decimal }>;
  usedKeys: string[];
  nextId: number;
}

interface Real {
  store: MemoryStore;
}

async function assertInvariant(m: Model, r: Real): Promise<void> {
  const avail = await r.store.getAvailable(USER);
  if (!avail.balance.eq(m.expectedBalance)) {
    throw new Error(
      `balance drifted from the ledger: store=${avail.balance} expected=${m.expectedBalance}`,
    );
  }
  if (avail.balance.lt(m.floor)) {
    throw new Error(`balance went below the floor (${m.floor}): ${avail.balance}`);
  }
  const expectedReserved = [...m.openHolds.values()].reduce((sum, h) => sum.plus(h.hold), D(0));
  if (!avail.reserved.eq(expectedReserved)) {
    throw new Error(`reserved mismatch: store=${avail.reserved} expected=${expectedReserved}`);
  }
  if (!avail.available.eq(avail.balance.minus(avail.reserved))) {
    throw new Error(
      `available (${avail.available}) != balance - reserved (${avail.balance.minus(avail.reserved)})`,
    );
  }
}

function freshKey(m: Model): string {
  m.nextId += 1;
  return `k${m.nextId}`;
}

class GrantCommand implements fc.AsyncCommand<Model, Real> {
  constructor(private readonly amount: Decimal) {}
  check(): boolean {
    return true;
  }
  async run(m: Model, r: Real): Promise<void> {
    await r.store.addCredits(USER, this.amount);
    m.expectedBalance = m.expectedBalance.plus(this.amount);
    await assertInvariant(m, r);
  }
  toString(): string {
    return `Grant(${this.amount})`;
  }
}

class DeductCommand implements fc.AsyncCommand<Model, Real> {
  constructor(private readonly amount: Decimal) {}
  check(): boolean {
    return true;
  }
  async run(m: Model, r: Real): Promise<void> {
    const key = freshKey(m);
    const result = await r.store.deductWithAllowance(USER, this.amount, {
      idempotencyKey: key,
      minBalance: m.floor,
    });
    if (result.error) {
      if (result.error !== "insufficient_credits") {
        throw new Error(`unexpected deduct error: ${result.error}`);
      }
      // The rejection must be genuinely warranted — charging the full
      // amount would have breached the floor. Otherwise this masks a
      // "floor enforced too strictly" regression (e.g. the overdraft floor
      // silently ignored/clamped to 0) just as surely as a too-loose floor
      // would breach it; only checking the error CODE (not whether
      // rejecting was actually correct) would miss that.
      if (!m.expectedBalance.minus(this.amount).lt(m.floor)) {
        throw new Error(
          `deduct rejected but the floor (${m.floor}) would not have been breached: ` +
            `balance=${m.expectedBalance} amount=${this.amount}`,
        );
      }
    } else {
      m.usedKeys.push(key);
      m.expectedBalance = m.expectedBalance.minus(result.amount);
      if (result.amount.gt(0)) {
        m.nextId += 1;
        m.refundable.set(m.nextId, { txId: result.transactionId, remaining: result.amount });
      }
    }
    await assertInvariant(m, r);
  }
  toString(): string {
    return `Deduct(${this.amount})`;
  }
}

class ReplayDeductCommand implements fc.AsyncCommand<Model, Real> {
  constructor(private readonly keyIndex: number) {}
  check(m: Readonly<Model>): boolean {
    return m.usedKeys.length > 0;
  }
  async run(m: Model, r: Real): Promise<void> {
    const key = m.usedKeys[this.keyIndex % m.usedKeys.length];
    const before = m.expectedBalance;
    const result = await r.store.deductWithAllowance(USER, D(1), {
      idempotencyKey: key,
      minBalance: m.floor,
    });
    if (result.idempotent !== true) {
      throw new Error(
        `replay of a used idempotency key was not flagged idempotent: ${JSON.stringify(result)}`,
      );
    }
    if (!m.expectedBalance.eq(before)) {
      throw new Error("replay must not touch the ledger");
    }
    await assertInvariant(m, r);
  }
  toString(): string {
    return `ReplayDeduct(#${this.keyIndex})`;
  }
}

class ReserveCommand implements fc.AsyncCommand<Model, Real> {
  constructor(private readonly amount: Decimal) {}
  check(): boolean {
    return true;
  }
  async run(m: Model, r: Real): Promise<void> {
    const lease = await r.store.createLease(USER, this.amount, "usage", {
      billingMode: m.billingMode,
      floor: m.floor,
      overdraftFloor: m.billingMode === "overdraft" ? m.floor : undefined,
    });
    if (lease.error) {
      if (lease.error !== "insufficient_credits") {
        throw new Error(`unexpected reserve error: ${lease.error}`);
      }
      // Same "rejection must be warranted" check as DeductCommand
      // (admission also accounts for currently-reserved holds).
      const reserved = [...m.openHolds.values()].reduce((sum, h) => sum.plus(h.hold), D(0));
      const available = m.expectedBalance.minus(reserved);
      if (!available.minus(this.amount).lt(m.floor)) {
        throw new Error(
          `reserve rejected but the floor (${m.floor}) would not have been breached: ` +
            `available=${available} amount=${this.amount}`,
        );
      }
    } else {
      m.nextId += 1;
      m.openHolds.set(m.nextId, { leaseId: lease.leaseId, hold: this.amount });
    }
    await assertInvariant(m, r);
  }
  toString(): string {
    return `Reserve(${this.amount})`;
  }
}

class SettleCommand implements fc.AsyncCommand<Model, Real> {
  constructor(
    private readonly holdIndex: number,
    private readonly fraction: number,
  ) {}
  check(m: Readonly<Model>): boolean {
    return m.openHolds.size > 0;
  }
  async run(m: Model, r: Real): Promise<void> {
    const keys = [...m.openHolds.keys()].sort((a, b) => a - b);
    const key = keys[this.holdIndex % keys.length];
    const { leaseId, hold } = m.openHolds.get(key)!;
    m.openHolds.delete(key);
    // `fraction` ranges over [0, 3] (see SETTLE_MULTIPLIER_ARB) so `actual`
    // regularly exceeds `hold`, exercising the de-clamp path.
    const actual = hold.times(this.fraction).toDecimalPlaces(4, Decimal.ROUND_DOWN);
    const result = await r.store.settleLease(USER, leaseId, actual);
    if (result.error) {
      throw new Error(`settle failed unexpectedly: ${result.error}`);
    }
    // The store floor-clamps net to the account's floor (never to `hold`).
    // Assert the exact charged amount, not just "no error", so an
    // accidental clamp-to-hold regression would be caught.
    const expectedNet = Decimal.min(actual, Decimal.max(m.expectedBalance.minus(m.floor), 0));
    if (!result.amount.eq(expectedNet)) {
      throw new Error(
        `settle not de-clamped correctly: actual=${actual} hold=${hold} got=${result.amount} expected=${expectedNet}`,
      );
    }
    m.expectedBalance = m.expectedBalance.minus(expectedNet);
    if (expectedNet.gt(0)) {
      m.nextId += 1;
      m.refundable.set(m.nextId, { txId: result.transactionId, remaining: expectedNet });
    }
    await assertInvariant(m, r);
  }
  toString(): string {
    return `Settle(#${this.holdIndex}, mult=${this.fraction.toFixed(3)})`;
  }
}

class ReleaseCommand implements fc.AsyncCommand<Model, Real> {
  constructor(private readonly holdIndex: number) {}
  check(m: Readonly<Model>): boolean {
    return m.openHolds.size > 0;
  }
  async run(m: Model, r: Real): Promise<void> {
    const keys = [...m.openHolds.keys()].sort((a, b) => a - b);
    const key = keys[this.holdIndex % keys.length];
    const { leaseId } = m.openHolds.get(key)!;
    m.openHolds.delete(key);
    const released = await r.store.releaseLease(USER, leaseId);
    if (released.released !== true) {
      throw new Error(
        `release of an active hold did not report released: ${JSON.stringify(released)}`,
      );
    }
    await assertInvariant(m, r);
  }
  toString(): string {
    return `Release(#${this.holdIndex})`;
  }
}

class RefundCommand implements fc.AsyncCommand<Model, Real> {
  constructor(
    private readonly refundIndex: number,
    private readonly fraction: number,
  ) {}
  check(m: Readonly<Model>): boolean {
    return [...m.refundable.values()].some((v) => v.remaining.gt(0));
  }
  async run(m: Model, r: Real): Promise<void> {
    const keys = [...m.refundable.keys()]
      .filter((k) => m.refundable.get(k)!.remaining.gt(0))
      .sort((a, b) => a - b);
    const key = keys[this.refundIndex % keys.length];
    const { txId, remaining } = m.refundable.get(key)!;
    // Keep amount > 0: refundCredits treats amount=0 as "full refund" (the
    // sentinel for "unspecified"), which would refund `remaining` in full
    // regardless of the fraction drawn — not what this command models.
    const amount = Decimal.max(remaining.times(this.fraction), D("0.0001")).toDecimalPlaces(
      4,
      Decimal.ROUND_DOWN,
    );
    const clamped = Decimal.min(amount, remaining);
    const result = await r.store.refundCredits(txId, clamped);
    if (result.error) {
      throw new Error(`refund failed unexpectedly: ${result.error}`);
    }
    m.expectedBalance = m.expectedBalance.plus(clamped);
    m.refundable.set(key, { txId, remaining: remaining.minus(clamped) });
    await assertInvariant(m, r);
  }
  toString(): string {
    return `Refund(#${this.refundIndex}, frac=${this.fraction.toFixed(3)})`;
  }
}

class OverRefundIsRejectedCommand implements fc.AsyncCommand<Model, Real> {
  constructor(private readonly refundIndex: number) {}
  check(m: Readonly<Model>): boolean {
    return [...m.refundable.values()].some((v) => v.remaining.gt(0));
  }
  async run(m: Model, r: Real): Promise<void> {
    const keys = [...m.refundable.keys()]
      .filter((k) => m.refundable.get(k)!.remaining.gt(0))
      .sort((a, b) => a - b);
    const key = keys[this.refundIndex % keys.length];
    const { txId, remaining } = m.refundable.get(key)!;
    const before = m.expectedBalance;
    const result = await r.store.refundCredits(txId, remaining.plus(1000));
    if (result.error !== "over_refund") {
      throw new Error(`expected over_refund, got: ${JSON.stringify(result)}`);
    }
    if (!m.expectedBalance.eq(before)) {
      throw new Error("rejected over-refund must not touch the ledger");
    }
    await assertInvariant(m, r);
  }
  toString(): string {
    return `OverRefundIsRejected(#${this.refundIndex})`;
  }
}

const allCommands = [
  AMOUNT_ARB.map((a) => new GrantCommand(a)),
  AMOUNT_ARB.map((a) => new DeductCommand(a)),
  fc.nat().map((i) => new ReplayDeductCommand(i)),
  AMOUNT_ARB.map((a) => new ReserveCommand(a)),
  fc.tuple(fc.nat(), SETTLE_MULTIPLIER_ARB).map(([i, f]) => new SettleCommand(i, f)),
  fc.nat().map((i) => new ReleaseCommand(i)),
  fc.tuple(fc.nat(), REFUND_FRACTION_ARB).map(([i, f]) => new RefundCommand(i, f)),
  fc.nat().map((i) => new OverRefundIsRejectedCommand(i)),
];

async function runLedgerInvariantProperty(config: {
  billingMode: "strict" | "overdraft";
  floor: Decimal;
  startingBalance: Decimal;
}): Promise<void> {
  await fc.assert(
    fc.asyncProperty(fc.commands(allCommands, { size: "medium" }), async (cmds) => {
      const setup = async () => {
        const store = new MemoryStore();
        await store.addCredits(USER, config.startingBalance);
        const model: Model = {
          expectedBalance: config.startingBalance,
          floor: config.floor,
          billingMode: config.billingMode,
          openHolds: new Map(),
          refundable: new Map(),
          usedKeys: [],
          nextId: 0,
        };
        return { model, real: { store } };
      };
      await fc.asyncModelRun(setup, cmds);
    }),
    { numRuns: 100 },
  );
}

describe("Ledger invariants (property-based, fast-check)", () => {
  it("conserves the ledger and never goes below the floor — strict_prepaid", async () => {
    await runLedgerInvariantProperty({
      billingMode: "strict",
      floor: D(0),
      startingBalance: D(10_000),
    });
  });

  // Small starting balance (vs. the strict case's 10,000) so deduct/settle
  // amounts (up to 500 each) regularly push the balance past zero and toward
  // the floor — with a large starting balance this essentially never touches
  // negative territory, so the overdraft-specific floor behavior would go
  // completely unexercised.
  it("conserves the ledger and never goes below the floor — overdraft", async () => {
    await runLedgerInvariantProperty({
      billingMode: "overdraft",
      floor: D(-500),
      startingBalance: D(50),
    });
  });
});
