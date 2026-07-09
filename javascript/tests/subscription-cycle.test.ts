import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import Decimal from "decimal.js";
import { CreditManager } from "../src/manager.js";
import { MemoryStore } from "../src/stores/memory-store.js";
import { CreditEventEmitter } from "../src/stores/events.js";
import type { CreditEvent } from "../src/stores/events.js";
import { ConfigError } from "../src/errors.js";

/**
 * A "subscription" bucket that expires, with a `ttlDays` fallback so the
 * `replacePrior` expire-adjustment (which never passes an explicit
 * `expiresAt`) can always resolve one, exactly like any other expiring bucket.
 */
const SUBSCRIPTION_CONFIG = {
  version: 1,
  metering: { models: { "*": "input_tokens * 1" } },
  ledger: {
    buckets: {
      subscription: {
        label: "Subscription",
        priority: 10,
        expires: true,
        ttlDays: 30,
        default: true,
      },
    },
  },
};

describe("CreditManager.grantSubscriptionCycle", () => {
  let store: MemoryStore;
  let manager: CreditManager;

  beforeEach(async () => {
    store = new MemoryStore();
    manager = new CreditManager(store);
    await store.setActivePricing(SUBSCRIPTION_CONFIG);
  });

  it("grants the cycle, lands in the 'subscription' tier by default, and emits credits.cycle_renewed", async () => {
    const emitter = new CreditEventEmitter();
    const mgr = new CreditManager(store, undefined, emitter);
    const events: CreditEvent[] = [];
    emitter.on("credits.cycle_renewed", (e) => events.push(e));

    const result = await mgr.grantSubscriptionCycle("user-1", 100, { ttlDays: 30 });

    expect(result.bucket).toBe("subscription");
    expect(result.amount.toString()).toBe("100");
    expect((await mgr.getBalance("user-1")).balance.toString()).toBe("100");

    expect(events).toHaveLength(1);
    expect(events[0].userId).toBe("user-1");
    expect(events[0].data?.bucket).toBe("subscription");
    expect((events[0].data?.amount as Decimal).toString()).toBe("100");
  });

  it("assigns planKey via setUserPlan when given", async () => {
    await manager.grantSubscriptionCycle("user-1", 100, {
      ttlDays: 30,
      planKey: "pro-monthly",
    });
    expect((await manager.getUserPlan("user-1")).planId).toBe("pro-monthly");
  });

  it("does not assign a plan when planKey is omitted", async () => {
    await manager.grantSubscriptionCycle("user-1", 100, { ttlDays: 30 });
    expect((await manager.getUserPlan("user-1")).planId).toBeFalsy();
  });

  describe("throws if both expiresAt and ttlDays are given", () => {
    it("rejects with ConfigError before touching the store", async () => {
      await expect(
        manager.grantSubscriptionCycle("user-1", 100, {
          ttlDays: 30,
          expiresAt: new Date(Date.now() + 30 * 86_400_000),
        }),
      ).rejects.toThrow(ConfigError);

      // No partial side effect — nothing was granted.
      expect((await manager.getBalance("user-1")).balance.toString()).toBe("0");
    });
  });

  describe("idempotency (isolated from replacePrior via replacePrior: false)", () => {
    it("the same idempotencyKey twice grants exactly once", async () => {
      const r1 = await manager.grantSubscriptionCycle("user-1", 100, {
        ttlDays: 30,
        replacePrior: false,
        idempotencyKey: "invoice-123",
      });
      const r2 = await manager.grantSubscriptionCycle("user-1", 100, {
        ttlDays: 30,
        replacePrior: false,
        idempotencyKey: "invoice-123",
      });

      expect(r2.transactionId).toBe(r1.transactionId);
      expect(r2.idempotent).toBe(true);
      // Only ONE grant landed, not two.
      expect((await manager.getBalance("user-1")).balance.toString()).toBe("100");
    });

    it("a different idempotencyKey grants again (a genuinely new cycle)", async () => {
      await manager.grantSubscriptionCycle("user-1", 100, {
        ttlDays: 30,
        replacePrior: false,
        idempotencyKey: "invoice-123",
      });
      await manager.grantSubscriptionCycle("user-1", 100, {
        ttlDays: 30,
        replacePrior: false,
        idempotencyKey: "invoice-124",
      });
      expect((await manager.getBalance("user-1")).balance.toString()).toBe("200");
    });
  });

  describe("redelivery with replacePrior: true (the real default combination)", () => {
    it("a redelivered webhook does not wipe the balance it just granted", async () => {
      const r1 = await manager.grantSubscriptionCycle("user-1", 100, {
        ttlDays: 30,
        idempotencyKey: "invoice-123",
      });
      expect((await manager.getBalance("user-1")).balance.toString()).toBe("100");

      // Redelivery of the SAME webhook event: must be a full no-op, including
      // skipping the replace-prior wipe against the balance the first call
      // just granted.
      const r2 = await manager.grantSubscriptionCycle("user-1", 100, {
        ttlDays: 30,
        idempotencyKey: "invoice-123",
      });
      expect(r2.transactionId).toBe(r1.transactionId);
      expect((await manager.getBalance("user-1")).balance.toString()).toBe("100");

      // A THIRD redelivery must still be a no-op.
      await manager.grantSubscriptionCycle("user-1", 100, {
        ttlDays: 30,
        idempotencyKey: "invoice-123",
      });
      expect((await manager.getBalance("user-1")).balance.toString()).toBe("100");
    });
  });

  describe("replacePrior true vs false", () => {
    it("replacePrior: true (default) expires the leftover balance before granting the new cycle", async () => {
      // Cycle 1.
      await manager.grantSubscriptionCycle("user-1", 100, { ttlDays: 30 });
      expect((await manager.getBalance("user-1")).balance.toString()).toBe("100");

      // Cycle 2 — no usage in between, 100 left over from cycle 1.
      await manager.grantSubscriptionCycle("user-1", 50, { ttlDays: 30 });
      // The 100 leftover was expired, not stacked: balance is 50, not 150.
      expect((await manager.getBalance("user-1")).balance.toString()).toBe("50");
    });

    it("replacePrior: false stacks the new cycle on top of any leftover balance", async () => {
      await manager.grantSubscriptionCycle("user-1", 100, { ttlDays: 30 });
      await manager.grantSubscriptionCycle("user-1", 50, { ttlDays: 30, replacePrior: false });
      expect((await manager.getBalance("user-1")).balance.toString()).toBe("150");
    });

    it("replacePrior: true is a no-op when there is nothing left to expire", async () => {
      const result = await manager.grantSubscriptionCycle("user-1", 100, { ttlDays: 30 });
      expect(result.amount.toString()).toBe("100");
      expect((await manager.getBalance("user-1")).balance.toString()).toBe("100");
    });
  });

  describe("ttlDays vs equivalent expiresAt produce the same result", () => {
    const T0 = new Date("2026-01-01T00:00:00.000Z");

    afterEach(() => {
      vi.useRealTimers();
    });

    it("both expire at the same wall-clock instant", async () => {
      vi.useFakeTimers();
      vi.setSystemTime(T0);

      // ttlDays-based grant.
      await manager.grantSubscriptionCycle("user-ttl", 100, { ttlDays: 30 });
      // Equivalent, explicit expiresAt-based grant (computed the same way
      // grantSubscriptionCycle computes it internally: now + 30 days).
      await manager.grantSubscriptionCycle("user-explicit", 100, {
        expiresAt: new Date(T0.getTime() + 30 * 86_400_000),
      });

      // Just before 30 days: neither has expired yet.
      vi.setSystemTime(new Date(T0.getTime() + 30 * 86_400_000 - 1000));
      const earlyTtl = await store.sweepExpiredCredits(true, "user-ttl");
      const earlyExplicit = await store.sweepExpiredCredits(true, "user-explicit");
      expect(earlyTtl.expiredCount).toBe(0);
      expect(earlyExplicit.expiredCount).toBe(0);

      // Just after 30 days: BOTH have expired identically.
      vi.setSystemTime(new Date(T0.getTime() + 30 * 86_400_000 + 1000));
      const lateTtl = await store.sweepExpiredCredits(true, "user-ttl");
      const lateExplicit = await store.sweepExpiredCredits(true, "user-explicit");
      expect(lateTtl.expiredCount).toBe(1);
      expect(lateExplicit.expiredCount).toBe(1);
      expect(lateTtl.expiredAmount.toString()).toBe(lateExplicit.expiredAmount.toString());
    });
  });
});
