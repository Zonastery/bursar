/**
 * Adversarial / financial-safety tests for the lease lifecycle (MemoryStore).
 *
 * Mirror of the Python `test_lease_adversarial.py` suite (parity is a hard
 * project invariant for this financial module). It attacks the money invariants
 * from every angle a billing system must survive: concurrency races (no
 * over-admission, no double-charge), exact Decimal precision (no binary-float
 * drift), invalid inputs, floor-boundary exactness, the full lease state
 * machine, user-scoped idempotency-key collisions, allowance consumption at
 * settle, and advisory spend caps at settle.
 *
 * The central invariant under test (interface plan, Guarantees §1/§2): in strict
 * mode, `balance` never drops below the floor, and the sum of active holds never
 * exceeds `balance − floor` — even under arbitrary concurrency — because
 * admission is a single atomic lease.
 *
 * Concurrency note: JS is single-threaded and the in-memory store has no `await`
 * inside its critical sections, so `Promise.all([...])` of store calls runs
 * sequentially (no true preemption). We still mirror the Python intent — fire
 * many calls "concurrently" and assert exactly the right number are admitted and
 * that no invariant (held ≤ balance, single debit) is ever violated.
 */

import { describe, it, expect, beforeEach } from "vitest";
import Decimal from "decimal.js";
import { CreditManager } from "../src/manager.js";
import type { CreditManagerOptions } from "../src/manager.js";
import { MemoryStore } from "../src/stores/memory-store.js";
import { CreditEventEmitter } from "../src/stores/events.js";
import type { CreditEvent } from "../src/stores/events.js";
import { CapReachedError, ConcurrencyLimitError, InsufficientCreditsError } from "../src/errors.js";

const D = (n: number | string) => new Decimal(n);

/** A manager with a default `input_tokens * 1` model and a configurable floor. */
async function manager(
  store: MemoryStore,
  minBalance: Decimal = D(0),
  options?: CreditManagerOptions,
): Promise<CreditManager> {
  const m = new CreditManager(store, undefined, undefined, options);
  await m.publishPricingFromDict({
    version: 1,
    metering: { models: { "*": "input_tokens * 1" } },
    ledger: { minBalance: minBalance.toNumber() },
  });
  return m;
}

/**
 * Deterministic, manually-advanced clock wired into `MemoryStore.setClock()`
 * — advances lease expiry without a real sleep and without reaching into
 * private reservation state (mirrors Python's `_FakeClock`).
 */
function fakeClock(start: Date): { advance: (ms: number) => void; fn: () => Date } {
  let now = start;
  return {
    advance: (ms: number) => {
      now = new Date(now.getTime() + ms);
    },
    fn: () => now,
  };
}

// ── Concurrency: atomic admission never over-admits ────────────────────────

describe("concurrency admission", () => {
  let store: MemoryStore;
  beforeEach(() => {
    store = new MemoryStore();
  });

  it("no over-admission under a storm of leases", async () => {
    await store.addCredits("u1", D(100)); // floor 0 ⇒ at most 3 holds of 30
    // Event-loop serialization: these resolve sequentially, but the assertion
    // (exactly 3 admitted, held ≤ balance) is identical in spirit to Python's
    // thread-storm — the atomic single-lease admission is what guarantees it.
    const results = await Promise.all(
      Array.from({ length: 40 }, () => store.createLease("u1", D(30), "usage", { floor: D(0) })),
    );

    const successes = results.filter((r) => !r.error);
    expect(successes).toHaveLength(3); // 3*30 = 90 ≤ 100; a 4th (120) breaches floor 0
    const avail = await store.getAvailable("u1");
    expect(avail.reserved.eq(D(90))).toBe(true);
    expect(avail.available.eq(D(10))).toBe(true);
    expect(avail.balance.eq(D(100))).toBe(true); // nothing charged yet — only held
  });

  it("maxConcurrent caps the count of holds", async () => {
    await store.addCredits("u1", D(10_000)); // plenty of balance; cap is on COUNT
    const results = await Promise.all(
      Array.from({ length: 40 }, () =>
        store.createLease("u1", D(1), "chat", { floor: D(0), maxConcurrent: 5 }),
      ),
    );

    expect(results.filter((r) => !r.error)).toHaveLength(5);
    expect(results.every((r) => r.error === undefined || r.error === "concurrency_limit")).toBe(
      true,
    );
  });

  it("concurrent settle with the same idempotency key charges once", async () => {
    await store.addCredits("u1", D(100));
    const lease = await store.createLease("u1", D(50), "usage", { floor: D(0) });
    // Many settles of the same lease + key — exactly one debit of 50, no double-charge.
    await Promise.all(
      Array.from({ length: 12 }, () =>
        store.settleLease("u1", lease.leaseId, D(50), { idempotencyKey: "k" }),
      ),
    );
    expect((await store.getBalance("u1")).balance.eq(D(50))).toBe(true);
  });

  it("pipeline invariant: balance never below the floor", async () => {
    await store.addCredits("u1", D(1000));
    // 50 leases of 20 (= 1000 exactly ⇒ all admit), each settling the actual 7.
    await Promise.all(
      Array.from({ length: 50 }, async () => {
        const lease = await store.createLease("u1", D(20), "usage", { floor: D(0) });
        expect(lease.error).toBeUndefined();
        await store.settleLease("u1", lease.leaseId, D(7));
      }),
    );

    const bal = await store.getBalance("u1");
    expect(bal.balance.eq(D(1000).minus(D(50).times(D(7))))).toBe(true); // 650, exact
    expect(bal.balance.gte(D(0))).toBe(true);
    expect((await store.getAvailable("u1")).reserved.eq(D(0))).toBe(true);
  });
});

// ── Idempotency-key collisions (user-scoped) ───────────────────────────────

describe("idempotency", () => {
  let store: MemoryStore;
  beforeEach(() => {
    store = new MemoryStore();
  });

  it("the same key across two leases charges once", async () => {
    await store.addCredits("u1", D(200));
    const l1 = await store.createLease("u1", D(50), "usage", { floor: D(0) });
    const l2 = await store.createLease("u1", D(50), "usage", { floor: D(0) });

    const d1 = await store.settleLease("u1", l1.leaseId, D(50), { idempotencyKey: "dup" });
    const d2 = await store.settleLease("u1", l2.leaseId, D(50), { idempotencyKey: "dup" });
    // The second settle replays the first's result — the shared pool is debited once.
    expect(d2.idempotent).toBe(true);
    expect(d2.transactionId).toBe(d1.transactionId);
    expect((await store.getBalance("u1")).balance.eq(D(150))).toBe(true);
  });

  it("re-settle with a different amount replays the original", async () => {
    await store.addCredits("u1", D(100));
    const lease = await store.createLease("u1", D(50), "usage", { floor: D(0) });
    const first = await store.settleLease("u1", lease.leaseId, D(20));
    // A re-settle (even with a different amount) must NOT charge again.
    const second = await store.settleLease("u1", lease.leaseId, D(999));
    expect(second.idempotent).toBe(true);
    expect(second.amount.eq(first.amount)).toBe(true);
    expect((await store.getBalance("u1")).balance.eq(D(80))).toBe(true);
  });
});

// ── Exact Decimal precision (no binary-float drift) ────────────────────────

describe("precision", () => {
  let store: MemoryStore;
  beforeEach(() => {
    store = new MemoryStore();
  });

  it("fractional reserve + settle is exact", async () => {
    await store.addCredits("u1", D(1));
    for (let i = 0; i < 3; i++) {
      const lease = await store.createLease("u1", D("0.0001"), "usage", { floor: D(0) });
      expect(lease.error).toBeUndefined();
      await store.settleLease("u1", lease.leaseId, D("0.0001"));
    }
    expect((await store.getBalance("u1")).balance.eq(D("0.9997"))).toBe(true);
  });

  it("settle smaller than a fractional hold", async () => {
    await store.addCredits("u1", D("10.5"));
    const lease = await store.createLease("u1", D("3.3333"), "usage", { floor: D(0) });
    const ded = await store.settleLease("u1", lease.leaseId, D("1.1111"));
    expect(ded.balanceAfter.eq(D("9.3889"))).toBe(true);
  });
});

// ── Invalid inputs ─────────────────────────────────────────────────────────

describe("invalid inputs", () => {
  let store: MemoryStore;
  beforeEach(() => {
    store = new MemoryStore();
  });

  // NaN / Infinity arrive as non-finite Decimals; 0 / negative as out-of-range.
  const badCreate: Array<[string, Decimal]> = [
    ["zero", D(0)],
    ["negative", D(-5)],
    ["NaN", new Decimal(NaN)],
    ["Infinity", new Decimal(Infinity)],
  ];
  it.each(badCreate)("createLease rejects %s amount", async (_label, bad) => {
    await store.addCredits("u1", D(100));
    expect((await store.createLease("u1", bad, "usage", { floor: D(0) })).error).toBe(
      "invalid_amount",
    );
  });

  const badSettle: Array<[string, Decimal]> = [
    ["negative", D(-1)],
    ["NaN", new Decimal(NaN)],
    ["-Infinity", new Decimal(-Infinity)],
  ];
  it.each(badSettle)("settleLease rejects %s amount", async (_label, bad) => {
    await store.addCredits("u1", D(100));
    const lease = await store.createLease("u1", D(20), "usage", { floor: D(0) });
    expect((await store.settleLease("u1", lease.leaseId, bad)).error).toBe("invalid_amount");
  });

  it("manager.reserve(0) raises (RangeError, JS analogue of Python ValueError)", async () => {
    const m = await manager(store);
    await store.addCredits("u1", D(100));
    await expect(m.reserve("u1", D(0))).rejects.toThrow(RangeError);
  });
});

// ── Floor-boundary exactness ───────────────────────────────────────────────

describe("floor boundary", () => {
  let store: MemoryStore;
  beforeEach(() => {
    store = new MemoryStore();
  });

  it("strict floor is inclusive (available − amount == floor is allowed)", async () => {
    await store.addCredits("u1", D(100));
    expect((await store.createLease("u1", D(95), "usage", { floor: D(5) })).error).toBeUndefined();
  });

  it("strict floor just-below is rejected", async () => {
    await store.addCredits("u2", D(100));
    // available − amount == 4 < floor 5 → rejected.
    expect((await store.createLease("u2", D(96), "usage", { floor: D(5) })).error).toBe(
      "insufficient_credits",
    );
  });

  it("overdraft floor boundary is exact", async () => {
    await store.addCredits("u1", D(0));
    // 0 − 50 == −50 == floor → allowed.
    const ok = await store.createLease("u1", D(50), "usage", {
      billingMode: "overdraft",
      floor: D(-50),
    });
    expect(ok.error).toBeUndefined();
    // A fresh hold of 1 more would be −51 < −50 → rejected.
    const bad = await store.createLease("u1", D(1), "usage", {
      billingMode: "overdraft",
      floor: D(-50),
    });
    expect(bad.error).toBe("insufficient_credits");
  });
});

// ── Lease state machine exhaustiveness ─────────────────────────────────────

describe("state machine", () => {
  let store: MemoryStore;
  beforeEach(() => {
    store = new MemoryStore();
  });

  it("renew after settle is not_found", async () => {
    await store.addCredits("u1", D(100));
    const lease = await store.createLease("u1", D(20), "usage", { floor: D(0) });
    await store.settleLease("u1", lease.leaseId, D(20));
    expect((await store.renewLease("u1", lease.leaseId, 600)).error).toBe("lease_not_found");
  });

  it("renew after release is not_found", async () => {
    await store.addCredits("u1", D(100));
    const lease = await store.createLease("u1", D(20), "usage", { floor: D(0) });
    await store.releaseLease("u1", lease.leaseId);
    expect((await store.renewLease("u1", lease.leaseId, 600)).error).toBe("lease_not_found");
  });

  it("an expired lease can still be released", async () => {
    const clock = fakeClock(new Date("2024-01-01T00:00:00Z"));
    store.setClock(clock.fn);
    await store.addCredits("u1", D(100));
    const lease = await store.createLease("u1", D(20), "usage", { floor: D(0), ttlSeconds: 1 });
    // Advance the fake clock past the lease's TTL — deterministic, no real
    // sleep and no reaching into private reservation state.
    clock.advance(2000);
    const r = await store.releaseLease("u1", lease.leaseId);
    expect(r.released).toBe(true);
  });

  it("another user's lease is not_found", async () => {
    await store.addCredits("u1", D(100));
    const lease = await store.createLease("u1", D(20), "usage", { floor: D(0) });
    expect((await store.settleLease("u2", lease.leaseId, D(20))).error).toBe("lease_not_found");
    expect((await store.releaseLease("u2", lease.leaseId)).reason).toBe("not_found");
  });

  it("getAvailable ignores non-active holds", async () => {
    await store.addCredits("u1", D(100));
    const settled = await store.createLease("u1", D(10), "usage", { floor: D(0) });
    const released = await store.createLease("u1", D(10), "usage", { floor: D(0) });
    const active = await store.createLease("u1", D(10), "usage", { floor: D(0) });
    await store.settleLease("u1", settled.leaseId, D(10));
    await store.releaseLease("u1", released.leaseId);
    const avail = await store.getAvailable("u1");
    // Only the one still-active hold (10) counts as reserved; settled debited 10.
    expect(avail.balance.eq(D(90))).toBe(true);
    expect(avail.reserved.eq(D(10))).toBe(true);
    expect(avail.available.eq(D(80))).toBe(true);
    expect(active.leaseId).toBeTruthy(); // referenced
  });
});

// ── Allowance consumed at settle ───────────────────────────────────────────

describe("allowance at settle", () => {
  let store: MemoryStore;
  beforeEach(() => {
    store = new MemoryStore();
  });

  async function managerWithAllowance(s: MemoryStore, allowance: Decimal): Promise<CreditManager> {
    const m = new CreditManager(s, undefined, undefined, { policy: "strict_prepaid" });
    const config = {
      version: 1,
      metering: { models: { "*": "input_tokens * 1" } },
      ledger: { minBalance: 0 },
      plans: {
        free: { label: "Free", allowance: { amount: allowance, period: "calendar_month" } },
      },
    };
    await m.publishPricing(config);
    return m;
  }

  it("allowance offsets settle, then depletes across two settles", async () => {
    const m = await managerWithAllowance(store, D(10));
    await store.addCredits("u1", D(100));
    await store.setUserPlan("u1", "free");

    const l1 = await m.reserve("u1", D(20));
    const d1 = await m.settle("u1", l1.leaseId, D(8)); // fully covered by allowance
    expect(d1.allowanceConsumed.eq(D(8))).toBe(true);
    expect(d1.amount.eq(D(0))).toBe(true);
    expect((await store.getBalance("u1")).balance.eq(D(100))).toBe(true);

    const l2 = await m.reserve("u1", D(20));
    const d2 = await m.settle("u1", l2.leaseId, D(8)); // only 2 allowance left → 6 net
    expect(d2.allowanceConsumed.eq(D(2))).toBe(true);
    expect(d2.amount.eq(D(6))).toBe(true);
    expect((await store.getBalance("u1")).balance.eq(D(94))).toBe(true);
  });
});

// ── Spend caps: deny at admission, advisory at settle ──────────────────────

describe("spend caps", () => {
  let store: MemoryStore;
  beforeEach(() => {
    store = new MemoryStore();
  });

  it("a deny cap blocks admission", async () => {
    const m = await manager(store);
    await store.addCredits("u1", D(1000));
    store.setSpendCap({ userId: "u1", type: "monthly", limit: D(10), action: "deny" });
    await expect(m.reserve("u1", D(20))).rejects.toThrow(CapReachedError);
  });

  it("a warn cap does not block settle but signals", async () => {
    const emitter = new CreditEventEmitter();
    const warnings: CreditEvent[] = [];
    emitter.on("credits.cap_warning", (e) => warnings.push(e));
    const m = new CreditManager(store, undefined, emitter, { policy: "strict_prepaid" });
    await m.publishPricingFromDict({
      version: 1,
      metering: { models: { "*": "input_tokens * 1" } },
      ledger: { minBalance: 0 },
    });
    await store.addCredits("u1", D(1000));
    store.setSpendCap({ userId: "u1", type: "monthly", limit: D(10), action: "warn" });

    const lease = await m.reserve("u1", D(20));
    const ded = await m.settle("u1", lease.leaseId, D(15)); // 15 > 10 warn cap
    expect(ded.balanceAfter.eq(D(985))).toBe(true); // charged in full (advisory)
    expect(ded.capWarning).toBe("warn");
    expect(warnings).toHaveLength(1);
  });

  it("a deny cap at settle is a non-blocking signal", async () => {
    const emitter = new CreditEventEmitter();
    const capReached: CreditEvent[] = [];
    emitter.on("credits.cap_reached", (e) => capReached.push(e));
    const m = new CreditManager(store, undefined, emitter, {
      policy: "overdraft",
      overdraftFloor: D(-500),
    });
    await m.publishPricingFromDict({
      version: 1,
      metering: { models: { "*": "input_tokens * 1" } },
      ledger: { minBalance: 0 },
    });
    await store.addCredits("u1", D(200));
    // Deny cap 100; admit a small hold (under cap), then settle past the cap.
    store.setSpendCap({ userId: "u1", type: "monthly", limit: D(100), action: "deny" });

    const lease = await m.reserve("u1", D(50)); // admission: 0 + 50 ≤ 100 ✓
    const ded = await m.settle("u1", lease.leaseId, D(120)); // de-clamped, breaches cap
    expect(ded.balanceAfter.eq(D(80))).toBe(true); // work is done → charged in full
    expect(ded.capWarning).toBe("deny");
    expect(capReached).toHaveLength(1);
    expect(capReached[0].data?.["blocking"]).toBe(false);
  });
});

// ── Overdraft reconciliation + low_balance re-arm ──────────────────────────

describe("overdraft reconcile", () => {
  let store: MemoryStore;
  beforeEach(() => {
    store = new MemoryStore();
  });

  it("debt then top-up re-arms low_balance", async () => {
    const emitter = new CreditEventEmitter();
    const fired: Decimal[] = [];
    emitter.on("credits.low_balance", (e) => fired.push(e.data?.["threshold"] as Decimal));
    const m = new CreditManager(store, undefined, emitter, {
      policy: "overdraft",
      overdraftFloor: D(-100),
      lowBalance: { thresholds: [D(20)] },
    });
    await m.publishPricingFromDict({
      version: 1,
      metering: { models: { "*": "input_tokens * 1" } },
      ledger: { minBalance: 0 },
    });
    await store.addCredits("u1", D(50));

    const l1 = await m.reserve("u1", D(40));
    await m.settle("u1", l1.leaseId, D(40)); // 50 → 10, crosses 20
    expect(fired.map((d) => d.toString())).toEqual(["20"]);

    // Drop further: still below, must NOT re-fire (no re-arm without top-up).
    const l2 = await m.reserve("u1", D(5));
    await m.settle("u1", l2.leaseId, D(5)); // 10 → 5
    expect(fired.map((d) => d.toString())).toEqual(["20"]);

    // Top up above the level, then descend again → fires once more.
    await m.addCredits("u1", D(95)); // 5 → 100
    const l3 = await m.reserve("u1", D(85));
    await m.settle("u1", l3.leaseId, D(85)); // 100 → 15, crosses 20 again
    expect(fired.map((d) => d.toString())).toEqual(["20", "20"]);
  });
});

// ── Per-operation policy isolation (mixed chat + batch) ────────────────────

describe("mixed operations", () => {
  let store: MemoryStore;
  beforeEach(() => {
    store = new MemoryStore();
  });

  it("chat and batch share one available pool", async () => {
    const m = new CreditManager(store, undefined, undefined, { policy: "strict_prepaid" });
    const config = {
      version: 1,
      metering: { models: { "*": "input_tokens * 1" } },
      ledger: { minBalance: 0 },
      plans: {
        pro: {
          label: "Pro",
          allowance: { amount: D(0), period: "calendar_month" },
          safety: {
            billingMode: "strict",
            perOperation: {
              chat: { billingMode: "strict", maxConcurrent: 1 },
              batch: { billingMode: "strict", maxConcurrent: 5 },
            },
          },
        },
      },
    };
    await m.publishPricing(config);
    await store.addCredits("u1", D(100));
    await store.setUserPlan("u1", "pro");

    // Both operation types lease against the SAME available pool under one lock.
    await m.reserve("u1", D(60), { operationType: "chat" });
    // Only 40 available now; a 60-credit batch can't fit even though its own
    // concurrency slot is free → cross-operation overspend is impossible.
    await expect(m.reserve("u1", D(60), { operationType: "batch" })).rejects.toThrow(
      InsufficientCreditsError,
    );
    // A 40 batch fits exactly.
    expect((await m.reserve("u1", D(40), { operationType: "batch" })).leaseId).toBeTruthy();

    // And chat's maxConcurrent=1 still blocks a second chat.
    await expect(m.reserve("u1", D(1), { operationType: "chat" })).rejects.toThrow(
      ConcurrencyLimitError,
    );
  });
});

// Randomized property invariant: moved to invariants.property.test.ts
// (fast-check model-based/stateful testing) — replaces this fixed-seed
// deterministic-LCG loop with proper property-based testing that explores
// many distinct sequences and shrinks failures to a minimal repro, which a
// single fixed seed can't do.
