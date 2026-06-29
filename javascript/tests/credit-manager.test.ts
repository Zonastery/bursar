import { describe, it, expect, beforeEach } from "vitest";
import Decimal from "decimal.js";
import { CreditManager } from "../src/manager.js";
import { MemoryStore } from "../src/stores/memory-store.js";
import { CreditEventEmitter } from "../src/stores/events.js";
import type { CreditEvent } from "../src/stores/events.js";
import {
  CapReachedError,
  ConfigError,
  InsufficientCreditsError,
  PricingNotLoadedError,
  RefundError,
} from "../src/errors.js";
import type { PricingConfigData } from "../src/types.js";

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
    await manager.addCredits("user-1", 500, "purchase");
    const balance = await manager.getBalance("user-1");
    expectDecimal(balance.balance, "1492.5");
    expectDecimal(balance.lifetimePurchased, "500");
  });

  describe("plan allowance", () => {
    it("fully covers cost with plan allowance, skipping balance deduct", async () => {
      const config: PricingConfigData = {
        models: { _default: "input_tokens * 1" },
        plans: { free: { id: "plan-free", name: "Free", freeAllowance: new Decimal(100) } },
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
        plans: { starter: { id: "plan-starter", name: "Starter", freeAllowance: new Decimal(10) } },
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
      const add = await manager.addCredits("user-1", 100, "purchase");
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
      await manager.addCredits(
        "user-1",
        100,
        "purchase",
        null,
        new Date(FIXED_NOW.getTime() - 60_000),
      );

      const result = await manager.sweepExpiredCredits();
      expect(result.expiredCount).toBe(1);
      expectDecimal(result.expiredAmount, "100");
      expectDecimal((await manager.getBalance("user-1")).balance, "0");
    });

    it("dryRun through manager reports without modifying", async () => {
      await manager.publishPricingFromDict(TEST_CONFIG);
      await manager.addCredits(
        "user-1",
        100,
        "purchase",
        null,
        new Date(FIXED_NOW.getTime() - 60_000),
      );

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
      await manager.addCredits(
        "user-1",
        100,
        "purchase",
        null,
        new Date(FIXED_NOW.getTime() - 60_000),
      );

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

    it("deductTeam insufficient balance returns error", async () => {
      const mgr = new CreditManager(store);
      await mgr.publishPricingFromDict({ models: { _default: "input_tokens * 1" } });

      const team = await store.createTeam("Poor Team", new Decimal(10));
      await store.addTeamMember(team.teamId, "user-1", "member");

      const result = await mgr.deductTeam(team.teamId, "user-1", { inputTokens: 100 });
      expect(result.error).toBe("insufficient_team_balance");
    });

    it("deductTeam user not in team returns error", async () => {
      const mgr = new CreditManager(store);
      await mgr.publishPricingFromDict({ models: { _default: "input_tokens * 1" } });

      const team = await store.createTeam("Closed Team", new Decimal(500));
      const result = await mgr.deductTeam(team.teamId, "user-1", { inputTokens: 10 });
      expect(result.error).toBe("user_not_in_team");
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
      // minBalance default 5 → threshold = 10.
      const mgr = new CreditManager(store, undefined, emitter);
      await mgr.publishPricingFromDict({ models: { _default: "input_tokens * 1" } });
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
        lowBalanceThreshold: new Decimal(50),
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
      await mgr.publishPricingFromDict({ models: { _default: "input_tokens * 1" } });
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
      await mgr.addCredits("user-1", 100, "purchase", null, new Date(FIXED_NOW.getTime() - 60_000));
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
  });
});
