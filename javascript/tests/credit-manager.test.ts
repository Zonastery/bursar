import { describe, it, expect, beforeEach } from "vitest";
import Decimal from "decimal.js";
import { CreditManager } from "../src/manager.js";
import { MemoryStore } from "../src/stores/memory-store.js";
import { CreditEventEmitter } from "../src/stores/events.js";
import type { CreditEvent } from "../src/stores/events.js";
import {
  CapReachedError,
  ConfigError,
  FeatureLimitReachedError,
  InsufficientCreditsError,
  PricingNotLoadedError,
  RefundError,
} from "../src/errors.js";
import type { PricingConfigData } from "../src/types.js";
import { resolveAllowanceWindow } from "../src/allowance.js";

const TEST_CONFIG: PricingConfigData = {
  models: {
    "gpt-4": "input_tokens * (10 / 1000) + output_tokens * (30 / 1000)",
  },
  tools: {
    _default: "tool_calls * 5 / 1000",
  },
};

/** A fixed clock for deterministic time-dependent tests (contract §8). */
const FIXED_NOW = new Date("2026-06-15T12:00:00.000Z");

/** Helper: collect events of any type into an array. */
function record(emitter: CreditEventEmitter, types: string[]): CreditEvent[] {
  const events: CreditEvent[] = [];
  for (const t of types) {
    emitter.on(t as CreditEvent["type"], (e) => events.push(e));
  }
  return events;
}

/** Exact Decimal equality assertion (no truthiness / `>0` shortcuts). */
function expectDecimal(actual: unknown, expected: Decimal.Value): void {
  expect(actual).toBeInstanceOf(Decimal);
  expect((actual as Decimal).eq(expected)).toBe(true);
}

describe("CreditManager", () => {
  let store: MemoryStore;
  let manager: CreditManager;

  beforeEach(() => {
    store = new MemoryStore();
    store.setClock(() => FIXED_NOW);
    manager = new CreditManager(store);
  });

  it("rejects deduct before pricing is loaded", async () => {
    await expect(() =>
      manager.deduct("user-1", { model: "gpt-4", inputTokens: 100 }),
    ).rejects.toThrow(PricingNotLoadedError);
  });

  it("completes full flow: publish pricing → add credits → deduct (no truncation)", async () => {
    await manager.publishPricingFromDict(TEST_CONFIG);
    await manager.addCredits("user-1", 1000);

    const result = await manager.deduct("user-1", {
      model: "gpt-4",
      inputTokens: 100,
      outputTokens: 50,
    });

    expect(result.transactionId).toBeTruthy();
    // Cost: 100 * 0.01 + 50 * 0.03 = 1 + 1.5 = 2.5 — charged EXACTLY (no truncation).
    expectDecimal(result.amount, "2.5");
    expect(result.idempotent).toBe(false);
    expectDecimal(result.balanceAfter, "997.5");

    const balance = await manager.getBalance("user-1");
    expectDecimal(balance.balance, "997.5");
  });

  it("charges a fractional sub-1 cost exactly (0.4, not truncated to 0) — H1", async () => {
    await manager.publishPricingFromDict({ models: { _default: "input_tokens * 0.4" } });
    await manager.addCredits("user-1", 100);

    const result = await manager.deduct("user-1", { inputTokens: 1 });
    expectDecimal(result.amount, "0.4");
    expectDecimal(result.balanceAfter, "99.6");

    const balance = await manager.getBalance("user-1");
    expectDecimal(balance.balance, "99.6");
  });

  describe("idempotency", () => {
    it("same key replays the original result (no second debit)", async () => {
      await manager.publishPricingFromDict(TEST_CONFIG);
      await manager.addCredits("user-1", 500);

      const result1 = await manager.deduct("user-1", { model: "gpt-4", inputTokens: 100 }, "idem-1");
      expect(result1.idempotent).toBe(false);
      expectDecimal(result1.amount, "1"); // 100 * 0.01
      expectDecimal(result1.balanceAfter, "499");

      const result2 = await manager.deduct("user-1", { model: "gpt-4", inputTokens: 100 }, "idem-1");
      expect(result2.idempotent).toBe(true);
      expectDecimal(result2.amount, "1");

      // Only ONE debit happened.
      const balance = await manager.getBalance("user-1");
      expectDecimal(balance.balance, "499");
    });

    it("same key + different amount still replays the ORIGINAL charge", async () => {
      await manager.publishPricingFromDict({ models: { _default: "input_tokens * 1" } });
      await manager.addCredits("user-1", 500);

      const first = await manager.deduct("user-1", { inputTokens: 10 }, "idem-2");
      expectDecimal(first.amount, "10");

      // Same key, but a request that would cost 999 — original is replayed.
      const replay = await manager.deduct("user-1", { inputTokens: 999 }, "idem-2");
      expect(replay.idempotent).toBe(true);
      expectDecimal(replay.amount, "10");

      const balance = await manager.getBalance("user-1");
      expectDecimal(balance.balance, "490"); // only the first 10 charged
    });

    it("same key + different user does NOT collide (user-scoped)", async () => {
      await manager.publishPricingFromDict({ models: { _default: "input_tokens * 1" } });
      await manager.addCredits("user-1", 100);
      await manager.addCredits("user-2", 100);

      const r1 = await manager.deduct("user-1", { inputTokens: 10 }, "shared-key");
      const r2 = await manager.deduct("user-2", { inputTokens: 20 }, "shared-key");

      expect(r1.idempotent).toBe(false);
      expect(r2.idempotent).toBe(false); // not a replay of user-1's transaction
      expectDecimal(r1.amount, "10");
      expectDecimal(r2.amount, "20");
      expectDecimal((await manager.getBalance("user-1")).balance, "90");
      expectDecimal((await manager.getBalance("user-2")).balance, "80");
    });
  });

  describe("deduct failure", () => {
    it("throws InsufficientCreditsError AND emits credits.deduct_failed (no success event)", async () => {
      const emitter = new CreditEventEmitter();
      const mgr = new CreditManager(store, undefined, emitter);
      await mgr.publishPricingFromDict(TEST_CONFIG);
      await mgr.addCredits("user-1", 1);

      const events = record(emitter, ["credits.deducted", "credits.deduct_failed"]);

      await expect(() =>
        mgr.deduct("user-1", { model: "gpt-4", inputTokens: 10_000 }),
      ).rejects.toThrow(InsufficientCreditsError);

      expect(events).toHaveLength(1);
      expect(events[0].type).toBe("credits.deduct_failed");
      expect(events[0].data?.error).toBe("insufficient_credits");

      // Balance untouched.
      expectDecimal((await mgr.getBalance("user-1")).balance, "1");
    });
  });

  it("loads pricing from store", async () => {
    await store.setActivePricing(TEST_CONFIG);

    const mgr = new CreditManager(store);
    await mgr.loadPricingFromStore();

    await mgr.addCredits("user-1", 100);
    const result = await mgr.deduct("user-1", { model: "gpt-4", inputTokens: 100 });
    expect(result.transactionId).toBeTruthy();
    expectDecimal(result.amount, "1");
  });

  describe("publishPricing", () => {
    it("updates the engine", async () => {
      await manager.publishPricing(TEST_CONFIG);
      expect(manager.pricingEngine).not.toBeNull();
    });

    it("awaits store persistence (H10) — config is durably set", async () => {
      await manager.publishPricing(TEST_CONFIG, "v1");
      const active = await store.getActivePricing();
      expect(active).not.toBeNull();
      expect(active?.config.models["gpt-4"]).toBe(TEST_CONFIG.models["gpt-4"]);
    });
  });

  describe("deductFixed", () => {
    it("charges the configured fixed cost exactly", async () => {
      const config: PricingConfigData = {
        models: { _default: "input_tokens * 1" },
        fixed: { batch_job: 50 },
      };
      await manager.publishPricingFromDict(config);
      await manager.addCredits("user-1", 100);

      const result = await manager.deductFixed("user-1", "batch_job");
      expect(result.transactionId).toBeTruthy();
      expectDecimal(result.amount, "50");
      expectDecimal(result.balanceAfter, "50");
    });

    it("rejects an unknown job instead of charging 0 (L1)", async () => {
      const config: PricingConfigData = {
        models: { _default: "input_tokens * 1" },
        fixed: { batch_job: 50 },
      };
      await manager.publishPricingFromDict(config);
      await manager.addCredits("user-1", 100);

      await expect(() => manager.deductFixed("user-1", "does_not_exist")).rejects.toThrow(
        ConfigError,
      );
      // Nothing charged.
      expectDecimal((await manager.getBalance("user-1")).balance, "100");
    });

    it("rejects deductFixed before pricing is loaded", async () => {
      await expect(() => manager.deductFixed("user-1", "batch_job")).rejects.toThrow(
        PricingNotLoadedError,
      );
    });
  });

  it("tracks balance correctly across multiple operations (no truncation)", async () => {
    await manager.publishPricingFromDict(TEST_CONFIG);
    await manager.addCredits("user-1", 1000);

    // 100*0.01 + 50*0.03 = 2.5
    await manager.deduct("user-1", { model: "gpt-4", inputTokens: 100, outputTokens: 50 });
    expectDecimal((await manager.getBalance("user-1")).balance, "997.5");

    // 200*0.01 + 100*0.03 = 5
    await manager.deduct("user-1", { model: "gpt-4", inputTokens: 200, outputTokens: 100 });
    expectDecimal((await manager.getBalance("user-1")).balance, "992.5");

    // purchase
    await manager.addCredits("user-1", 500, { type: "purchase" });
    const balance = await manager.getBalance("user-1");
    expectDecimal(balance.balance, "1492.5");
    expectDecimal(balance.lifetimePurchased, "500");
  });

  describe("plan allowance", () => {
    it("fully covers cost with plan allowance, skipping balance deduct", async () => {
      const config: PricingConfigData = {
        models: { _default: "input_tokens * 1" },
        plans: { "plan-free": { id: "plan-free", name: "Free", freeAllowance: new Decimal(100) } },
      };
      await store.setActivePricing(config);
      await store.setUserPlan("user-1", "plan-free");

      const mgr = new CreditManager(store);
      await mgr.publishPricingFromDict(config);
      await mgr.addCredits("user-1", 10);

      // Deduct 5 — fully covered by allowance, net 0.
      const result = await mgr.deduct("user-1", { inputTokens: 5 });
      expectDecimal(result.amount, "0");
      expectDecimal(result.allowanceConsumed, "5");
      expect(result.transactionId).toBeTruthy(); // atomic ledger row exists

      expectDecimal((await mgr.getBalance("user-1")).balance, "10"); // unchanged
      expectDecimal((await store.checkAllowance("user-1")).allowanceRemaining, "95");
    });

    it("partially covers cost with plan allowance, deducts remainder from balance", async () => {
      const config: PricingConfigData = {
        models: { _default: "input_tokens * 1" },
        plans: { "plan-starter": { id: "plan-starter", name: "Starter", freeAllowance: new Decimal(10) } },
      };
      await store.setActivePricing(config);
      await store.setUserPlan("user-1", "plan-starter");

      const mgr = new CreditManager(store);
      await mgr.publishPricingFromDict(config);
      await mgr.addCredits("user-1", 100);

      // Deduct 25 — 10 from allowance, 15 net from balance.
      const result = await mgr.deduct("user-1", { inputTokens: 25 });
      expectDecimal(result.amount, "15");
      expectDecimal(result.allowanceConsumed, "10");

      expectDecimal((await mgr.getBalance("user-1")).balance, "85");
      expectDecimal((await store.checkAllowance("user-1")).allowanceRemaining, "0");
    });

    it("no plan uses balance-only deduct flow", async () => {
      await manager.publishPricingFromDict(TEST_CONFIG);
      await manager.addCredits("user-1", 100);

      const result = await manager.deduct("user-1", {
        model: "gpt-4",
        inputTokens: 100,
        outputTokens: 50,
      });
      expectDecimal(result.amount, "2.5");
      expectDecimal(result.allowanceConsumed, "0");
    });

    it("checkFeature through manager distinguishes presence from truthiness", async () => {
      const config: PricingConfigData = {
        models: { _default: "input_tokens * 1" },
        plans: {
          premium: {
            id: "premium",
            name: "Premium",
            freeAllowance: new Decimal(2000),
            features: { aiChat: true, maxRoadmaps: 20, freeSeats: 0, label: "" },
          },
        },
      };
      await store.setActivePricing(config);
      await store.setUserPlan("user-1", "premium");

      const mgr = new CreditManager(store);
      await mgr.publishPricingFromDict(config);

      const chat = await mgr.checkFeature("user-1", "aiChat");
      expect(chat.hasFeature).toBe(true);
      expect(chat.value).toBe(true);

      expect((await mgr.checkFeature("user-1", "maxRoadmaps")).value).toBe(20);

      // numeric 0 / "" ⇒ present (contract §5 / M6)
      const seats = await mgr.checkFeature("user-1", "freeSeats");
      expect(seats.value).toBe(0);
      expect(seats.hasFeature).toBe(true);
      const label = await mgr.checkFeature("user-1", "label");
      expect(label.hasFeature).toBe(true);

      expect((await mgr.checkFeature("user-1", "exportPdf")).hasFeature).toBe(false);
      expect((await mgr.checkFeature("nobody", "aiChat")).hasFeature).toBe(false);
    });
  });

  describe("refunds", () => {
    it("refunds a full deduction through manager", async () => {
      const emitter = new CreditEventEmitter();
      const mgr = new CreditManager(store, undefined, emitter);
      await mgr.publishPricingFromDict(TEST_CONFIG);
      await mgr.addCredits("user-1", 1000);

      const events = record(emitter, ["credits.refunded", "credits.refund_failed"]);

      const deduct = await mgr.deduct("user-1", { model: "gpt-4", inputTokens: 100 });
      expectDecimal(deduct.amount, "1");

      const refund = await mgr.refundCredits(deduct.transactionId);
      expect(refund.error).toBeFalsy();
      expectDecimal(refund.amount, "1");
      expectDecimal((await mgr.getBalance("user-1")).balance, "1000");

      expect(events).toHaveLength(1);
      expect(events[0].type).toBe("credits.refunded");
    });

    it("partial refund through manager", async () => {
      await manager.publishPricingFromDict({ models: { _default: "input_tokens * 1" } });
      await manager.addCredits("user-1", 100);

      const deduct = await manager.deduct("user-1", { inputTokens: 50 });
      const refund = await manager.refundCredits(deduct.transactionId, 20);
      expect(refund.error).toBeFalsy();
      expectDecimal(refund.amount, "20");
      expectDecimal((await manager.getBalance("user-1")).balance, "70"); // 100 - 50 + 20
    });

    it("duplicate (over-)refund throws RefundError and emits refund_failed (NO success event)", async () => {
      const emitter = new CreditEventEmitter();
      const mgr = new CreditManager(store, undefined, emitter);
      await mgr.publishPricingFromDict({ models: { _default: "input_tokens * 1" } });
      await mgr.addCredits("user-1", 100);

      const deduct = await mgr.deduct("user-1", { inputTokens: 40 });
      const first = await mgr.refundCredits(deduct.transactionId);
      expect(first.error).toBeFalsy();

      const events = record(emitter, ["credits.refunded", "credits.refund_failed"]);

      await expect(() => mgr.refundCredits(deduct.transactionId)).rejects.toThrow(RefundError);

      // Only the failure event fired — no false success.
      expect(events).toHaveLength(1);
      expect(events[0].type).toBe("credits.refund_failed");
      expect(events[0].data?.error).toBe("already_refunded");

      // Balance unchanged by the failed refund.
      expectDecimal((await mgr.getBalance("user-1")).balance, "100");
    });

    it("over-refund (amount > remaining) throws RefundError", async () => {
      await manager.publishPricingFromDict({ models: { _default: "input_tokens * 1" } });
      await manager.addCredits("user-1", 100);

      const deduct = await manager.deduct("user-1", { inputTokens: 30 });
      await expect(() => manager.refundCredits(deduct.transactionId, 999)).rejects.toThrow(
        RefundError,
      );
      expectDecimal((await manager.getBalance("user-1")).balance, "70"); // unchanged
    });

    it("refund of a purchase (not a debit) is rejected", async () => {
      await manager.publishPricingFromDict({ models: { _default: "input_tokens * 1" } });
      const add = await manager.addCredits("user-1", 100, { type: "purchase" });
      await expect(() => manager.refundCredits(add.transactionId)).rejects.toThrow(RefundError);
    });

    it("refund of unknown transaction throws RefundError", async () => {
      await manager.publishPricingFromDict({ models: { _default: "input_tokens * 1" } });
      await expect(() => manager.refundCredits("no-such-transaction")).rejects.toThrow(RefundError);
    });
  });

  describe("usage analytics", () => {
    it("aggregateStats returns aggregate data through manager", async () => {
      await manager.publishPricingFromDict(TEST_CONFIG);
      await manager.addCredits("user-1", 500);
      await manager.addCredits("user-2", 500);
      await manager.deduct("user-1", { model: "gpt-4", inputTokens: 100 });
      await manager.deduct("user-2", { model: "gpt-4", inputTokens: 50 });

      const stats = await manager.aggregateStats(
        new Date(FIXED_NOW.getTime() - 1000),
        new Date(FIXED_NOW.getTime() + 1000),
      );
      // user-1: 1 credit, user-2: 0.5 — total 1.5 exactly.
      expectDecimal(stats.totalCreditsConsumed, "1.5");
      expect(stats.activeUsers).toBe(2);
      expect(stats.topUser).toBe("user-1");
    });

    it("aggregateStats returns empty stats for empty window", async () => {
      const stats = await manager.aggregateStats(new Date("2020-01-01"), new Date("2020-01-02"));
      expectDecimal(stats.totalCreditsConsumed, "0");
      expect(stats.activeUsers).toBe(0);
      expect(stats.topModel).toBe("");
    });

    it("spendByUser delegates to store and returns exact results", async () => {
      await manager.publishPricingFromDict(TEST_CONFIG);
      await manager.addCredits("user-1", 500);
      await manager.deduct("user-1", { model: "gpt-4", inputTokens: 100 });

      const rows = await manager.spendByUser(
        new Date(FIXED_NOW.getTime() - 1000),
        new Date(FIXED_NOW.getTime() + 1000),
      );
      expect(rows).toHaveLength(1);
      expect(rows[0].userId).toBe("user-1");
      expectDecimal(rows[0].totalSpend, "1");
    });

    it("spendByModel returns results through manager", async () => {
      await manager.publishPricingFromDict(TEST_CONFIG);
      await manager.addCredits("user-1", 500);
      await manager.deduct("user-1", { model: "gpt-4", inputTokens: 100 });

      const rows = await manager.spendByModel(
        new Date(FIXED_NOW.getTime() - 1000),
        new Date(FIXED_NOW.getTime() + 1000),
      );
      expect(rows).toHaveLength(1);
      expect(rows[0].model).toBe("gpt-4");
      expectDecimal(rows[0].totalSpend, "1");
    });

    it("topUsers returns top users through manager", async () => {
      await manager.publishPricingFromDict(TEST_CONFIG);
      await manager.addCredits("user-1", 500);
      await manager.deduct("user-1", { model: "gpt-4", inputTokens: 100 });

      const rows = await manager.topUsers(
        5,
        new Date(FIXED_NOW.getTime() - 1000),
        new Date(FIXED_NOW.getTime() + 1000),
      );
      expect(rows).toHaveLength(1);
      expect(rows[0].userId).toBe("user-1");
    });

    it("dailySpend returns bucketed results through manager", async () => {
      await manager.publishPricingFromDict(TEST_CONFIG);
      await manager.addCredits("user-1", 500);
      await manager.deduct("user-1", { model: "gpt-4", inputTokens: 100 });

      const rows = await manager.dailySpend(
        new Date(FIXED_NOW.getTime() - 86400000),
        new Date(FIXED_NOW.getTime() + 86400000),
      );
      expect(rows).toHaveLength(1);
      expectDecimal(rows[0].totalSpend, "1");
    });
  });

  describe("credit expiry", () => {
    it("sweepExpiredCredits delegates to store (fixed clock, no sleep)", async () => {
      await manager.publishPricingFromDict(TEST_CONFIG);
      // Grant expiring 1 minute before the fixed clock.
      await manager.addCredits("user-1", 100, {
        type: "purchase",
        expiresAt: new Date(FIXED_NOW.getTime() - 60_000),
      });

      const result = await manager.sweepExpiredCredits();
      expect(result.expiredCount).toBe(1);
      expectDecimal(result.expiredAmount, "100");
      expectDecimal((await manager.getBalance("user-1")).balance, "0");
    });

    it("dryRun through manager reports without modifying", async () => {
      await manager.publishPricingFromDict(TEST_CONFIG);
      await manager.addCredits("user-1", 100, {
        type: "purchase",
        expiresAt: new Date(FIXED_NOW.getTime() - 60_000),
      });

      const result = await manager.sweepExpiredCredits(true);
      expect(result.expiredCount).toBe(1);
      expect(result.dryRun).toBe(true);
      expectDecimal((await manager.getBalance("user-1")).balance, "100");
    });

    it("credits without expiry never expire through manager", async () => {
      await manager.publishPricingFromDict(TEST_CONFIG);
      await manager.addCredits("user-1", 100);

      const result = await manager.sweepExpiredCredits();
      expect(result.expiredCount).toBe(0);
      expectDecimal(result.expiredAmount, "0");
      expectDecimal((await manager.getBalance("user-1")).balance, "100");
    });

    it("double-sweep reports zero the second time (H4)", async () => {
      await manager.publishPricingFromDict(TEST_CONFIG);
      await manager.addCredits("user-1", 100, {
        type: "purchase",
        expiresAt: new Date(FIXED_NOW.getTime() - 60_000),
      });

      const first = await manager.sweepExpiredCredits();
      expect(first.expiredCount).toBe(1);
      const second = await manager.sweepExpiredCredits();
      expect(second.expiredCount).toBe(0);
      expectDecimal((await manager.getBalance("user-1")).balance, "0");
    });
  });

  describe("team balance pools", () => {
    it("deductTeam calculates cost and debits team pool", async () => {
      const mgr = new CreditManager(store);
      await mgr.publishPricingFromDict({ models: { _default: "input_tokens * 1" } });

      const team = await store.createTeam("Team", new Decimal(500));
      await store.addTeamMember(team.teamId, "user-1", "member");

      const result = await mgr.deductTeam(team.teamId, "user-1", { inputTokens: 100 });
      expectDecimal(result.amount, "-100"); // team store returns negated ledger amount
      expectDecimal(result.teamBalanceAfter, "400");
      expect(result.transactionId).toBeTruthy();
    });

    it("deductTeam zero-cost returns without deducting", async () => {
      const mgr = new CreditManager(store);
      await mgr.publishPricingFromDict({ models: { _default: "input_tokens * 1" } });

      const team = await store.createTeam("Team", new Decimal(500));
      await store.addTeamMember(team.teamId, "user-1", "member");

      const result = await mgr.deductTeam(team.teamId, "user-1", { inputTokens: 0 });
      expectDecimal(result.amount, "0");
      expectDecimal(result.teamBalanceAfter, "500");
    });

    it("deductTeam threads idempotencyKey (H12) — retry does not double-charge", async () => {
      const mgr = new CreditManager(store);
      await mgr.publishPricingFromDict({ models: { _default: "input_tokens * 1" } });

      const team = await store.createTeam("Team", new Decimal(500));
      await store.addTeamMember(team.teamId, "user-1", "member");

      const first = await mgr.deductTeam(team.teamId, "user-1", { inputTokens: 100 }, "team-idem-1");
      expectDecimal(first.teamBalanceAfter, "400");

      const replay = await mgr.deductTeam(
        team.teamId,
        "user-1",
        { inputTokens: 100 },
        "team-idem-1",
      );
      // Replay returns the original transaction, balance not debited again.
      expect(replay.transactionId).toBe(first.transactionId);
      expectDecimal((await store.getTeamBalance(team.teamId)).balance, "400");
    });

    it("deductTeam throws without pricing loaded", async () => {
      const mgr = new CreditManager(store);
      await expect(() => mgr.deductTeam("team-1", "user-1", { inputTokens: 100 })).rejects.toThrow(
        PricingNotLoadedError,
      );
    });

    it("deductTeam insufficient balance throws InsufficientCreditsError", async () => {
      // H2 fix: manager now throws on store error rather than returning a
      // silent error object — mirrors Python manager.py:1069-1082.
      const mgr = new CreditManager(store);
      await mgr.publishPricingFromDict({ models: { _default: "input_tokens * 1" } });

      const team = await store.createTeam("Poor Team", new Decimal(10));
      await store.addTeamMember(team.teamId, "user-1", "member");

      await expect(mgr.deductTeam(team.teamId, "user-1", { inputTokens: 100 })).rejects.toThrow(
        InsufficientCreditsError,
      );
    });

    it("deductTeam user not in team throws InsufficientCreditsError", async () => {
      const mgr = new CreditManager(store);
      await mgr.publishPricingFromDict({ models: { _default: "input_tokens * 1" } });

      const team = await store.createTeam("Closed Team", new Decimal(500));
      await expect(mgr.deductTeam(team.teamId, "user-1", { inputTokens: 10 })).rejects.toThrow(
        InsufficientCreditsError,
      );
    });

    it("team balance reflects deductions through manager", async () => {
      const mgr = new CreditManager(store);
      await mgr.publishPricingFromDict({ models: { _default: "input_tokens * 1" } });

      const team = await store.createTeam("Pipeline Team", new Decimal(500));
      await store.addTeamMember(team.teamId, "user-1", "member");

      const result = await mgr.deductTeam(team.teamId, "user-1", { inputTokens: 150 });
      expectDecimal(result.teamBalanceAfter, "350");
      expectDecimal((await store.getTeamBalance(team.teamId)).balance, "350");
    });
  });

  describe("spend caps", () => {
    it("daily deny cap throws CapReachedError and emits deduct_failed", async () => {
      const emitter = new CreditEventEmitter();
      const mgr = new CreditManager(store, undefined, emitter);
      await mgr.publishPricingFromDict({ models: { _default: "input_tokens * 1" } });
      await mgr.addCredits("user-1", 1000);
      store.setSpendCap({ userId: "user-1", type: "daily", limit: new Decimal(10), action: "deny" });

      const events = record(emitter, ["credits.deducted", "credits.deduct_failed"]);

      // 11 credits exceed cap of 10 → deny.
      await expect(() => mgr.deduct("user-1", { inputTokens: 11 })).rejects.toThrow(CapReachedError);

      expect(events).toHaveLength(1);
      expect(events[0].type).toBe("credits.deduct_failed");
      expect(events[0].data?.error).toBe("cap_reached");

      // No allowance consumed, no balance change.
      expectDecimal((await mgr.getBalance("user-1")).balance, "1000");
    });

    it("warn action allows deduction through and emits cap_warning", async () => {
      const emitter = new CreditEventEmitter();
      const mgr = new CreditManager(store, undefined, emitter);
      await mgr.publishPricingFromDict({ models: { _default: "input_tokens * 1" } });
      await mgr.addCredits("user-1", 1000);
      store.setSpendCap({ userId: "user-1", type: "daily", limit: new Decimal(10), action: "warn" });

      const events = record(emitter, ["credits.deducted", "credits.cap_warning"]);

      const result = await mgr.deduct("user-1", { inputTokens: 11 });
      expectDecimal(result.amount, "11");
      expectDecimal(result.balanceAfter, "989");

      const types = events.map((e) => e.type).sort();
      expect(types).toEqual(["credits.cap_warning", "credits.deducted"]);
    });

    it("notify action allows deduction through", async () => {
      const mgr = new CreditManager(store);
      await mgr.publishPricingFromDict({ models: { _default: "input_tokens * 1" } });
      await mgr.addCredits("user-1", 1000);
      store.setSpendCap({
        userId: "user-1",
        type: "daily",
        limit: new Decimal(10),
        action: "notify",
      });

      const result = await mgr.deduct("user-1", { inputTokens: 11 });
      expect(result.capWarning).toBe("notify");
      expectDecimal(result.amount, "11");
    });

    it("cap accumulates across prior window spend (no TOCTOU bypass, H2)", async () => {
      const mgr = new CreditManager(store);
      await mgr.publishPricingFromDict({ models: { _default: "input_tokens * 1" } });
      await mgr.addCredits("user-1", 1000);
      store.setSpendCap({
        userId: "user-1",
        type: "daily",
        limit: new Decimal(10),
        action: "deny",
      });

      // First 6 within cap.
      await mgr.deduct("user-1", { inputTokens: 6 });
      // Next 5 would total 11 > 10 → denied (prior spend counts).
      await expect(() => mgr.deduct("user-1", { inputTokens: 5 })).rejects.toThrow(CapReachedError);
      expectDecimal((await mgr.getBalance("user-1")).balance, "994"); // only first 6 charged
    });

    it("spend cap does not affect deductions within limit", async () => {
      const mgr = new CreditManager(store);
      await mgr.publishPricingFromDict({ models: { _default: "input_tokens * 1" } });
      await mgr.addCredits("user-1", 1000);
      store.setSpendCap({
        userId: "user-1",
        type: "daily",
        limit: new Decimal(100),
        action: "deny",
      });

      const result = await mgr.deduct("user-1", { inputTokens: 5 });
      expectDecimal(result.amount, "5");
      expect(result.capWarning).toBeNull();
    });
  });

  describe("low_balance event", () => {
    it("fires once when a deduction crosses the threshold (edge-triggered, M18)", async () => {
      const emitter = new CreditEventEmitter();
      // Explicit minBalance: 5 → threshold = 10 (WS6: the implicit default is now 0).
      const mgr = new CreditManager(store, undefined, emitter);
      await mgr.publishPricingFromDict({ models: { _default: "input_tokens * 1" }, minBalance: 5 });
      await mgr.addCredits("user-1", 12);

      const events = record(emitter, ["credits.low_balance"]);

      // 12 → 9 crosses 10 → fires.
      const r1 = await mgr.deduct("user-1", { inputTokens: 3 });
      expectDecimal(r1.balanceAfter, "9");
      expect(events).toHaveLength(1);
      const ev = events[0];
      expectDecimal(ev.data?.balance, "9");
      expectDecimal(ev.data?.threshold, "10");

      // 9 → 7 already below threshold → does NOT fire again (edge-triggered).
      await mgr.deduct("user-1", { inputTokens: 2 });
      expect(events).toHaveLength(1);
    });

    it("does not fire when the balance stays above the threshold", async () => {
      const emitter = new CreditEventEmitter();
      const mgr = new CreditManager(store, undefined, emitter);
      await mgr.publishPricingFromDict({ models: { _default: "input_tokens * 1" } });
      await mgr.addCredits("user-1", 100);

      const events = record(emitter, ["credits.low_balance"]);
      const r = await mgr.deduct("user-1", { inputTokens: 5 }); // 100 → 95
      expectDecimal(r.balanceAfter, "95");
      expect(events).toHaveLength(0);
    });

    it("honours a configurable absolute threshold", async () => {
      const emitter = new CreditEventEmitter();
      const mgr = new CreditManager(store, undefined, emitter, {
        lowBalance: { thresholds: [new Decimal(50)] },
      });
      await mgr.publishPricingFromDict({ models: { _default: "input_tokens * 1" } });
      await mgr.addCredits("user-1", 60);

      const events = record(emitter, ["credits.low_balance"]);
      // 60 → 45 crosses 50.
      await mgr.deduct("user-1", { inputTokens: 15 });
      expect(events).toHaveLength(1);
      expectDecimal(events[0].data?.threshold, "50");
    });

    it("does not fire on an idempotent replay (balance did not move)", async () => {
      const emitter = new CreditEventEmitter();
      const mgr = new CreditManager(store, undefined, emitter);
      // Explicit minBalance: 5 → threshold = 10 (WS6: the implicit default is now 0).
      await mgr.publishPricingFromDict({ models: { _default: "input_tokens * 1" }, minBalance: 5 });
      await mgr.addCredits("user-1", 12);

      const events = record(emitter, ["credits.low_balance"]);
      await mgr.deduct("user-1", { inputTokens: 3 }, "lb-key"); // 12 → 9, fires once
      expect(events).toHaveLength(1);
      await mgr.deduct("user-1", { inputTokens: 3 }, "lb-key"); // replay, no move
      expect(events).toHaveLength(1);
    });
  });

  describe("event system", () => {
    it("emits credits.deducted with Decimal money payload", async () => {
      const emitter = new CreditEventEmitter();
      const mgr = new CreditManager(store, undefined, emitter);
      await mgr.publishPricingFromDict({ models: { _default: "input_tokens * 1" } });
      await mgr.addCredits("user-1", 100);

      const events = record(emitter, ["credits.deducted"]);
      await mgr.deduct("user-1", { inputTokens: 10 });

      expect(events).toHaveLength(1);
      expect(events[0].userId).toBe("user-1");
      expectDecimal(events[0].data?.amount, "10");
      expectDecimal(events[0].data?.balanceAfter, "90");
    });

    it("credits.added event includes Decimal amount and newBalance", async () => {
      const emitter = new CreditEventEmitter();
      const mgr = new CreditManager(new MemoryStore(), undefined, emitter);

      const events = record(emitter, ["credits.added"]);
      await mgr.publishPricingFromDict({ models: { _default: "input_tokens * 1" } });
      await mgr.addCredits("user-1", 50);

      expect(events).toHaveLength(1);
      expectDecimal(events[0].data?.amount, "50");
    });

    it("emits credits.expired event on sweep", async () => {
      const emitter = new CreditEventEmitter();
      const s = new MemoryStore();
      s.setClock(() => FIXED_NOW);
      const mgr = new CreditManager(s, undefined, emitter);
      await mgr.publishPricingFromDict({ models: { _default: "input_tokens * 1" } });

      const events = record(emitter, ["credits.expired"]);
      await mgr.addCredits("user-1", 100, {
        type: "purchase",
        expiresAt: new Date(FIXED_NOW.getTime() - 60_000),
      });
      await mgr.sweepExpiredCredits();

      expect(events).toHaveLength(1);
      expect(events[0].data?.expiredCount).toBe(1);
    });

    it("multiple handlers all fire for same event", async () => {
      const emitter = new CreditEventEmitter();
      const mgr = new CreditManager(store, undefined, emitter);

      const called: number[] = [];
      emitter.on("credits.deducted", () => called.push(1));
      emitter.on("credits.deducted", () => called.push(2));

      await mgr.publishPricingFromDict({ models: { _default: "input_tokens * 1" } });
      await mgr.addCredits("user-1", 100);
      await mgr.deduct("user-1", { inputTokens: 10 });

      expect(called).toEqual([1, 2]);
    });

    it("a throwing handler is isolated and does not break the flow", async () => {
      const emitter = new CreditEventEmitter();
      const mgr = new CreditManager(store, undefined, emitter);

      emitter.on("credits.deducted", () => {
        throw new Error("boom");
      });
      const seen: string[] = [];
      emitter.on("credits.deducted", () => seen.push("ok"));

      await mgr.publishPricingFromDict({ models: { _default: "input_tokens * 1" } });
      await mgr.addCredits("user-1", 100);

      // The deduction still succeeds despite the throwing handler.
      const result = await mgr.deduct("user-1", { inputTokens: 10 });
      expectDecimal(result.amount, "10");
      expect(seen).toEqual(["ok"]);
    });

    it("a rejecting async handler does not produce an unhandled rejection", async () => {
      const emitter = new CreditEventEmitter();
      emitter.on("credits.deducted", async () => {
        await Promise.resolve();
        throw new Error("async boom");
      });
      // emit must not throw synchronously nor surface the rejection.
      expect(() =>
        emitter.emit({ type: "credits.deducted", timestamp: new Date(), userId: "u1" }),
      ).not.toThrow();
    });

    it("unregistered event type does not throw", () => {
      const emitter = new CreditEventEmitter();
      expect(() =>
        emitter.emit({ type: "credits.deducted", timestamp: new Date(), userId: "u1" }),
      ).not.toThrow();
    });

    // MG1 — credits.plan_changed fires on setUserPlan
    it("MG1: credits.plan_changed fires on setUserPlan and plan is updated", async () => {
      const emitter = new CreditEventEmitter();
      const config: PricingConfigData = {
        models: { _default: "input_tokens * 1" },
        plans: {
          pro: { id: "pro", name: "Pro", freeAllowance: new Decimal(100) },
        },
      };
      await store.setActivePricing(config);
      const mgr = new CreditManager(store, undefined, emitter);
      await mgr.publishPricingFromDict(config);

      const events = record(emitter, ["credits.plan_changed"]);

      await mgr.setUserPlan("user-1", "pro");

      // (a) getUserPlan returns the new plan
      const plan = await mgr.getUserPlan("user-1");
      expect(plan.planId).toBe("pro");

      // (b) credits.plan_changed event was emitted with correct payload
      expect(events).toHaveLength(1);
      expect(events[0].type).toBe("credits.plan_changed");
      expect(events[0].userId).toBe("user-1");
      expect(events[0].data?.userId).toBe("user-1");
      expect(events[0].data?.planKey).toBe("pro");
      expect(typeof events[0].data?.timestamp).toBe("string");
    });

    // MG3 — cap_warning AND credits.deducted both fire
    it("MG3: cap_warning and credits.deducted both fire on a warn-capped deduction", async () => {
      const emitter = new CreditEventEmitter();
      const mgr = new CreditManager(store, undefined, emitter);
      await mgr.publishPricingFromDict({ models: { _default: "input_tokens * 1" } });
      await mgr.addCredits("user-1", 1000);
      store.setSpendCap({
        userId: "user-1",
        type: "daily",
        limit: new Decimal(5),
        action: "warn",
      });

      const events = record(emitter, ["credits.deducted", "credits.cap_warning"]);

      // 10 > 5 → triggers the warn cap but deduction succeeds
      await mgr.deduct("user-1", { inputTokens: 10 });

      const types = events.map((e) => e.type).sort();
      expect(types).toContain("credits.deducted");
      expect(types).toContain("credits.cap_warning");
    });

    // MG4 — deductTeam with zero cost
    it("MG4: deductTeam with zero cost succeeds without debiting the pool and does not emit credits.deducted", async () => {
      const emitter = new CreditEventEmitter();
      const mgr = new CreditManager(store, undefined, emitter);
      // Zero-rate model: cost is always 0
      await mgr.publishPricingFromDict({ models: { _default: "input_tokens * 0" } });

      const team = await store.createTeam("ZeroTeam", new Decimal(500));
      await store.addTeamMember(team.teamId, "user-1", "member");

      const events = record(emitter, ["credits.deducted"]);

      // Any non-zero inputTokens with zero rate produces cost=0
      const result = await mgr.deductTeam(team.teamId, "user-1", { inputTokens: 100 });

      // No debit from pool
      expectDecimal(result.amount, "0");
      expectDecimal((await store.getTeamBalance(team.teamId)).balance, "500");
      expect(result.error).toBeFalsy();

      // Implementation short-circuits on zero cost with no event emission
      expect(events).toHaveLength(0);
    });

    // MG6 — Low balance edge-triggered: fires once, not on every deduct below threshold
    it("MG6: credits.low_balance fires exactly once, not on subsequent deductions already below threshold", async () => {
      const emitter = new CreditEventEmitter();
      // Explicit threshold of 20 to make the test deterministic
      const mgr = new CreditManager(store, undefined, emitter, {
        lowBalance: { thresholds: [new Decimal(20)] },
      });
      await mgr.publishPricingFromDict({ models: { _default: "input_tokens * 1" } });
      await mgr.addCredits("user-1", 25);

      const events = record(emitter, ["credits.low_balance"]);

      // 25 → 15: crosses threshold of 20 → fires
      await mgr.deduct("user-1", { inputTokens: 10 });
      expectDecimal((await mgr.getBalance("user-1")).balance, "15");
      expect(events).toHaveLength(1);

      // 15 → 10: already below threshold → does NOT fire again
      await mgr.deduct("user-1", { inputTokens: 5 });
      expectDecimal((await mgr.getBalance("user-1")).balance, "10");
      expect(events).toHaveLength(1);
    });

    // MG7 — deductFixed with unknown job → ConfigError
    it("MG7: deductFixed with unknown job throws ConfigError", async () => {
      const config: PricingConfigData = {
        models: { _default: "input_tokens * 1" },
        fixed: { known_job: 10 },
      };
      await manager.publishPricingFromDict(config);
      await manager.addCredits("user-1", 100);

      await expect(() =>
        manager.deductFixed("user-1", "nonexistent_job_xyz"),
      ).rejects.toThrow(ConfigError);

      // Balance untouched
      expectDecimal((await manager.getBalance("user-1")).balance, "100");
    });
  });

  // MG2 — Team idempotency key is per-team, not per-user
  describe("MG2: deductTeam idempotency key scope", () => {
    it("same idempotency key for a different user on the same team is a replay (per-team scope)", async () => {
      const mgr = new CreditManager(store);
      await mgr.publishPricingFromDict({ models: { _default: "input_tokens * 1" } });

      const team = await store.createTeam("Team", new Decimal(500));
      await store.addTeamMember(team.teamId, "user-1", "member");
      await store.addTeamMember(team.teamId, "user-2", "member");

      // user-1 charges 50 with key "k1"
      const r1 = await mgr.deductTeam(team.teamId, "user-1", { inputTokens: 50 }, "k1");
      expectDecimal(r1.amount, "-50");
      expectDecimal((await store.getTeamBalance(team.teamId)).balance, "450");

      // user-2 uses the SAME key "k1" — the MemoryStore idempotency lookup is
      // scoped to (teamId + idempotencyKey) without a userId check, so this is
      // treated as a replay of user-1's transaction.
      const r2 = await mgr.deductTeam(team.teamId, "user-2", { inputTokens: 50 }, "k1");
      // replay: no new debit; team balance stays at 450
      expect(r2.transactionId).toBe(r1.transactionId);
      expectDecimal((await store.getTeamBalance(team.teamId)).balance, "450");
    });
  });

  // H2 — Concurrent deduct + refund race
  describe("H2: concurrent deduct and refund do not corrupt balance", () => {
    it("10 concurrent deductions of 1 + 5 concurrent refunds — balance stays ≥ 0", async () => {
      await manager.publishPricingFromDict({ models: { _default: "input_tokens * 1" } });
      await manager.addCredits("user-1", 20);

      // Run 10 deductions (each costs 1) and collect their transaction IDs.
      // MemoryStore is synchronous so all Promise.all tasks run atomically.
      const deductPromises = Array.from({ length: 10 }, () =>
        manager.deduct("user-1", { inputTokens: 1 }),
      );

      // We don't have transaction IDs yet, so launch refunds of 5 of the deductions
      // after the deductions settle (Promise.all resolves left-to-right in order).
      const deductResults = await Promise.all(deductPromises);

      // All deductions must have succeeded or have a typed error — none should throw.
      for (const r of deductResults) {
        expect(r.transactionId).toBeDefined();
      }

      // Refund first 5 deductions concurrently.
      const refundPromises = deductResults.slice(0, 5).map((r) =>
        manager.refundCredits(r.transactionId).catch((err: unknown) => {
          // A typed RefundError is acceptable (e.g. already_refunded) — untyped is not.
          expect(err).toBeInstanceOf(Error);
          return null;
        }),
      );
      await Promise.all(refundPromises);

      const { balance } = await manager.getBalance("user-1");
      expect(balance.gte(0)).toBe(true);
    });
  });

  // H3 — Plan change + deduction consistency
  describe("H3: plan change + deduction consistency", () => {
    it("concurrent plan change and deduction do not throw and leave a non-negative balance", async () => {
      const planConfig = {
        models: { _default: "input_tokens * 1" },
        plans: {
          "plan-a": { id: "plan-a", name: "Plan A", freeAllowance: new Decimal(50) },
          "plan-b": { id: "plan-b", name: "Plan B", freeAllowance: new Decimal(20) },
        },
      };
      await manager.publishPricingFromDict(planConfig);
      await store.setActivePricing(planConfig);
      await store.setUserPlan("user-1", "plan-a");
      await manager.addCredits("user-1", 100);

      // Run a plan change and a deduction concurrently.
      const [, deductResult] = await Promise.all([
        manager.setUserPlan("user-1", "plan-b"),
        manager.deduct("user-1", { inputTokens: 10 }),
      ]);

      // No exception thrown; deduction must have completed.
      expect(deductResult.transactionId).toBeDefined();

      const { balance } = await manager.getBalance("user-1");
      expect(balance.gte(0)).toBe(true);
    });
  });

  // H15 — listUserTransactions passthrough
  describe("H15: listUserTransactions passthrough", () => {
    it("returns paginated transactions with correct types and honours limit", async () => {
      await manager.publishPricingFromDict({ models: { _default: "input_tokens * 1" } });
      await manager.addCredits("user-1", 500);

      // Create 5 usage transactions.
      for (let i = 0; i < 5; i++) {
        await manager.deduct("user-1", { inputTokens: 1 });
      }

      const page = await manager.listUserTransactions("user-1", { limit: 3, types: ["usage"] });

      // Paging: asked for 3, total is 5.
      expect(page.items).toHaveLength(3);
      expect(page.total).toBe(5);

      // Every item has the correct shape.
      for (const item of page.items) {
        expect(item.userId).toBe("user-1");
        expect(item.type).toBe("usage");
        expect(item.id).toBeTruthy();
        expect(typeof item.createdAt).toBe("string");
      }
    });

    it("returns all transaction types (usage + adjustment) when no type filter given", async () => {
      await manager.publishPricingFromDict({ models: { _default: "input_tokens * 1" } });
      await manager.addCredits("user-1", 100); // type=adjustment
      await manager.deduct("user-1", { inputTokens: 5 }); // type=usage

      const page = await manager.listUserTransactions("user-1");
      expect(page.total).toBeGreaterThanOrEqual(2);
      const types = new Set(page.items.map((i) => i.type));
      expect(types.has("usage")).toBe(true);
      expect(types.has("adjustment")).toBe(true);
    });
  });

  // M15 — Low-balance threshold default (minBalance * 2)
  describe("M15: low_balance threshold defaults to minBalance * 2", () => {
    it("fires when balance crosses minBalance*2 from above (minBalance=5 → threshold=10)", async () => {
      // No explicit lowBalance.thresholds — must default to minBalance*2 = 10.
      const emitter = new CreditEventEmitter();
      const mgr = new CreditManager(store, undefined, emitter);

      // Explicit minBalance: 5 (WS6: the engine's implicit default is now 0, so
      // this must be set explicitly to exercise the minBalance*2 default).
      await mgr.publishPricingFromDict({ models: { _default: "input_tokens * 1" }, minBalance: 5 });
      // Start at 12 so a single deduction of 3 crosses 10 from above.
      await mgr.addCredits("user-1", 12);

      const lowBalanceEvents: CreditEvent[] = [];
      emitter.on("credits.low_balance", (e) => lowBalanceEvents.push(e));

      // 12 → 9: crosses threshold 10.
      await mgr.deduct("user-1", { inputTokens: 3 });
      expect(lowBalanceEvents).toHaveLength(1);
      expect((lowBalanceEvents[0].data?.threshold as { toString(): string }).toString()).toBe("10");

      // 9 → 7: already below threshold → edge-triggered, must NOT fire again.
      await mgr.deduct("user-1", { inputTokens: 2 });
      expect(lowBalanceEvents).toHaveLength(1);
    });
  });

  // M9 — Idempotency key isolation: personal vs team
  describe("M9: idempotency key scoped independently to personal vs team", () => {
    it("same key 'key-1' for personal deduct AND team deduct are both charged (no collision)", async () => {
      await manager.publishPricingFromDict({ models: { _default: "input_tokens * 1" } });
      await manager.addCredits("user-1", 200);

      const team = await store.createTeam("Team X", new Decimal(500));
      await store.addTeamMember(team.teamId, "user-1", "member");

      const SHARED_KEY = "key-1";

      // Personal deduction with key-1.
      const personalResult = await manager.deduct(
        "user-1",
        { inputTokens: 10 },
        SHARED_KEY,
      );
      expect(personalResult.idempotent).toBe(false);
      expectDecimal(personalResult.amount, "10");

      // Team deduction with the SAME key-1 — must NOT be treated as a replay.
      const teamResult = await manager.deductTeam(
        team.teamId,
        "user-1",
        { inputTokens: 20 },
        SHARED_KEY,
      );
      // Team store idempotency is keyed on (teamId + idempotencyKey), separate from user.
      expect(teamResult.error).toBeFalsy();
      expectDecimal(teamResult.amount, "-20");

      // Personal balance was only charged 10 (personal key), team was charged 20.
      expectDecimal((await manager.getBalance("user-1")).balance, "190");
      expectDecimal((await store.getTeamBalance(team.teamId)).balance, "480");
    });
  });

  // MG5 — Full lifecycle test
  describe("MG5: full lifecycle end-to-end with MemoryStore", () => {
    it("add → deduct (allowance) → deduct (balance) → refund → sweep → aggregateStats", async () => {
      const s = new MemoryStore();
      s.setClock(() => FIXED_NOW);
      const emitter = new CreditEventEmitter();
      const mgr = new CreditManager(s, undefined, emitter);

      const config: PricingConfigData = {
        models: { _default: "input_tokens * 1" },
        plans: {
          starter: { id: "starter", name: "Starter", freeAllowance: new Decimal(10) },
        },
      };
      await s.setActivePricing(config);
      await s.setUserPlan("user-1", "starter");
      await mgr.publishPricingFromDict(config);

      // 1. addCredits(100) → balance = 100
      await mgr.addCredits("user-1", 100);
      expectDecimal((await mgr.getBalance("user-1")).balance, "100");

      // 2. deduct cost 30 with plan allowance of 10 → allowance 10 consumed, net 20 debited
      const d1 = await mgr.deduct("user-1", { inputTokens: 30 });
      expectDecimal(d1.allowanceConsumed, "10");
      expectDecimal(d1.amount, "20");
      expectDecimal((await mgr.getBalance("user-1")).balance, "80");

      // 3. second deduct 15 — allowance exhausted, full 15 from balance
      const d2 = await mgr.deduct("user-1", { inputTokens: 15 });
      expectDecimal(d2.allowanceConsumed, "0");
      expectDecimal(d2.amount, "15");
      expectDecimal((await mgr.getBalance("user-1")).balance, "65");

      // 4. refund first deduction → balance restored by 20 (the net charged amount)
      const refund = await mgr.refundCredits(d1.transactionId);
      expect(refund.error).toBeFalsy();
      expectDecimal(refund.amount, "20");
      expectDecimal((await mgr.getBalance("user-1")).balance, "85");

      // 5. sweepExpiredCredits({ dryRun: true }) → count=0 (nothing expired)
      const sweep = await mgr.sweepExpiredCredits(true);
      expect(sweep.expiredCount).toBe(0);
      expect(sweep.dryRun).toBe(true);

      // 6. aggregateStats → totalTransactions > 0
      const stats = await mgr.aggregateStats(
        new Date(FIXED_NOW.getTime() - 1000),
        new Date(FIXED_NOW.getTime() + 1000),
      );
      expect(stats.activeUsers).toBeGreaterThan(0);
      // Two usage deductions occurred
      expect(stats.totalCreditsConsumed.gt(0)).toBe(true);
    });
  });

  // ── WS9c: calendar_month plans take the fast path (no window computation) ──
  describe("WS9c: calendar_month plan — deduct/settle behaviour is unchanged", () => {
    it("deduct() behaves identically for an explicit calendar_month plan and a planless user", async () => {
      const s = new MemoryStore();
      s.setClock(() => FIXED_NOW);
      const mgr = new CreditManager(s);

      const config: PricingConfigData = {
        models: { _default: "input_tokens * 1" },
        plans: {
          basic: {
            id: "basic",
            name: "Basic",
            freeAllowance: new Decimal(10),
            allowancePeriod: "calendar_month",
          },
        },
      };
      await s.setActivePricing(config);
      await s.setUserPlan("user-1", "basic");
      await mgr.publishPricingFromDict(config);

      await mgr.addCredits("user-1", 100);

      // Allowance (10) covers part of a 15-credit deduction; net 5 comes from balance —
      // exactly the pre-WS9 calendar-month behaviour (no periodStart threading needed).
      const d1 = await mgr.deduct("user-1", { inputTokens: 15 });
      expectDecimal(d1.allowanceConsumed, "10");
      expectDecimal(d1.amount, "5");
      expectDecimal((await mgr.getBalance("user-1")).balance, "95");

      // A reserve/settle round-trip behaves the same way.
      const lease = await mgr.reserve("user-1", { inputTokens: 8 });
      const settled = await mgr.settle("user-1", lease.leaseId, { inputTokens: 8 });
      expectDecimal(settled.amount, "8");
      expectDecimal((await mgr.getBalance("user-1")).balance, "87");
    });

    it("getUserPlan reports allowancePeriod calendar_month when omitted from the plan definition", async () => {
      const s = new MemoryStore();
      const mgr = new CreditManager(s);
      const config: PricingConfigData = {
        models: { _default: "1" },
        plans: { basic: { id: "basic", name: "Basic", freeAllowance: new Decimal(0) } },
      };
      await s.setActivePricing(config);
      await s.setUserPlan("user-1", "basic");
      const plan = await mgr.getUserPlan("user-1");
      expect(plan.allowancePeriod ?? "calendar_month").toBe("calendar_month");
    });
  });

  // ── manager.checkAllowance (new passthrough method, no prior coverage) ────
  describe("checkAllowance", () => {
    it("calendar_month plan delegates straight to store.checkAllowance (fast path)", async () => {
      const s = new MemoryStore();
      s.setClock(() => FIXED_NOW);
      const mgr = new CreditManager(s);
      const config: PricingConfigData = {
        models: { _default: "1" },
        plans: {
          basic: {
            id: "basic",
            name: "Basic",
            freeAllowance: new Decimal(20),
            allowancePeriod: "calendar_month",
          },
        },
      };
      await s.setActivePricing(config);
      await s.setUserPlan("user-1", "basic");

      const direct = await s.checkAllowance("user-1");
      const viaManager = await mgr.checkAllowance("user-1");
      expect(viaManager).toEqual(direct);
    });

    it("planless user delegates straight to store.checkAllowance (fast path)", async () => {
      const s = new MemoryStore();
      s.setClock(() => FIXED_NOW);
      const mgr = new CreditManager(s);

      const direct = await s.checkAllowance("no-such-user");
      const viaManager = await mgr.checkAllowance("no-such-user");
      expect(viaManager).toEqual(direct);
      expect(viaManager.allowanceRemaining.toString()).toBe("0");
    });

    // NOTE: manager.checkAllowance resolves rolling_30d/anniversary windows via
    // `resolveAllowanceWindow(new Date(), ...)` — the manager has no injectable
    // clock of its own (only MemoryStore does, via setClock). So these tests
    // anchor the plan at the REAL "now" (store defaults to the real wall clock)
    // and assert against `resolveAllowanceWindow` computed with that same "now",
    // rather than pinning to a fixed historical date the manager can't see.
    it("rolling_30d plan overrides periodStart/periodEnd with the resolved window", async () => {
      const s = new MemoryStore();
      const mgr = new CreditManager(s);
      const config: PricingConfigData = {
        models: { _default: "1" },
        plans: {
          rolling: {
            id: "rolling",
            name: "Rolling",
            freeAllowance: new Decimal(10),
            allowancePeriod: "rolling_30d",
          },
        },
      };
      await s.setActivePricing(config);
      const before = new Date();
      await s.setUserPlan("user-1", "rolling"); // anchors planAssignedAt at "now"
      const after = new Date();
      const anchor = (await s.getUserPlan("user-1")).planAssignedAt!;
      expect(anchor.getTime()).toBeGreaterThanOrEqual(before.getTime());
      expect(anchor.getTime()).toBeLessThanOrEqual(after.getTime());

      const result = await mgr.checkAllowance("user-1");
      const now = new Date();
      const expectedWindow = resolveAllowanceWindow(now, "rolling_30d", anchor);
      const expectedPeriodEnd = new Date(expectedWindow.end.getTime() - 86_400_000);
      expect(result.periodStart).toBe(expectedWindow.start.toISOString());
      expect(result.periodEnd).toBe(expectedPeriodEnd.toISOString());
      expect(result.allowanceRemaining.toString()).toBe("10");
    });

    it("anniversary plan overrides periodStart/periodEnd with the resolved window", async () => {
      const s = new MemoryStore();
      const mgr = new CreditManager(s);
      const config: PricingConfigData = {
        models: { _default: "1" },
        plans: {
          anniv: {
            id: "anniv",
            name: "Anniversary",
            freeAllowance: new Decimal(8),
            allowancePeriod: "anniversary",
          },
        },
      };
      await s.setActivePricing(config);
      await s.setUserPlan("user-1", "anniv");
      const anchor = (await s.getUserPlan("user-1")).planAssignedAt!;

      const result = await mgr.checkAllowance("user-1");
      const now = new Date();
      const expectedWindow = resolveAllowanceWindow(now, "anniversary", anchor);
      const expectedPeriodEnd = new Date(expectedWindow.end.getTime() - 86_400_000);
      expect(result.periodStart).toBe(expectedWindow.start.toISOString());
      expect(result.periodEnd).toBe(expectedPeriodEnd.toISOString());
      expect(result.allowanceRemaining.toString()).toBe("8");
    });

    it("rolling_30d plan reflects a partial deduction in allowanceRemaining", async () => {
      const s = new MemoryStore();
      const mgr = new CreditManager(s);
      const config: PricingConfigData = {
        models: { _default: "input_tokens * 1" },
        plans: {
          rolling: {
            id: "rolling",
            name: "Rolling",
            freeAllowance: new Decimal(10),
            allowancePeriod: "rolling_30d",
          },
        },
      };
      await s.setActivePricing(config);
      await s.setUserPlan("user-1", "rolling");
      await mgr.publishPricingFromDict(config);
      await mgr.addCredits("user-1", 100);

      await mgr.deduct("user-1", { inputTokens: 4 });
      const result = await mgr.checkAllowance("user-1");
      expect(result.allowanceRemaining.toString()).toBe("6");
    });
  });

  // ── Credit tiers pass-through (see tests/tiers.test.ts for the main
  // feature coverage on MemoryStore itself) ─────────────────────────────
  describe("credit tiers pass-through", () => {
    const TIERED_CONFIG: PricingConfigData = {
      models: { _default: "input_tokens * 1" },
      tiers: {
        gifted: { name: "Gifted", priority: 10, expires: true, defaultTtlDays: 30 },
        purchased: { name: "Purchased", priority: 20, expires: false, isDefault: true },
      },
    };

    it("addCredits(..., { tier }) routes the grant into the requested tier", async () => {
      await manager.publishPricingFromDict(TIERED_CONFIG);
      const result = await manager.addCredits("user-1", 25, { tier: "gifted" });
      expect(result.tier).toBe("gifted");

      const tiers = await manager.getCreditTiers("user-1");
      const gifted = tiers.tiers.find((t) => t.tierKey === "gifted");
      expectDecimal(gifted!.balance, "25");
    });

    it("addCredits with an unknown tier rejects (store StoreError propagates)", async () => {
      await manager.publishPricingFromDict(TIERED_CONFIG);
      await expect(manager.addCredits("user-1", 10, { tier: "bogus" })).rejects.toThrow();
    });

    it("addCredits omitting tier resolves to the configured default tier", async () => {
      await manager.publishPricingFromDict(TIERED_CONFIG);
      const result = await manager.addCredits("user-1", 10);
      expect(result.tier).toBe("purchased");
    });

    it("getCreditTiers passes through to the store with no event emitted", async () => {
      const emitter = new CreditEventEmitter();
      const mgr = new CreditManager(store, undefined, emitter);
      await mgr.publishPricingFromDict(TIERED_CONFIG);
      await mgr.addCredits("user-1", 20, { tier: "gifted" });
      await mgr.addCredits("user-1", 10);

      // Registered AFTER the setup addCredits calls above, so only events
      // from the getCreditTiers call itself (if any) would land here.
      const events = record(emitter, [
        "credits.added",
        "credits.deducted",
        "credits.refunded",
        "credits.expired",
      ]);

      const result = await mgr.getCreditTiers("user-1");
      expect(result.userId).toBe("user-1");
      expect(result.tiers.map((t) => t.tierKey).sort()).toEqual(["gifted", "purchased"]);
      const gifted = result.tiers.find((t) => t.tierKey === "gifted");
      const purchased = result.tiers.find((t) => t.tierKey === "purchased");
      expectDecimal(gifted!.balance, "20");
      expectDecimal(purchased!.balance, "10");
      expectDecimal(result.totalBalance, "30");

      // getCreditTiers itself emits nothing (thin pass-through, matches
      // getBalance/getAvailable).
      expect(events).toHaveLength(0);
    });

    it("deduct() reports tierBreakdown drained in priority order through the manager", async () => {
      const mgr = new CreditManager(store);
      await mgr.publishPricingFromDict(TIERED_CONFIG);
      await mgr.addCredits("user-1", 20, { tier: "gifted" });
      await mgr.addCredits("user-1", 10); // default → purchased

      const result = await mgr.deduct("user-1", { inputTokens: 25 });
      expect(result.tierBreakdown?.gifted?.toString()).toBe("20");
      expect(result.tierBreakdown?.purchased?.toString()).toBe("5");
    });
  });
});

// ── Feature limits (per-feature invocation-count limits) ───────────────────
//
// Manager wiring: `feature` kwarg on `deduct`/`reserve`/`settle`/`runBilled`,
// error mapping (`feature_limit_reached` -> FeatureLimitReachedError), event
// emission (`credits.feature_limit_reached`/`credits.feature_limit_warning`),
// and the public `checkFeatureLimit` method — mirrors the existing
// `TestSpendCapsManager`-equivalent coverage above (spend caps).
describe("CreditManager — feature limits", () => {
  let store: MemoryStore;
  let manager: CreditManager;

  // NOTE: unlike most other describe blocks in this file, the store's clock is
  // NOT overridden here. `resolveCalendarWindow` (feature-limit window
  // resolution) is invoked by the manager against the REAL wall clock —
  // mirrors Python `_resolve_feature_limit` (`datetime.now(UTC)`), matching
  // how `_resolve_allowance_period_start` also uses real time for non-
  // calendar-month periods. Pinning the store's clock to a fixed instant
  // while the manager resolves windows against real time would desync
  // transaction timestamps from the resolved window, so both must share the
  // real clock here.
  beforeEach(() => {
    store = new MemoryStore();
    manager = new CreditManager(store);
  });

  const FEATURE_LIMIT_CONFIG: PricingConfigData = {
    models: { _default: "input_tokens * 1" },
    plans: {
      free: {
        id: "free",
        name: "Free",
        freeAllowance: D(0),
        featureLimits: {
          export: { maxCalls: 2, period: "monthly", action: "deny" },
          hdExport: { maxCalls: 1, period: "monthly", action: "warn" },
        },
      },
    },
  };

  function D(n: number | string): Decimal {
    return new Decimal(n);
  }

  it("deduct(feature=...) under the limit succeeds; at the limit throws FeatureLimitReachedError", async () => {
    await manager.publishPricingFromDict(FEATURE_LIMIT_CONFIG);
    await manager.setUserPlan("user-1", "free");
    await manager.addCredits("user-1", 100);

    const r1 = await manager.deduct("user-1", { inputTokens: 1 }, null, null, "export");
    expect(r1.error).toBeUndefined();
    const r2 = await manager.deduct("user-1", { inputTokens: 1 }, null, null, "export");
    expect(r2.error).toBeUndefined();

    await expect(manager.deduct("user-1", { inputTokens: 1 }, null, null, "export")).rejects.toThrow(
      FeatureLimitReachedError,
    );
  });

  it("deduct(feature=...) emits credits.feature_limit_reached on breach", async () => {
    await manager.publishPricingFromDict(FEATURE_LIMIT_CONFIG);
    await manager.setUserPlan("user-1", "free");
    await manager.addCredits("user-1", 100);
    const emitter = new CreditEventEmitter();
    manager = new CreditManager(store, undefined, emitter);
    await manager.publishPricingFromDict(FEATURE_LIMIT_CONFIG);
    const events = record(emitter, ["credits.feature_limit_reached", "credits.deduct_failed"]);

    await manager.deduct("user-1", { inputTokens: 1 }, null, null, "export");
    await manager.deduct("user-1", { inputTokens: 1 }, null, null, "export");
    await expect(manager.deduct("user-1", { inputTokens: 1 }, null, null, "export")).rejects.toThrow(
      FeatureLimitReachedError,
    );

    const reached = events.filter((e) => e.type === "credits.feature_limit_reached");
    expect(reached).toHaveLength(1);
    expect(reached[0]?.data?.feature).toBe("export");
  });

  it("deduct(feature=...) warn action emits credits.feature_limit_warning without blocking", async () => {
    await manager.publishPricingFromDict(FEATURE_LIMIT_CONFIG);
    await manager.setUserPlan("user-1", "free");
    await manager.addCredits("user-1", 100);
    const emitter = new CreditEventEmitter();
    manager = new CreditManager(store, undefined, emitter);
    await manager.publishPricingFromDict(FEATURE_LIMIT_CONFIG);
    const events = record(emitter, ["credits.feature_limit_warning"]);

    await manager.deduct("user-1", { inputTokens: 1 }, null, null, "hdExport");
    const r2 = await manager.deduct("user-1", { inputTokens: 1 }, null, null, "hdExport");
    expect(r2.error).toBeUndefined();

    expect(events).toHaveLength(1);
    expect(events[0]?.data?.action).toBe("warn");
    expect(events[0]?.data?.feature).toBe("hdExport");
  });

  it("reserve(feature=...) enforces deny at admission; settle(feature=...) re-supplies the feature", async () => {
    await manager.publishPricingFromDict(FEATURE_LIMIT_CONFIG);
    await manager.setUserPlan("user-1", "free");
    await manager.addCredits("user-1", 100);

    const l1 = await manager.reserve("user-1", D(1), { feature: "export" });
    await manager.settle("user-1", l1.leaseId, D(1), { feature: "export" });
    const l2 = await manager.reserve("user-1", D(1), { feature: "export" });
    await manager.settle("user-1", l2.leaseId, D(1), { feature: "export" });

    await expect(manager.reserve("user-1", D(1), { feature: "export" })).rejects.toThrow(
      FeatureLimitReachedError,
    );
  });

  it("runBilled(feature=...) threads the feature through reserve and settle", async () => {
    await manager.publishPricingFromDict(FEATURE_LIMIT_CONFIG);
    await manager.setUserPlan("user-1", "free");
    await manager.addCredits("user-1", 100);

    await manager.runBilled("user-1", {
      estimate: D(1),
      doWork: async () => ({ result: "ok", actual: D(1) }),
      feature: "export",
    });
    await manager.runBilled("user-1", {
      estimate: D(1),
      doWork: async () => ({ result: "ok", actual: D(1) }),
      feature: "export",
    });

    await expect(
      manager.runBilled("user-1", {
        estimate: D(1),
        doWork: async () => ({ result: "ok", actual: D(1) }),
        feature: "export",
      }),
    ).rejects.toThrow(FeatureLimitReachedError);
  });

  it("checkFeatureLimit returns limited:false when no limit is configured for the feature", async () => {
    await manager.publishPricingFromDict(FEATURE_LIMIT_CONFIG);
    await manager.setUserPlan("user-1", "free");

    const result = await manager.checkFeatureLimit("user-1", "unconfigured-feature");
    expect(result.limited).toBe(false);
    expect(result.action).toBeNull();
  });

  it("checkFeatureLimit resolves the configured limit and current usage", async () => {
    await manager.publishPricingFromDict(FEATURE_LIMIT_CONFIG);
    await manager.setUserPlan("user-1", "free");
    await manager.addCredits("user-1", 100);

    await manager.deduct("user-1", { inputTokens: 1 }, null, null, "export");

    const result = await manager.checkFeatureLimit("user-1", "export");
    expect(result.limited).toBe(true);
    expect(result.limit).toBe(2);
    expect(result.used).toBe(1);
    expect(result.remaining).toBe(1);
    expect(result.action).toBe("deny");
  });

  it("planless user: no feature limit is ever enforced", async () => {
    await manager.publishPricingFromDict(FEATURE_LIMIT_CONFIG);
    await manager.addCredits("user-1", 100);

    for (let i = 0; i < 5; i++) {
      const r = await manager.deduct("user-1", { inputTokens: 1 }, null, null, "export");
      expect(r.error).toBeUndefined();
    }
  });
});
