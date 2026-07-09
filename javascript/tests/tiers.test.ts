/**
 * Tests for credit tiers (priority-ordered per-tier balances) on MemoryStore
 * + CreditManager.
 *
 * Mirrors the structure/conventions of `memory-store.test.ts` and
 * `credit-manager.test.ts`: `MemoryStore` is the reference implementation
 * (repo `CLAUDE.md`'s parity rule), money is exact `Decimal` everywhere, and
 * time-dependent tests use the store's injectable clock (`setClock`) instead
 * of real sleeps.
 *
 * Covers (see the credit-tiers plan): the no-tiers regression baseline,
 * config validation, `addCredits` tier resolution + expiry reconciliation,
 * priority-ordered deduction (`deductWithAllowance`/`settleLease`),
 * overdraft routing, idempotent replay, LIFO refund restoration, per-tier
 * expiry sweep, `getBucketBalances` shape, and the `CreditManager` pass-through.
 */

import { describe, it, expect, beforeEach } from "vitest";
import Decimal from "decimal.js";
import { MemoryStore } from "../src/stores/memory-store.js";
import { CreditManager } from "../src/manager.js";
import { CreditEventEmitter } from "../src/stores/events.js";
import type { CreditEvent } from "../src/stores/events.js";
import { loadConfigFromDict } from "../src/config.js";
import { ConfigError, StoreError } from "../src/errors.js";

const D = (n: number | string) => new Decimal(n);

/** All credit lifecycle event types (mirrors `credit-manager.test.ts`'s `record` helper). */
const ALL_EVENT_TYPES: string[] = [
  "credits.deducted",
  "credits.deduct_failed",
  "credits.added",
  "credits.refunded",
  "credits.refund_failed",
  "credits.expired",
  "credits.cap_reached",
  "credits.cap_warning",
  "credits.low_balance",
  "credits.plan_changed",
  "credits.reserved",
  "credits.reservation_released",
  "credits.lease_expired",
  "credits.overdraft",
];

/** Collect events of any type into an array (mirrors `credit-manager.test.ts`). */
function record(emitter: CreditEventEmitter, types: string[] = ALL_EVENT_TYPES): CreditEvent[] {
  const events: CreditEvent[] = [];
  for (const t of types) emitter.on(t as CreditEvent["type"], (e) => events.push(e));
  return events;
}

/** A 2-tier config: expiring "gifted" (priority 10) + non-expiring default "purchased" (priority 20). */
const TWO_TIER_CONFIG = {
  version: 1,
  metering: { models: { "*": "input_tokens * 1" } },
  ledger: {
    minBalance: 0,
    buckets: {
      gifted: { label: "Gifted", priority: 10, expires: true, ttlDays: 30 },
      purchased: { label: "Purchased", priority: 20, expires: false, isDefaultBucket: true },
    },
  },
};

/** Same as {@link TWO_TIER_CONFIG} plus a third expiring tier with no `ttlDays`. */
const THREE_TIER_CONFIG = {
  version: 1,
  metering: { models: { "*": "input_tokens * 1" } },
  ledger: {
    minBalance: 0,
    buckets: {
      gifted: { label: "Gifted", priority: 10, expires: true, ttlDays: 30 },
      purchased: { label: "Purchased", priority: 20, expires: false, isDefaultBucket: true },
      noTtl: { label: "NoTTL", priority: 15, expires: true },
    },
  },
};

describe("Credit tiers", () => {
  let store: MemoryStore;

  beforeEach(() => {
    store = new MemoryStore();
  });

  // ── 1. No tiers configured (regression safety) ──────────────────────────

  describe("no tiers configured (regression safety)", () => {
    it("addCredits/deduct behave exactly as pre-tiers", async () => {
      const add = await store.addCredits("u1", D(100), "purchase");
      expect(add.bucket).toBe("default");
      expect(add.newBalance.toString()).toBe("100");

      const deduct = await store.deductWithAllowance("u1", D(30));
      expect(deduct.error).toBeUndefined();
      expect(deduct.amount.toString()).toBe("30");
      expect(deduct.balanceAfter.toString()).toBe("70");
      // Single-tier walk always reports a breakdown (one code path, plan §4),
      // even with no tiers configured — it just has one synthetic key.
      expect(Object.keys(deduct.bucketBreakdown ?? {})).toEqual(["default"]);
      expect(deduct.bucketBreakdown?.default.toString()).toBe("30");
    });

    it("addCredits accepts explicit tier: 'default' with no tiers configured", async () => {
      const add = await store.addCredits("u1", D(10), "adjustment", null, null, "default");
      expect(add.bucket).toBe("default");
    });

    it("addCredits with an unknown explicit tier throws tier_not_found even with no tiers configured", async () => {
      await expect(
        store.addCredits("u1", D(10), "adjustment", null, null, "bogus"),
      ).rejects.toThrow(/tier_not_found/);
    });

    it("getBucketBalances synthesizes a single 'default' entry from the aggregate balance", async () => {
      await store.addCredits("u1", D(50));
      const result = await store.getBucketBalances("u1");
      expect(result.userId).toBe("u1");
      expect(result.buckets).toHaveLength(1);
      expect(result.buckets[0]).toMatchObject({
        bucketKey: "default",
        label: "default",
        priority: 0,
        expires: false,
      });
      expect(result.buckets[0].balance.toString()).toBe("50");
      expect(result.totalBalance.toString()).toBe("50");
    });
  });

  // ── 2. Tier config validation ────────────────────────────────────────────

  describe("tier config validation (loadConfigFromDict)", () => {
    it("rejects duplicate allowOverdraft: true across tiers", () => {
      expect(() =>
        loadConfigFromDict({
          version: 1,
          metering: { models: { "*": "input_tokens * 1" } },
          ledger: {
            minBalance: 0,
            buckets: {
              a: { label: "A", priority: 10, expires: false, allowOverdraft: true },
              b: {
                label: "B",
                priority: 20,
                expires: false,
                allowOverdraft: true,
                isDefaultBucket: true,
              },
            },
          },
        }),
      ).toThrow(ConfigError);
    });

    it("rejects duplicate isDefaultBucket: true across tiers", () => {
      expect(() =>
        loadConfigFromDict({
          version: 1,
          metering: { models: { "*": "input_tokens * 1" } },
          ledger: {
            minBalance: 0,
            buckets: {
              a: { label: "A", priority: 10, expires: false, isDefaultBucket: true },
              b: { label: "B", priority: 20, expires: false, isDefaultBucket: true },
            },
          },
        }),
      ).toThrow(ConfigError);
    });

    it("rejects ttlDays <= 0", () => {
      expect(() =>
        loadConfigFromDict({
          version: 1,
          metering: { models: { "*": "input_tokens * 1" } },
          ledger: {
            minBalance: 0,
            buckets: { a: { label: "A", priority: 10, expires: true, ttlDays: 0 } },
          },
        }),
      ).toThrow(ConfigError);
      expect(() =>
        loadConfigFromDict({
          version: 1,
          metering: { models: { "*": "input_tokens * 1" } },
          ledger: {
            minBalance: 0,
            buckets: { a: { label: "A", priority: 10, expires: true, ttlDays: -5 } },
          },
        }),
      ).toThrow(ConfigError);
    });

    it("rejects an explicit empty buckets object", () => {
      expect(() =>
        loadConfigFromDict({
          version: 1,
          metering: { models: { "*": "input_tokens * 1" } },
          ledger: { minBalance: 0, buckets: {} },
        }),
      ).toThrow(ConfigError);
    });

    it("accepts a valid multi-tier config with one default and one overdraft tier", () => {
      expect(() =>
        loadConfigFromDict({
          version: 1,
          metering: { models: { "*": "input_tokens * 1" } },
          ledger: {
            minBalance: 0,
            buckets: {
              gifted: { label: "Gifted", priority: 10, expires: true, ttlDays: 30 },
              purchased: {
                label: "Purchased",
                priority: 20,
                expires: false,
                isDefaultBucket: true,
                allowOverdraft: true,
              },
            },
          },
        }),
      ).not.toThrow();
    });
  });

  // ── 3. addCredits tier resolution ────────────────────────────────────────

  describe("addCredits tier resolution", () => {
    beforeEach(async () => {
      await store.setActivePricing(THREE_TIER_CONFIG);
    });

    it("explicit valid tier lands money in that tier", async () => {
      const r = await store.addCredits("u1", D(20), "adjustment", null, null, "gifted");
      expect(r.bucket).toBe("gifted");
      const tiers = await store.getBucketBalances("u1");
      const gifted = tiers.buckets.find((t) => t.bucketKey === "gifted");
      expect(gifted?.balance.toString()).toBe("20");
    });

    it("explicit unknown tier throws tier_not_found", async () => {
      await expect(
        store.addCredits("u1", D(10), "adjustment", null, null, "bogus"),
      ).rejects.toThrow(StoreError);
      await expect(
        store.addCredits("u1", D(10), "adjustment", null, null, "bogus"),
      ).rejects.toThrow(/tier_not_found/);
    });

    it("omitted tier resolves to the isDefault tier", async () => {
      const r = await store.addCredits("u1", D(15));
      expect(r.bucket).toBe("purchased");
    });

    it("omitted tier with no configured default throws tier_required", async () => {
      const noDefaultStore = new MemoryStore();
      await noDefaultStore.setActivePricing({
        version: 1,
        metering: { models: { "*": "input_tokens * 1" } },
        ledger: { minBalance: 0, buckets: { a: { label: "A", priority: 10, expires: false } } },
      });
      await expect(noDefaultStore.addCredits("u1", D(10))).rejects.toThrow(StoreError);
      await expect(noDefaultStore.addCredits("u1", D(10))).rejects.toThrow(/tier_required/);
    });
  });

  // ── 4. addCredits expiry reconciliation ──────────────────────────────────

  describe("addCredits expiry reconciliation", () => {
    const T0 = new Date("2026-01-01T00:00:00.000Z");

    beforeEach(async () => {
      store.setClock(() => T0);
      await store.setActivePricing(THREE_TIER_CONFIG);
    });

    it("non-expiring tier + explicit expiresAt throws tier_does_not_expire", async () => {
      const future = new Date(T0.getTime() + 86_400_000);
      await expect(
        store.addCredits("u1", D(10), "adjustment", null, future, "purchased"),
      ).rejects.toThrow(/tier_does_not_expire/);
    });

    it("expiring tier + explicit past expiresAt throws invalid_expires_at", async () => {
      const past = new Date(T0.getTime() - 1000);
      await expect(
        store.addCredits("u1", D(10), "adjustment", null, past, "gifted"),
      ).rejects.toThrow(/invalid_expires_at/);
    });

    it("expiring tier + explicit future expiresAt succeeds", async () => {
      const future = new Date(T0.getTime() + 86_400_000);
      const r = await store.addCredits("u1", D(10), "adjustment", null, future, "gifted");
      expect(r.bucket).toBe("gifted");
      expect(r.newBalance.toString()).toBe("10");
    });

    it("expiring tier + omitted expiresAt computes TTL from ttlDays (verified via sweep)", async () => {
      await store.addCredits("u1", D(10), "adjustment", null, null, "gifted"); // ttlDays: 30

      // Just before the 30-day TTL: not yet expired.
      store.setClock(() => new Date(T0.getTime() + 30 * 86_400_000 - 1000));
      const early = await store.sweepExpiredCredits(true);
      expect(early.expiredCount).toBe(0);

      // Just after the 30-day TTL: expired.
      store.setClock(() => new Date(T0.getTime() + 30 * 86_400_000 + 1000));
      const late = await store.sweepExpiredCredits(true);
      expect(late.expiredCount).toBe(1);
      expect(late.expiredByBucket?.gifted?.toString()).toBe("10");
    });

    it("expiring tier + omitted expiresAt + no ttlDays throws expires_at_required", async () => {
      await expect(
        store.addCredits("u1", D(10), "adjustment", null, null, "noTtl"),
      ).rejects.toThrow(/expires_at_required/);
    });
  });

  // ── 5. Priority-ordered deduction (deductWithAllowance) ──────────────────

  describe("priority-ordered deduction", () => {
    it("drains tiers in priority order and reports an exact bucketBreakdown", async () => {
      await store.setActivePricing({
        version: 1,
        metering: { models: { "*": "input_tokens * 1" } },
        ledger: {
          minBalance: 0,
          buckets: {
            gifted: { label: "Gifted", priority: 10, expires: false },
            allowance: { label: "Allowance", priority: 20, expires: false },
            purchased: { label: "Purchased", priority: 30, expires: false, isDefaultBucket: true },
          },
        },
      });
      await store.addCredits("u1", D(20), "adjustment", null, null, "gifted");
      await store.addCredits("u1", D(15), "adjustment", null, null, "allowance");
      await store.addCredits("u1", D(10)); // omitted → default "purchased"

      const r = await store.deductWithAllowance("u1", D(30));
      expect(r.error).toBeUndefined();
      expect(r.bucketBreakdown?.gifted?.toString()).toBe("20");
      expect(r.bucketBreakdown?.allowance?.toString()).toBe("10");
      expect(r.bucketBreakdown?.purchased).toBeUndefined();

      const tiers = await store.getBucketBalances("u1");
      const byKey = Object.fromEntries(
        tiers.buckets.map((t) => [t.bucketKey, t.balance.toString()]),
      );
      expect(byKey).toEqual({ gifted: "0", allowance: "5", purchased: "10" });
      expect(tiers.totalBalance.toString()).toBe("15");
    });
  });

  // ── 6. Overdraft routing ──────────────────────────────────────────────────

  describe("overdraft routing", () => {
    it("routes excess beyond total tier balance into the allowOverdraft tier", async () => {
      await store.setActivePricing({
        version: 1,
        metering: { models: { "*": "input_tokens * 1" } },
        ledger: {
          minBalance: 0,
          buckets: {
            gifted: { label: "Gifted", priority: 10, expires: false },
            purchased: { label: "Purchased", priority: 20, expires: false, isDefaultBucket: true },
            bonus: { label: "Bonus", priority: 30, expires: false, allowOverdraft: true },
          },
        },
      });
      await store.addCredits("u1", D(10), "adjustment", null, null, "gifted");
      await store.addCredits("u1", D(5)); // default → purchased

      const r = await store.deductWithAllowance("u1", D(40), { minBalance: D(-100) });
      expect(r.error).toBeUndefined();
      expect(r.bucketBreakdown?.gifted?.toString()).toBe("10");
      expect(r.bucketBreakdown?.purchased?.toString()).toBe("5");
      expect(r.bucketBreakdown?.bonus?.toString()).toBe("25");

      const tiers = await store.getBucketBalances("u1");
      const byKey = Object.fromEntries(
        tiers.buckets.map((t) => [t.bucketKey, t.balance.toString()]),
      );
      expect(byKey).toEqual({ gifted: "0", purchased: "0", bonus: "-25" });
      expect(tiers.totalBalance.toString()).toBe("-25");
    });
  });

  // ── 7. settleLease applies the same tier walk ────────────────────────────

  describe("settleLease tier walk", () => {
    it("applies the same tier-priority walk at settle", async () => {
      await store.setActivePricing(TWO_TIER_CONFIG);
      await store.addCredits("u1", D(20), "adjustment", null, null, "gifted");
      await store.addCredits("u1", D(10)); // default → purchased

      const lease = await store.createLease("u1", D(25), "usage", { floor: D(0) });
      expect(lease.error).toBeUndefined();

      const settled = await store.settleLease("u1", lease.leaseId, D(18));
      expect(settled.error).toBeUndefined();
      expect(settled.bucketBreakdown?.gifted?.toString()).toBe("18");
      expect(settled.bucketBreakdown?.purchased).toBeUndefined();

      const tiers = await store.getBucketBalances("u1");
      const byKey = Object.fromEntries(
        tiers.buckets.map((t) => [t.bucketKey, t.balance.toString()]),
      );
      expect(byKey).toEqual({ gifted: "2", purchased: "10" });
    });
  });

  // ── 8. Idempotent replay returns the EXACT original bucketBreakdown ────────

  describe("idempotent replay preserves the original bucketBreakdown", () => {
    it("returns the exact original breakdown, never recomputed, after tier balances change", async () => {
      await store.setActivePricing(TWO_TIER_CONFIG);
      await store.addCredits("u1", D(10), "adjustment", null, null, "gifted");
      await store.addCredits("u1", D(10)); // default → purchased

      const first = await store.deductWithAllowance("u1", D(15), { idempotencyKey: "k1" });
      expect(first.error).toBeUndefined();
      expect(first.bucketBreakdown?.gifted?.toString()).toBe("10");
      expect(first.bucketBreakdown?.purchased?.toString()).toBe("5");

      // Mutate tier balances after the fact — a naive recompute would see
      // different numbers.
      await store.addCredits("u1", D(50), "adjustment", null, null, "gifted");

      const replay = await store.deductWithAllowance("u1", D(15), { idempotencyKey: "k1" });
      expect(replay.idempotent).toBe(true);
      expect(replay.bucketBreakdown?.gifted?.toString()).toBe(
        first.bucketBreakdown!.gifted!.toString(),
      );
      expect(replay.bucketBreakdown?.purchased?.toString()).toBe(
        first.bucketBreakdown!.purchased!.toString(),
      );

      // No double-debit from the replay: gifted reflects only the mutation.
      const tiers = await store.getBucketBalances("u1");
      const gifted = tiers.buckets.find((t) => t.bucketKey === "gifted");
      expect(gifted?.balance.toString()).toBe("50");
    });
  });

  // ── 9. LIFO refund restoration ────────────────────────────────────────────

  describe("LIFO refund restoration", () => {
    it("restores tiers in reverse priority order on a full refund", async () => {
      await store.setActivePricing(TWO_TIER_CONFIG);
      await store.addCredits("u1", D(20), "adjustment", null, null, "gifted");
      await store.addCredits("u1", D(20)); // default → purchased

      const deduct = await store.deductWithAllowance("u1", D(25));
      expect(deduct.bucketBreakdown?.gifted?.toString()).toBe("20");
      expect(deduct.bucketBreakdown?.purchased?.toString()).toBe("5");

      const refund = await store.refundCredits(deduct.transactionId);
      expect(refund.error).toBeUndefined();
      expect(refund.amount.toString()).toBe("25");
      // LIFO: purchased (last drained) is restored first, then gifted.
      expect(refund.bucketBreakdown?.purchased?.toString()).toBe("5");
      expect(refund.bucketBreakdown?.gifted?.toString()).toBe("20");

      const tiers = await store.getBucketBalances("u1");
      const byKey = Object.fromEntries(
        tiers.buckets.map((t) => [t.bucketKey, t.balance.toString()]),
      );
      // Fully restored to the pre-deduct state.
      expect(byKey).toEqual({ gifted: "20", purchased: "20" });
      expect(tiers.totalBalance.toString()).toBe("40");
    });

    it("composes two partial refunds of the same transaction without double-restoring", async () => {
      await store.setActivePricing(TWO_TIER_CONFIG);
      await store.addCredits("u1", D(20), "adjustment", null, null, "gifted");
      await store.addCredits("u1", D(20)); // default → purchased

      const deduct = await store.deductWithAllowance("u1", D(25));
      expect(deduct.bucketBreakdown?.gifted?.toString()).toBe("20");
      expect(deduct.bucketBreakdown?.purchased?.toString()).toBe("5");

      const r1 = await store.refundCredits(deduct.transactionId, D(10));
      expect(r1.error).toBeUndefined();
      expect(r1.bucketBreakdown?.purchased?.toString()).toBe("5");
      expect(r1.bucketBreakdown?.gifted?.toString()).toBe("5");

      const r2 = await store.refundCredits(deduct.transactionId, D(10));
      expect(r2.error).toBeUndefined();
      // purchased is already fully restored (5/5 used up by r1) — must NOT
      // receive more; the entire second refund goes to gifted.
      expect(r2.bucketBreakdown?.purchased).toBeUndefined();
      expect(r2.bucketBreakdown?.gifted?.toString()).toBe("10");

      const tiers = await store.getBucketBalances("u1");
      const byKey = Object.fromEntries(
        tiers.buckets.map((t) => [t.bucketKey, t.balance.toString()]),
      );
      expect(byKey).toEqual({ gifted: "15", purchased: "20" });
      expect(tiers.totalBalance.toString()).toBe("35");
    });
  });

  // ── 10. Per-tier expiry sweep ─────────────────────────────────────────────

  describe("per-tier expiry sweep", () => {
    const T0 = new Date("2026-01-01T00:00:00.000Z");
    const LATER = new Date("2026-02-05T00:00:00.000Z"); // past the 30-day gifted TTL

    it("dry run reports expiredByBucket without mutating; a real sweep decrements only that tier", async () => {
      store.setClock(() => T0);
      await store.setActivePricing(TWO_TIER_CONFIG);
      await store.addCredits("u1", D(20), "adjustment", null, null, "gifted"); // expires in 30d
      await store.addCredits("u1", D(15)); // purchased — never expires

      store.setClock(() => LATER);

      const dry = await store.sweepExpiredCredits(true);
      expect(dry.expiredCount).toBe(1);
      expect(dry.expiredAmount.toString()).toBe("20");
      expect(dry.dryRun).toBe(true);
      expect(dry.expiredByBucket?.gifted?.toString()).toBe("20");

      // Dry run must not mutate any balance.
      let tiers = await store.getBucketBalances("u1");
      let byKey = Object.fromEntries(tiers.buckets.map((t) => [t.bucketKey, t.balance.toString()]));
      expect(byKey).toEqual({ gifted: "20", purchased: "15" });

      const real = await store.sweepExpiredCredits(false);
      expect(real.expiredCount).toBe(1);
      expect(real.expiredByBucket?.gifted?.toString()).toBe("20");

      tiers = await store.getBucketBalances("u1");
      byKey = Object.fromEntries(tiers.buckets.map((t) => [t.bucketKey, t.balance.toString()]));
      // Only the expiring tier lost balance — the non-expiring tier is untouched.
      expect(byKey).toEqual({ gifted: "0", purchased: "15" });
      expect(tiers.totalBalance.toString()).toBe("15");
    });
  });

  // ── 11. getBucketBalances shape ──────────────────────────────────────────────

  describe("getBucketBalances shape", () => {
    it("sorts by priority ascending, reports correct fields, and totalBalance matches getBalance", async () => {
      await store.setActivePricing({
        version: 1,
        metering: { models: { "*": "input_tokens * 1" } },
        ledger: {
          minBalance: 0,
          buckets: {
            c: { label: "C", priority: 5, expires: false },
            a: { label: "A", priority: 1, expires: true, ttlDays: 10 },
            b: { label: "B", priority: 3, expires: false, isDefaultBucket: true },
          },
        },
      });
      await store.addCredits("u1", D(5), "adjustment", null, null, "c");
      await store.addCredits("u1", D(3), "adjustment", null, null, "a");
      await store.addCredits("u1", D(7)); // omitted → default "b"

      const result = await store.getBucketBalances("u1");
      expect(result.userId).toBe("u1");
      expect(result.buckets.map((t) => t.bucketKey)).toEqual(["a", "b", "c"]);
      expect(result.buckets.find((t) => t.bucketKey === "a")).toMatchObject({
        label: "A",
        priority: 1,
        expires: true,
      });
      expect(result.buckets.find((t) => t.bucketKey === "b")).toMatchObject({
        label: "B",
        priority: 3,
        expires: false,
      });
      expect(result.buckets.find((t) => t.bucketKey === "c")).toMatchObject({
        label: "C",
        priority: 5,
        expires: false,
      });

      const balance = await store.getBalance("u1");
      expect(result.totalBalance.toString()).toBe(balance.balance.toString());
      expect(result.totalBalance.toString()).toBe("15");
    });
  });

  // ── 12. CreditManager pass-through ────────────────────────────────────────

  describe("CreditManager.getBucketBalances pass-through", () => {
    it("returns the same result as store.getBucketBalances and emits no event", async () => {
      const emitter = new CreditEventEmitter();
      const mgr = new CreditManager(store, undefined, emitter);
      await mgr.publishPricingFromDict(TWO_TIER_CONFIG);
      await mgr.addCredits("u1", 20, { tier: "gifted" });
      await mgr.addCredits("u1", 10);

      const events = record(emitter);

      const fromManager = await mgr.getBucketBalances("u1");
      const fromStore = await store.getBucketBalances("u1");

      expect(fromManager.userId).toBe(fromStore.userId);
      expect(fromManager.totalBalance.toString()).toBe(fromStore.totalBalance.toString());
      expect(fromManager.buckets.map((t) => [t.bucketKey, t.balance.toString()])).toEqual(
        fromStore.buckets.map((t) => [t.bucketKey, t.balance.toString()]),
      );
      expect(events).toHaveLength(0);
    });
  });
});
