/**
 * Tests for the lease lifecycle (interface plan §3/§4) on MemoryStore + manager.
 *
 * Covers the plan's acceptance criteria: atomic lease admission & double-submit,
 * strict zero-debt under concurrency, the agentic feature gate, overdraft
 * full-billing (D5), TTL / renewal, release idempotency, multi-level low_balance,
 * and presets / planless defaults.
 *
 * Money is exact `Decimal` everywhere (contract §1). Lease expiry is forced
 * via `MemoryStore.setClock()` (a manually-advanced fake clock) rather than
 * sleeping or reaching into private reservation state.
 */

import { describe, it, expect, beforeEach } from "vitest";
import Decimal from "decimal.js";
import { CreditManager } from "../src/manager.js";
import type { CreditManagerOptions } from "../src/manager.js";
import { MemoryStore } from "../src/stores/memory-store.js";
import { CreditEventEmitter } from "../src/stores/events.js";
import type { CreditEvent } from "../src/stores/events.js";
import {
  ConcurrencyLimitError,
  FeatureNotEntitledError,
  InsufficientCreditsError,
  LeaseExpiredError,
  LeaseNotFoundError,
} from "../src/errors.js";
import type { PricingConfigData } from "../src/types.js";

const D = (n: number | string) => new Decimal(n);

/** A strict_prepaid manager with a default `input_tokens * 1` model. */
async function strictManager(
  store: MemoryStore,
  minBalance: Decimal = D(5),
  options?: CreditManagerOptions,
): Promise<CreditManager> {
  const m = new CreditManager(store, undefined, undefined, {
    policy: "strict_prepaid",
    ...options,
  });
  await m.publishPricingFromDict({
    models: { _default: "input_tokens * 1" },
    minBalance: minBalance.toNumber(),
  });
  return m;
}

/**
 * Deterministic, manually-advanced clock wired into `MemoryStore.setClock()`
 * — advances lease expiry without a real sleep and without reaching into
 * private reservation state.
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

// ── 1. Lease admission / double-submit (maxConcurrent) ─────────────────────

describe("admission concurrency", () => {
  let store: MemoryStore;
  beforeEach(() => {
    store = new MemoryStore();
  });

  it("double-submit is blocked with maxConcurrent=1", async () => {
    const m = await strictManager(store, D(0), { maxConcurrent: 1 });
    await store.addCredits("u1", D(100));

    const lease = await m.reserve("u1", D(10), { operationType: "chat" });
    expect(lease.leaseId).toBeTruthy();
    // A second concurrent op of the same type is rejected — not a balance leak.
    await expect(m.reserve("u1", D(10), { operationType: "chat" })).rejects.toThrow(
      ConcurrencyLimitError,
    );

    const avail = await m.getAvailable("u1");
    expect(avail.reserved.eq(D(10))).toBe(true); // only the one live hold
    expect(avail.available.eq(D(90))).toBe(true);
  });

  it("releasing frees a concurrency slot", async () => {
    const m = await strictManager(store, D(0), { maxConcurrent: 1 });
    await store.addCredits("u1", D(100));

    const lease = await m.reserve("u1", D(10), { operationType: "chat" });
    await m.release("u1", lease.leaseId);
    // Slot is free again.
    const lease2 = await m.reserve("u1", D(10), { operationType: "chat" });
    expect(lease2.leaseId).toBeTruthy();
  });

  it("maxConcurrent is per operation type", async () => {
    const m = await strictManager(store, D(0), { maxConcurrent: 1 });
    await store.addCredits("u1", D(100));
    await m.reserve("u1", D(10), { operationType: "chat" });
    // A different op type has its own slot.
    const other = await m.reserve("u1", D(10), { operationType: "batch" });
    expect(other.leaseId).toBeTruthy();
  });
});

// ── 2. Strict zero-debt under concurrency ──────────────────────────────────

describe("strict zero-debt", () => {
  let store: MemoryStore;
  beforeEach(() => {
    store = new MemoryStore();
  });

  it("worst-case leases never breach the floor", async () => {
    const m = await strictManager(store, D(5));
    await store.addCredits("u1", D(100)); // floor 5 ⇒ 95 usable

    const l1 = await m.reserve("u1", D(40));
    const l2 = await m.reserve("u1", D(40));
    // Third worst-case lease would push available below the floor → rejected.
    await expect(m.reserve("u1", D(40))).rejects.toThrow(InsufficientCreditsError);

    // Each settle charges the ACTUAL (≤ worst-case lease); balance stays ≥ floor.
    await m.settle("u1", l1.leaseId, D(30));
    await m.settle("u1", l2.leaseId, D(15));
    const bal = await m.getBalance("u1");
    expect(bal.balance.eq(D(55))).toBe(true);
    expect(bal.balance.gte(D(5))).toBe(true);
  });

  it("available accounts for active holds", async () => {
    const m = await strictManager(store, D(0));
    await store.addCredits("u1", D(100));
    await m.reserve("u1", D(30));
    const avail = await m.getAvailable("u1");
    expect(avail.balance.eq(D(100))).toBe(true);
    expect(avail.reserved.eq(D(30))).toBe(true);
    expect(avail.available.eq(D(70))).toBe(true);
  });
});

// ── 3. Agentic feature gate ────────────────────────────────────────────────

describe("feature gate", () => {
  let store: MemoryStore;
  beforeEach(() => {
    store = new MemoryStore();
  });

  async function managerWithPlans(s: MemoryStore): Promise<CreditManager> {
    const m = new CreditManager(s, undefined, undefined, { policy: "strict_prepaid" });
    const config: PricingConfigData = {
      models: { _default: "input_tokens * 1" },
      minBalance: 0,
      plans: {
        free: { id: "free", name: "Free", freeAllowance: D(0), features: { chat: true } },
        pro: {
          id: "pro",
          name: "Pro",
          freeAllowance: D(0),
          features: { chat: true, agentic: true },
        },
      },
    };
    await m.publishPricing(config);
    return m;
  }

  it("free user is blocked from agentic", async () => {
    const m = await managerWithPlans(store);
    await store.addCredits("u1", D(100));
    await store.setUserPlan("u1", "free");
    await expect(m.reserve("u1", D(10), { requiredFeature: "agentic" })).rejects.toThrow(
      FeatureNotEntitledError,
    );
  });

  it("pro user is allowed agentic", async () => {
    const m = await managerWithPlans(store);
    await store.addCredits("u1", D(100));
    await store.setUserPlan("u1", "pro");
    const lease = await m.reserve("u1", D(10), { requiredFeature: "agentic" });
    expect(lease.leaseId).toBeTruthy();
  });

  it("canAfford reports the feature gate", async () => {
    const m = await managerWithPlans(store);
    await store.addCredits("u1", D(100));
    await store.setUserPlan("u1", "free");
    const res = await m.canAfford("u1", D(10), { requiredFeature: "agentic" });
    expect(res.affordable).toBe(false);
    expect(res.reason).toBe("feature_not_entitled");
  });
});

// ── 4. Overdraft full-billing (D5) ─────────────────────────────────────────

describe("overdraft", () => {
  let store: MemoryStore;
  beforeEach(() => {
    store = new MemoryStore();
  });

  it("settle clamps to overdraft floor (C1 fix)", async () => {
    // C1 fix: settle clamps the charge so balance >= overdraft_floor (-50).
    // balance=0, actual=60, floor=-50 → max debit=50, balance_after=-50.
    const m = new CreditManager(store, undefined, undefined, {
      policy: "overdraft",
      overdraftFloor: D(-50),
    });
    await m.publishPricingFromDict({ models: { _default: "input_tokens * 1" }, minBalance: 0 });
    await store.addCredits("u1", D(0)); // ensure a balance row at 0

    const lease = await m.reserve("u1", D(10)); // small estimate
    const ded = await m.settle("u1", lease.leaseId, D(60));
    expect(ded.balanceAfter.eq(D(-50))).toBe(true);

    // A NEW admission is now rejected (available ≤ floor).
    await expect(m.reserve("u1", D(1))).rejects.toThrow(InsufficientCreditsError);

    // addCredits reconciles the negative balance.
    const res = await m.addCredits("u1", D(200));
    expect(res.newBalance.eq(D(150))).toBe(true);
  });

  it("overdraft event is emitted when the balance goes negative", async () => {
    const emitter = new CreditEventEmitter();
    const events: CreditEvent[] = [];
    emitter.on("credits.overdraft", (e) => events.push(e));
    const m = new CreditManager(store, undefined, emitter, {
      policy: "overdraft",
      overdraftFloor: D(-50),
    });
    await m.publishPricingFromDict({ models: { _default: "input_tokens * 1" }, minBalance: 0 });
    await store.addCredits("u1", D(0));

    const lease = await m.reserve("u1", D(10));
    await m.settle("u1", lease.leaseId, D(30));
    expect(events).toHaveLength(1);
    expect((events[0].data?.balance as Decimal).eq(D(-30))).toBe(true);
  });
});

// ── 5. TTL / renewal ───────────────────────────────────────────────────────

describe("ttl / renewal", () => {
  let store: MemoryStore;
  beforeEach(() => {
    store = new MemoryStore();
  });

  it("settle on an expired lease raises", async () => {
    const clock = fakeClock(new Date("2024-01-01T00:00:00Z"));
    store.setClock(clock.fn);
    const m = await strictManager(store, D(0));
    await store.addCredits("u1", D(100));
    const lease = await m.reserve("u1", D(20), { ttl: 1 });
    // Advance the fake clock past the lease's TTL — deterministic, no real
    // sleep and no reaching into private reservation state.
    clock.advance(2000);
    await expect(m.settle("u1", lease.leaseId, D(20))).rejects.toThrow(LeaseExpiredError);
    // The expired hold no longer counts against available.
    expect((await m.getAvailable("u1")).available.eq(D(100))).toBe(true);
  });

  it("renew extends the TTL and allows settle", async () => {
    const clock = fakeClock(new Date("2024-01-01T00:00:00Z"));
    store.setClock(clock.fn);
    const m = await strictManager(store, D(0));
    await store.addCredits("u1", D(100));
    const lease = await m.reserve("u1", D(20), { ttl: 1 });
    // Almost-expired (still valid) → renew pushes it out, then settle succeeds.
    clock.advance(999);
    const renewed = await m.renew("u1", lease.leaseId, 600);
    expect(renewed.error == null).toBe(true);
    const ded = await m.settle("u1", lease.leaseId, D(20));
    expect(ded.balanceAfter.eq(D(80))).toBe(true);
  });

  it("renew on an expired lease raises", async () => {
    const clock = fakeClock(new Date("2024-01-01T00:00:00Z"));
    store.setClock(clock.fn);
    const m = await strictManager(store, D(0));
    await store.addCredits("u1", D(100));
    const lease = await m.reserve("u1", D(20), { ttl: 1 });
    clock.advance(2000);
    await expect(m.renew("u1", lease.leaseId, 600)).rejects.toThrow(LeaseExpiredError);
  });
});

// ── 6. release idempotency (H1) ────────────────────────────────────────────

describe("release idempotency", () => {
  let store: MemoryStore;
  beforeEach(() => {
    store = new MemoryStore();
  });

  it("double release is typed, not an error", async () => {
    const m = await strictManager(store, D(0));
    await store.addCredits("u1", D(100));
    const lease = await m.reserve("u1", D(20));

    const r1 = await m.release("u1", lease.leaseId);
    expect(r1.released).toBe(true);
    expect(r1.reason).toBe("released");
    const r2 = await m.release("u1", lease.leaseId);
    expect(r2.released).toBe(false);
    expect(r2.reason).toBe("already_released");
  });

  it("settle after release returns lease_not_found", async () => {
    const m = await strictManager(store, D(0));
    await store.addCredits("u1", D(100));
    const lease = await m.reserve("u1", D(20));
    await m.release("u1", lease.leaseId);
    await expect(m.settle("u1", lease.leaseId, D(20))).rejects.toThrow(LeaseNotFoundError);
  });

  it("settle after settle replays", async () => {
    const m = await strictManager(store, D(0));
    await store.addCredits("u1", D(100));
    const lease = await m.reserve("u1", D(20));
    const first = await m.settle("u1", lease.leaseId, D(20));
    const second = await m.settle("u1", lease.leaseId, D(20));
    expect(second.idempotent).toBe(true);
    expect(second.amount.eq(first.amount)).toBe(true);
    // Balance only moved once.
    expect((await m.getBalance("u1")).balance.eq(D(80))).toBe(true);
  });

  it("release after settle reports already_settled", async () => {
    const m = await strictManager(store, D(0));
    await store.addCredits("u1", D(100));
    const lease = await m.reserve("u1", D(20));
    await m.settle("u1", lease.leaseId, D(20));
    const r = await m.release("u1", lease.leaseId);
    expect(r.released).toBe(false);
    expect(r.reason).toBe("already_settled");
  });

  it("release of an unknown lease", async () => {
    const m = await strictManager(store, D(0));
    const r = await m.release("u1", "no-such-lease");
    expect(r.released).toBe(false);
    expect(r.reason).toBe("not_found");
  });
});

// ── 7. Multi-level low_balance (H4) ────────────────────────────────────────

describe("multi-level low_balance", () => {
  let store: MemoryStore;
  beforeEach(() => {
    store = new MemoryStore();
  });

  it("each level fires once per descent and re-arms", async () => {
    const emitter = new CreditEventEmitter();
    const m = new CreditManager(store, undefined, emitter, {
      policy: "overdraft",
      overdraftFloor: D(0),
      lowBalance: { thresholds: [D(50), D(20), D(10)] },
    });
    await m.publishPricingFromDict({ models: { _default: "input_tokens * 1" }, minBalance: 0 });
    const fired: Decimal[] = [];
    emitter.on("credits.low_balance", (e) => fired.push(e.data?.threshold as Decimal));
    await store.addCredits("u1", D(100));

    const charge = async (amount: Decimal): Promise<void> => {
      const lease = await m.reserve("u1", amount);
      await m.settle("u1", lease.leaseId, amount);
    };

    await charge(D(55)); // 100 → 45 : crosses 50
    await charge(D(30)); // 45 → 15 : crosses 20
    await charge(D(7)); // 15 → 8  : crosses 10
    expect(fired.map((d) => d.toString())).toEqual(["50", "20", "10"]);

    // Top-up re-arms; a single big charge crossing all levels fires once for the
    // lowest crossed.
    await m.addCredits("u1", D(92)); // 8 → 100
    await charge(D(95)); // 100 → 5 : crosses 50,20,10 → fire once @ 10
    expect(fired.map((d) => d.toString())).toEqual(["50", "20", "10", "10"]);
  });

  it("onLowBalance handler failure never blocks", async () => {
    const m = new CreditManager(store, undefined, undefined, {
      policy: "overdraft",
      overdraftFloor: D(0),
      lowBalance: {
        thresholds: [D(20)],
        onTrigger: () => {
          throw new Error("handler down");
        },
      },
    });
    await m.publishPricingFromDict({ models: { _default: "input_tokens * 1" }, minBalance: 0 });
    await store.addCredits("u1", D(100));
    const lease = await m.reserve("u1", D(85));
    // The handler throws, but settle still completes normally.
    const ded = await m.settle("u1", lease.leaseId, D(85));
    expect(ded.balanceAfter.eq(D(15))).toBe(true);
  });
});

// ── 8. Presets / planless default (M1) ─────────────────────────────────────

describe("presets / planless", () => {
  let store: MemoryStore;
  beforeEach(() => {
    store = new MemoryStore();
  });

  it("strict_prepaid never goes negative", async () => {
    const m = await strictManager(store, D(0));
    await store.addCredits("u1", D(50));
    // Cannot reserve beyond the balance under strict.
    await expect(m.reserve("u1", D(60))).rejects.toThrow(InsufficientCreditsError);
    expect((await m.getBalance("u1")).balance.eq(D(50))).toBe(true);
  });

  it("planless user gets the constructor default, not unlimited", async () => {
    // Overdraft preset with a bounded floor applies to a user with no plan.
    const m = new CreditManager(store, undefined, undefined, {
      policy: "overdraft",
      overdraftFloor: D(-20),
    });
    await m.publishPricingFromDict({ models: { _default: "input_tokens * 1" }, minBalance: 0 });
    await store.addCredits("u1", D(0));
    await m.reserve("u1", D(20)); // down to the floor, ok
    await expect(m.reserve("u1", D(1))).rejects.toThrow(InsufficientCreditsError);
  });

  it("plan per-operation overrides the preset", async () => {
    // strict_prepaid preset, but the plan opts one op type into overdraft.
    const m = new CreditManager(store, undefined, undefined, { policy: "strict_prepaid" });
    const config: PricingConfigData = {
      models: { _default: "input_tokens * 1" },
      minBalance: 0,
      plans: {
        pro: {
          id: "pro",
          name: "Pro",
          freeAllowance: D(0),
          defaultBillingMode: "strict",
          perOperation: {
            agent: { billingMode: "overdraft", overdraftFloor: D(-30) },
          },
        },
      },
    };
    await m.publishPricing(config);
    await store.addCredits("u1", D(0));
    await store.setUserPlan("u1", "pro");

    // Default op stays strict (no debt allowed).
    await expect(m.reserve("u1", D(5), { operationType: "chat" })).rejects.toThrow(
      InsufficientCreditsError,
    );
    // The 'agent' op inherits the plan's overdraft policy → can go to -30.
    const lease = await m.reserve("u1", D(25), { operationType: "agent" });
    expect(lease.leaseId).toBeTruthy();
    expect(lease.billingMode).toBe("overdraft");
  });
});

// ── runBilled shortcut (§4) ────────────────────────────────────────────────

describe("runBilled", () => {
  let store: MemoryStore;
  beforeEach(() => {
    store = new MemoryStore();
  });

  it("reserves then settles", async () => {
    const m = await strictManager(store, D(0));
    await store.addCredits("u1", D(100));

    const out = await m.runBilled("u1", {
      estimate: D(50),
      doWork: async () => ({ result: "answer", actual: D(30) }),
    });
    expect(out.result).toBe("answer");
    expect(out.deduction.balanceAfter.eq(D(70))).toBe(true);
  });

  it("releases on failure", async () => {
    const m = await strictManager(store, D(0));
    await store.addCredits("u1", D(100));

    await expect(
      m.runBilled("u1", {
        estimate: D(50),
        doWork: async () => {
          throw new Error("work failed");
        },
      }),
    ).rejects.toThrow("work failed");
    // Lease was released — nothing held, nothing charged.
    const avail = await m.getAvailable("u1");
    expect(avail.reserved.eq(D(0))).toBe(true);
    expect(avail.available.eq(D(100))).toBe(true);
  });
});

// ── zero-cost settle (M3) & metrics-based sizing ───────────────────────────

describe("misc", () => {
  let store: MemoryStore;
  beforeEach(() => {
    store = new MemoryStore();
  });

  it("zero-cost settle releases without charge", async () => {
    const m = await strictManager(store, D(0));
    await store.addCredits("u1", D(100));
    const lease = await m.reserve("u1", D(20));
    const ded = await m.settle("u1", lease.leaseId, D(0));
    expect(ded.amount.eq(D(0))).toBe(true);
    expect((await m.getBalance("u1")).balance.eq(D(100))).toBe(true);
    // Lease is finalized (settled), not still holding.
    expect((await m.getAvailable("u1")).reserved.eq(D(0))).toBe(true);
  });

  it("reserve and settle with metrics", async () => {
    const m = new CreditManager(store, undefined, undefined, { policy: "strict_prepaid" });
    await m.publishPricingFromDict({
      models: { "gpt-4": "input_tokens * 0.01 + output_tokens * 0.03" },
      minBalance: 0,
    });
    await store.addCredits("u1", D(100));
    const worst = { model: "gpt-4", inputTokens: 1000, outputTokens: 1000 }; // cost 40
    const lease = await m.reserve("u1", worst);
    expect(lease.amount.eq(D("40"))).toBe(true);
    const actual = { model: "gpt-4", inputTokens: 500, outputTokens: 200 }; // cost 11
    const ded = await m.settle("u1", lease.leaseId, actual);
    expect(ded.balanceAfter.eq(D("89"))).toBe(true);
  });
});
