import { describe, it, expect, beforeEach } from "vitest";
import Decimal from "decimal.js";
import { CreditManager } from "../src/manager.js";
import { MemoryStore } from "../src/stores/memory-store.js";
import { CreditEventEmitter } from "../src/stores/events.js";
import type { CreditEvent } from "../src/stores/events.js";
import { InsufficientCreditsError } from "../src/errors.js";
import type { PricingConfigData } from "../src/types.js";

const D = (n: number | string) => new Decimal(n);

const TEST_CONFIG: PricingConfigData = {
  models: { _default: "input_tokens * 1" },
};

/**
 * Credit expiry is the one time-based mechanism that is NOT lazy-on-read by
 * default (unlike allowance windows / lease TTLs, which are always checked at
 * read time). `options.lazyExpiry` closes that gap. These tests use the
 * store's injectable clock (see `MemoryStore.setClock`, also used by
 * tests/tiers.test.ts) rather than sleeps.
 */
describe("CreditManager lazyExpiry", () => {
  let store: MemoryStore;
  const T0 = new Date("2026-01-01T00:00:00.000Z");
  const AFTER_EXPIRY = new Date("2026-01-02T00:00:00.000Z");

  beforeEach(() => {
    store = new MemoryStore();
    store.setClock(() => T0);
  });

  describe("default (lazyExpiry unset/false) — unchanged behaviour", () => {
    it("an expired grant stays in the balance until an explicit sweep", async () => {
      const manager = new CreditManager(store);
      await manager.addCredits("user-1", 100, {
        type: "purchase",
        expiresAt: new Date(T0.getTime() + 1000),
      });

      store.setClock(() => AFTER_EXPIRY);

      // No lazy sweep — the expired grant is still counted.
      expect((await manager.getBalance("user-1")).balance.toString()).toBe("100");
      expect((await manager.getCreditTiers("user-1")).totalBalance.toString()).toBe("100");

      // Only an explicit sweep removes it.
      const sweep = await manager.sweepExpiredCredits();
      expect(sweep.expiredCount).toBe(1);
      expect((await manager.getBalance("user-1")).balance.toString()).toBe("0");
    });

    it("deduct/reserve still see (and can spend) the expired grant with no explicit sweep", async () => {
      const manager = new CreditManager(store);
      await manager.publishPricingFromDict(TEST_CONFIG);
      await manager.addCredits("user-1", 100, {
        type: "purchase",
        expiresAt: new Date(T0.getTime() + 1000),
      });

      store.setClock(() => AFTER_EXPIRY);

      const result = await manager.deduct("user-1", { inputTokens: 50 });
      expect(result.error).toBeUndefined();
      expect(result.balanceAfter.toString()).toBe("50");
    });

    it("getAvailable still sees the expired grant until an explicit sweep", async () => {
      const manager = new CreditManager(store);
      await manager.addCredits("user-1", 100, {
        type: "purchase",
        expiresAt: new Date(T0.getTime() + 1000),
      });

      store.setClock(() => AFTER_EXPIRY);

      expect((await manager.getAvailable("user-1")).available.toString()).toBe("100");
    });
  });

  describe("lazyExpiry: true — expired grant invisible with no explicit sweep", () => {
    function makeManager(emitter?: CreditEventEmitter) {
      return new CreditManager(store, undefined, emitter, { lazyExpiry: true });
    }

    it("getBalance transparently sweeps the user's own expired credits", async () => {
      const manager = makeManager();
      await manager.addCredits("user-1", 50, { type: "purchase" }); // permanent
      await manager.addCredits("user-1", 100, {
        type: "purchase",
        expiresAt: new Date(T0.getTime() + 1000),
      }); // expires

      store.setClock(() => AFTER_EXPIRY);

      // No manual sweepExpiredCredits() call anywhere in this test.
      const balance = await manager.getBalance("user-1");
      expect(balance.balance.toString()).toBe("50");
    });

    it("getCreditTiers transparently sweeps before reporting balances", async () => {
      const manager = makeManager();
      await manager.addCredits("user-1", 100, {
        type: "purchase",
        expiresAt: new Date(T0.getTime() + 1000),
      });

      store.setClock(() => AFTER_EXPIRY);

      const tiers = await manager.getCreditTiers("user-1");
      expect(tiers.totalBalance.toString()).toBe("0");
    });

    it("getAvailable (the credits-page 'UI only' read) transparently sweeps before reporting", async () => {
      const manager = makeManager();
      await manager.addCredits("user-1", 100, {
        type: "purchase",
        expiresAt: new Date(T0.getTime() + 1000),
      });

      store.setClock(() => AFTER_EXPIRY);

      // No manual sweepExpiredCredits() call anywhere in this test.
      expect((await manager.getAvailable("user-1")).available.toString()).toBe("0");
    });

    it("canAfford reflects true non-expired spending power", async () => {
      const manager = makeManager();
      await manager.publishPricingFromDict(TEST_CONFIG);
      await manager.addCredits("user-1", 100, {
        type: "purchase",
        expiresAt: new Date(T0.getTime() + 1000),
      });

      store.setClock(() => AFTER_EXPIRY);

      const result = await manager.canAfford("user-1", D(50));
      expect(result.affordable).toBe(false);
      expect(result.spendable.toString()).toBe("0");
    });

    it("deduct never sees the expired grant — insufficient credits once it's gone", async () => {
      const manager = makeManager();
      await manager.publishPricingFromDict(TEST_CONFIG);
      // 50 permanent + 100 expiring; after expiry only 50 remains.
      await manager.addCredits("user-1", 50, { type: "purchase" });
      await manager.addCredits("user-1", 100, {
        type: "purchase",
        expiresAt: new Date(T0.getTime() + 1000),
      });

      store.setClock(() => AFTER_EXPIRY);

      // Requesting more than the 50 that legitimately remains fails —
      // the expired 100 was never available to cover it.
      await expect(manager.deduct("user-1", { inputTokens: 80 })).rejects.toThrow(
        InsufficientCreditsError,
      );

      // The 50 that legitimately remains is still spendable.
      const result = await manager.deduct("user-1", { inputTokens: 50 });
      expect(result.balanceAfter.toString()).toBe("0");
    });

    it("reserve never admits a hold sized against an already-expired grant", async () => {
      const manager = makeManager();
      await manager.addCredits("user-1", 50, { type: "purchase" });
      await manager.addCredits("user-1", 100, {
        type: "purchase",
        expiresAt: new Date(T0.getTime() + 1000),
      });

      store.setClock(() => AFTER_EXPIRY);

      await expect(manager.reserve("user-1", D(80))).rejects.toThrow(InsufficientCreditsError);

      // A hold within the legitimately-remaining balance still succeeds.
      const lease = await manager.reserve("user-1", D(50));
      expect(lease.error).toBeUndefined();
    });

    it("settle also sees the swept-down balance (via the lease path)", async () => {
      const manager = makeManager();
      await manager.addCredits("user-1", 50, { type: "purchase" });
      await manager.addCredits("user-1", 100, {
        type: "purchase",
        expiresAt: new Date(T0.getTime() + 5000),
      });

      // Reserve while the expiring grant is still active. A long TTL ensures
      // the lease itself is still active a day later (only the credit GRANT
      // expires quickly in this test, not the lease hold).
      const lease = await manager.reserve("user-1", D(120), { ttl: 200_000 });
      expect(lease.error).toBeUndefined();

      // Advance past expiry, then settle for the full worst-case amount —
      // the now-expired 100 is gone, so settling for anything the balance
      // (post-sweep) can't cover goes negative in strict mode... instead
      // assert the sweep ran by checking the balance is what remains.
      store.setClock(() => AFTER_EXPIRY);
      await manager.settle("user-1", lease.leaseId, D(10));
      expect((await manager.getBalance("user-1")).balance.toString()).toBe("40");
    });

    it("emits credits.expired for the acting user (not 'system') on a lazy sweep", async () => {
      const emitter = new CreditEventEmitter();
      const manager = makeManager(emitter);
      const events: CreditEvent[] = [];
      emitter.on("credits.expired", (e) => events.push(e));

      await manager.addCredits("user-1", 100, {
        type: "purchase",
        expiresAt: new Date(T0.getTime() + 1000),
      });
      store.setClock(() => AFTER_EXPIRY);

      await manager.getBalance("user-1");

      expect(events).toHaveLength(1);
      expect(events[0].userId).toBe("user-1");
      expect((events[0].data?.expiredAmount as Decimal).toString()).toBe("100");
    });

    it("the public sweepExpiredCredits() remains a global sweep, unaffected by lazyExpiry", async () => {
      const manager = makeManager();
      await manager.addCredits("user-1", 100, {
        type: "purchase",
        expiresAt: new Date(T0.getTime() + 1000),
      });
      await manager.addCredits("user-2", 200, {
        type: "purchase",
        expiresAt: new Date(T0.getTime() + 1000),
      });
      store.setClock(() => AFTER_EXPIRY);

      // A getBalance("user-1") call would already lazily sweep user-1 alone;
      // call the explicit global sweep directly instead to prove it still
      // covers every user in one call.
      const sweep = await manager.sweepExpiredCredits();
      expect(sweep.expiredCount).toBe(2);
      expect((await manager.getBalance("user-2")).balance.toString()).toBe("0");
    });

    it("no-op (and does not throw) for a user with nothing expired", async () => {
      const manager = makeManager();
      await manager.addCredits("user-1", 10, { type: "purchase" }); // never expires
      store.setClock(() => AFTER_EXPIRY);

      const balance = await manager.getBalance("user-1");
      expect(balance.balance.toString()).toBe("10");
    });
  });
});
