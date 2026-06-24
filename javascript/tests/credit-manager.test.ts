import { describe, it, expect, beforeEach } from "vitest";
import { CreditManager } from "../src/manager.js";
import { MemoryStore } from "../src/stores/memory-store.js";
import { InsufficientCreditsError, PricingNotLoadedError } from "../src/errors.js";
import type { PlanDefinition, PricingConfigData } from "../src/types.js";

const TEST_CONFIG: PricingConfigData = {
  version: 1,
  models: {
    "gpt-4": "input_tokens * (10 / 1000) + output_tokens * (30 / 1000)",
  },
  tools: {
    _default: "tool_calls * 5 / 1000",
  },
};

describe("CreditManager", () => {
  let manager: CreditManager;

  beforeEach(() => {
    const store = new MemoryStore();
    manager = new CreditManager(store);
  });

  it("rejects deduct before pricing is loaded", async () => {
    await expect(() =>
      manager.deduct("user-1", { model: "gpt-4", inputTokens: 100 }),
    ).rejects.toThrow(PricingNotLoadedError);
  });

  it("completes full flow: publish pricing → add credits → deduct", async () => {
    manager.publishPricingFromDict(TEST_CONFIG);

    await manager.addCredits("user-1", 1000);

    const result = await manager.deduct("user-1", {
      model: "gpt-4",
      inputTokens: 100,
      outputTokens: 50,
    });

    expect(result.transactionId).toBeTruthy();
    expect(result.amount).toBeLessThan(0);
    expect(result.idempotent).toBe(false);

    const balance = await manager.getBalance("user-1");

    // Cost: 100 * 0.01 + 50 * 0.03 = 1 + 1.5 = 2.5 → truncate to 2
    // Balance: 1000 - 2 = 998
    expect(balance.balance).toBe(998);
  });

  it("handles idempotent deductions", async () => {
    manager.publishPricingFromDict(TEST_CONFIG);
    await manager.addCredits("user-1", 500);

    const result1 = await manager.deduct(
      "user-1",
      {
        model: "gpt-4",
        inputTokens: 100,
      },
      "idem-key-1",
    );
    expect(result1.idempotent).toBe(false);

    // Same idempotency key — should return cached result
    const result2 = await manager.deduct(
      "user-1",
      {
        model: "gpt-4",
        inputTokens: 100,
      },
      "idem-key-1",
    );
    expect(result2.idempotent).toBe(true);
  });

  it("throws on insufficient credits", async () => {
    manager.publishPricingFromDict(TEST_CONFIG);
    await manager.addCredits("user-1", 1);

    await expect(() =>
      manager.deduct("user-1", {
        model: "gpt-4",
        inputTokens: 10_000,
      }),
    ).rejects.toThrow(InsufficientCreditsError);
  });

  it("loads pricing from store", async () => {
    const store = new MemoryStore();
    await store.setActivePricing(TEST_CONFIG);

    const mgr = new CreditManager(store);
    await mgr.loadPricingFromStore();

    // Now deduct should work
    await mgr.addCredits("user-1", 100);
    const result = await mgr.deduct("user-1", {
      model: "gpt-4",
      inputTokens: 100,
    });
    expect(result.transactionId).toBeTruthy();
  });

  it("publishPricing updates engine", async () => {
    manager.publishPricing(TEST_CONFIG);
    expect(manager.pricingEngine).not.toBeNull();
  });

  it("deductFixed shortcut works", async () => {
    const config: PricingConfigData = {
      version: 1,
      models: { _default: "input_tokens * 1" },
      fixed: { batch_job: 50 },
    };
    manager.publishPricingFromDict(config);
    await manager.addCredits("user-1", 100);

    const result = await manager.deductFixed("user-1", "batch_job");
    expect(result.transactionId).toBeTruthy();
    expect(result.amount).toBe(-50);
  });

  it("tracks balance correctly across multiple operations", async () => {
    manager.publishPricingFromDict(TEST_CONFIG);
    await manager.addCredits("user-1", 1000);

    // First deduction
    await manager.deduct("user-1", { model: "gpt-4", inputTokens: 100, outputTokens: 50 });
    let balance = await manager.getBalance("user-1");
    expect(balance.balance).toBe(998);

    // Second deduction
    await manager.deduct("user-1", { model: "gpt-4", inputTokens: 200, outputTokens: 100 });
    balance = await manager.getBalance("user-1");
    expect(balance.balance).toBe(993);

    // Add more credits
    await manager.addCredits("user-1", 500, "purchase");
    balance = await manager.getBalance("user-1");
    expect(balance.balance).toBe(1493);
    expect(balance.lifetimePurchased).toBe(500);
  });

  describe("plan allowance", () => {
    it("fully covers cost with plan allowance, skipping balance deduct", async () => {
      const store = new MemoryStore();
      const v2Config: PricingConfigData & { plans: Record<string, PlanDefinition> } = {
        version: 2,
        models: { _default: "input_tokens * 1" },
        plans: {
          free: { id: "plan-free", name: "Free", freeAllowance: 100 },
        },
      };
      store.setActivePricing(v2Config);
      store.setUserPlan("user-1", "plan-free");

      const mgr = new CreditManager(store);
      mgr.publishPricingFromDict(v2Config);
      await mgr.addCredits("user-1", 10); // small balance

      // Deduct 5 — fully covered by allowance
      const result = await mgr.deduct("user-1", { inputTokens: 5 });
      expect(result.amount).toBe(0); // no credits deducted
      expect(result.transactionId).toBe("");

      // Balance unchanged
      const balance = await mgr.getBalance("user-1");
      expect(balance.balance).toBe(10);

      // Allowance reduced
      const allowance = await store.checkAllowance("user-1");
      expect(allowance.allowanceRemaining).toBe(95);
    });

    it("partially covers cost with plan allowance, deducts remainder from balance", async () => {
      const store = new MemoryStore();
      const v2Config: PricingConfigData & { plans: Record<string, PlanDefinition> } = {
        version: 2,
        models: { _default: "input_tokens * 1" },
        plans: {
          starter: { id: "plan-starter", name: "Starter", freeAllowance: 10 },
        },
      };
      store.setActivePricing(v2Config);
      store.setUserPlan("user-1", "plan-starter");

      const mgr = new CreditManager(store);
      mgr.publishPricingFromDict(v2Config);
      await mgr.addCredits("user-1", 100);

      // Deduct 25 — covers 10 from allowance, 15 from balance
      const result = await mgr.deduct("user-1", { inputTokens: 25 });
      expect(result.amount).toBe(-15);
      expect(result.transactionId).toBeTruthy();

      const balance = await mgr.getBalance("user-1");
      expect(balance.balance).toBe(85);

      const allowance = await store.checkAllowance("user-1");
      expect(allowance.allowanceRemaining).toBe(0);
    });

    it("no plan uses existing balance-only deduct flow", async () => {
      manager.publishPricingFromDict(TEST_CONFIG);
      await manager.addCredits("user-1", 100);

      const result = await manager.deduct("user-1", {
        model: "gpt-4",
        inputTokens: 100,
        outputTokens: 50,
      });
      expect(result.amount).toBe(-2);
      expect(result.transactionId).toBeTruthy();
    });

    it("setUserPlan/getUserPlan round-trip before deduct", async () => {
      const store = new MemoryStore();
      const v2Config: PricingConfigData & { plans: Record<string, PlanDefinition> } = {
        version: 2,
        models: { _default: "input_tokens * 1" },
        plans: {
          pro: { id: "plan-pro", name: "Pro", freeAllowance: 500 },
        },
      };
      store.setActivePricing(v2Config);
      store.setUserPlan("user-1", "plan-pro");

      const plan = await store.getUserPlan("user-1");
      expect(plan.planId).toBe("plan-pro");
      expect(plan.freeAllowance).toBe(500);

      const mgr = new CreditManager(store);
      mgr.publishPricingFromDict(v2Config);
      await mgr.addCredits("user-1", 100);

      const result = await mgr.deduct("user-1", { inputTokens: 10 });
      expect(result.amount).toBe(0); // fully covered by plan allowance
    });
  });

  describe("refunds", () => {
    it("refunds a full deduction through manager", async () => {
      manager.publishPricingFromDict(TEST_CONFIG);
      await manager.addCredits("user-1", 1000);

      const deduct = await manager.deduct("user-1", {
        model: "gpt-4",
        inputTokens: 100,
      });
      expect(deduct.amount).toBe(-1);

      const refund = await manager.refundCredits(deduct.transactionId);
      expect(refund.error).toBeUndefined();
      expect(refund.amount).toBe(1);

      const balance = await manager.getBalance("user-1");
      expect(balance.balance).toBe(1000);
    });

    it("partial refund through manager", async () => {
      manager.publishPricingFromDict({ version: 1, models: { _default: "input_tokens * 1" } });
      await manager.addCredits("user-1", 100);

      const deduct = await manager.deduct("user-1", { inputTokens: 50 });

      const refund = await manager.refundCredits(deduct.transactionId, 20);
      expect(refund.error).toBeUndefined();
      expect(refund.amount).toBe(20);

      const balance = await manager.getBalance("user-1");
      expect(balance.balance).toBe(70); // 100 - 50 + 20
    });

    it("double refund returns error through manager", async () => {
      manager.publishPricingFromDict({ version: 1, models: { _default: "input_tokens * 1" } });
      await manager.addCredits("user-1", 100);

      const deduct = await manager.deduct("user-1", { inputTokens: 40 });
      const first = await manager.refundCredits(deduct.transactionId);
      expect(first.error).toBeUndefined();

      const second = await manager.refundCredits(deduct.transactionId);
      expect(second.error).toBe("already_refunded");
    });

    it("refund of unknown transaction returns error through manager", async () => {
      manager.publishPricingFromDict({ version: 1, models: { _default: "input_tokens * 1" } });
      await manager.addCredits("user-1", 100);

      const result = await manager.refundCredits("no-such-transaction");
      expect(result.error).toBe("transaction_not_found");
    });
  });

  describe("usage analytics", () => {
    it("spendByUser delegates to store and returns results", async () => {
      manager.publishPricingFromDict(TEST_CONFIG);
      await manager.addCredits("user-1", 500);
      await manager.deduct("user-1", { model: "gpt-4", inputTokens: 100 });

      const now = new Date();
      const rows = await manager.spendByUser(
        new Date(now.getTime() - 1000),
        new Date(now.getTime() + 1000),
      );
      expect(rows.length).toBeGreaterThanOrEqual(1);
      expect(rows[0].userId).toBe("user-1");
    });

    it("spendByModel returns results through manager", async () => {
      manager.publishPricingFromDict(TEST_CONFIG);
      await manager.addCredits("user-1", 500);
      await manager.deduct("user-1", { model: "gpt-4", inputTokens: 100 });

      const now = new Date();
      const rows = await manager.spendByModel(
        new Date(now.getTime() - 1000),
        new Date(now.getTime() + 1000),
      );
      expect(rows.length).toBeGreaterThanOrEqual(1);
    });

    it("topUsers returns top users through manager", async () => {
      manager.publishPricingFromDict(TEST_CONFIG);
      await manager.addCredits("user-1", 500);
      await manager.deduct("user-1", { model: "gpt-4", inputTokens: 100 });

      const now = new Date();
      const rows = await manager.topUsers(
        5,
        new Date(now.getTime() - 1000),
        new Date(now.getTime() + 1000),
      );
      expect(rows.length).toBeGreaterThanOrEqual(1);
    });

    it("dailySpend returns bucketed results through manager", async () => {
      manager.publishPricingFromDict(TEST_CONFIG);
      await manager.addCredits("user-1", 500);
      await manager.deduct("user-1", { model: "gpt-4", inputTokens: 100 });

      const now = new Date();
      const rows = await manager.dailySpend(
        new Date(now.getTime() - 86400000),
        new Date(now.getTime() + 86400000),
      );
      expect(rows.length).toBeGreaterThanOrEqual(1);
      expect(rows[0].totalSpend).toBeGreaterThan(0);
    });
  });

  describe("credit expiry", () => {
    it("sweepExpiredCredits delegates to store", async () => {
      manager.publishPricingFromDict(TEST_CONFIG);
      await manager.addCredits("user-1", 100, "purchase", null, new Date(Date.now() + 1));

      await new Promise((r) => setTimeout(r, 10));

      const result = await manager.sweepExpiredCredits();
      expect(result.expiredCount).toBe(1);
      expect(result.expiredAmount).toBe(100);
      expect((await manager.getBalance("user-1")).balance).toBe(0);
    });

    it("dryRun through manager reports without modifying", async () => {
      manager.publishPricingFromDict(TEST_CONFIG);
      await manager.addCredits("user-1", 100, "purchase", null, new Date(Date.now() + 1));

      await new Promise((r) => setTimeout(r, 10));

      const result = await manager.sweepExpiredCredits(true);
      expect(result.expiredCount).toBe(1);
      expect(result.dryRun).toBe(true);
      expect((await manager.getBalance("user-1")).balance).toBe(100);
    });

    it("credits without expiry never expire through manager", async () => {
      manager.publishPricingFromDict(TEST_CONFIG);
      await manager.addCredits("user-1", 100);

      const result = await manager.sweepExpiredCredits();
      expect(result.expiredCount).toBe(0);
      expect(result.expiredAmount).toBe(0);
      expect((await manager.getBalance("user-1")).balance).toBe(100);
    });

    it("sweep with no expired returns zero through manager", async () => {
      manager.publishPricingFromDict(TEST_CONFIG);
      await manager.addCredits("user-1", 50);

      const result = await manager.sweepExpiredCredits();
      expect(result.expiredCount).toBe(0);
      expect(result.expiredAmount).toBe(0);
    });
  });

  describe("team balance pools", () => {
    it("deductTeam calculates cost and debits team pool", async () => {
      const store = new MemoryStore();
      const mgr = new CreditManager(store);
      mgr.publishPricingFromDict({ version: 1, models: { _default: "input_tokens * 1" } });

      const team = await store.createTeam("Team", 500);
      await store.addTeamMember(team.teamId, "user-1", "member");

      const result = await mgr.deductTeam(team.teamId, "user-1", { inputTokens: 100 });
      expect(result.amount).toBe(-100);
      expect(result.teamBalanceAfter).toBe(400);
      expect(result.transactionId).toBeTruthy();
    });

    it("deductTeam zero-cost returns without deducting", async () => {
      const store = new MemoryStore();
      const mgr = new CreditManager(store);
      mgr.publishPricingFromDict({ version: 1, models: { _default: "input_tokens * 1" } });

      const team = await store.createTeam("Team", 500);
      await store.addTeamMember(team.teamId, "user-1", "member");

      const result = await mgr.deductTeam(team.teamId, "user-1", { inputTokens: 0 });
      expect(result.amount).toBe(0);
      expect(result.teamBalanceAfter).toBe(500);
    });

    it("deductTeam throws without pricing loaded", async () => {
      const store = new MemoryStore();
      const mgr = new CreditManager(store);

      await expect(() => mgr.deductTeam("team-1", "user-1", { inputTokens: 100 })).rejects.toThrow(
        PricingNotLoadedError,
      );
    });

    it("deductTeam insufficient balance returns error", async () => {
      const store = new MemoryStore();
      const mgr = new CreditManager(store);
      mgr.publishPricingFromDict({ version: 1, models: { _default: "input_tokens * 1" } });

      const team = await store.createTeam("Poor Team", 10);
      await store.addTeamMember(team.teamId, "user-1", "member");

      const result = await mgr.deductTeam(team.teamId, "user-1", { inputTokens: 100 });
      expect(result.error).toBe("insufficient_team_balance");
    });

    it("deductTeam user not in team returns error", async () => {
      const store = new MemoryStore();
      const mgr = new CreditManager(store);
      mgr.publishPricingFromDict({ version: 1, models: { _default: "input_tokens * 1" } });

      const team = await store.createTeam("Closed Team", 500);
      const result = await mgr.deductTeam(team.teamId, "user-1", { inputTokens: 10 });
      expect(result.error).toBe("user_not_in_team");
    });

    it("createTeam via store with initial balance", async () => {
      const store = new MemoryStore();
      const mgr = new CreditManager(store);
      mgr.publishPricingFromDict({ version: 1, models: { _default: "input_tokens * 1" } });

      const team = await store.createTeam("Dev Team", 300);
      expect(team.teamId).toBeTruthy();
      expect(team.name).toBe("Dev Team");

      const balance = await store.getTeamBalance(team.teamId);
      expect(balance.balance).toBe(300);
    });

    it("addTeamMember via store then deduct via manager", async () => {
      const store = new MemoryStore();
      const mgr = new CreditManager(store);
      mgr.publishPricingFromDict({ version: 1, models: { _default: "input_tokens * 1" } });

      const team = await store.createTeam("Squad", 200);
      await store.addTeamMember(team.teamId, "user-1", "member");

      const members = await store.getTeamMembers(team.teamId);
      expect(members).toHaveLength(1);
      expect(members[0].userId).toBe("user-1");
      expect(members[0].role).toBe("member");
    });

    it("team balance reflects deductions through manager", async () => {
      const store = new MemoryStore();
      const mgr = new CreditManager(store);
      mgr.publishPricingFromDict({ version: 1, models: { _default: "input_tokens * 1" } });

      const team = await store.createTeam("Pipeline Team", 500);
      await store.addTeamMember(team.teamId, "user-1", "member");

      const result = await mgr.deductTeam(team.teamId, "user-1", { inputTokens: 150 });
      expect(result.teamBalanceAfter).toBe(350);

      const balance = await store.getTeamBalance(team.teamId);
      expect(balance.balance).toBe(350);
    });
  });

  describe("spend caps", () => {
    it("daily deny cap blocks 11th credit", async () => {
      const store = new MemoryStore();
      const mgr = new CreditManager(store);
      mgr.publishPricingFromDict({ version: 1, models: { _default: "input_tokens * 1" } });

      await mgr.addCredits("user-1", 1000);
      store.setSpendCap({ userId: "user-1", type: "daily", limit: 10, action: "deny" });

      // 11 tokens would cost 11, which exceeds cap of 10
      await expect(() => mgr.deduct("user-1", { model: "gpt-4", inputTokens: 11 })).rejects.toThrow(
        "Spend cap exceeded",
      );
    });

    it("warn action allows deduction through", async () => {
      const store = new MemoryStore();
      const mgr = new CreditManager(store);
      mgr.publishPricingFromDict({ version: 1, models: { _default: "input_tokens * 1" } });

      await mgr.addCredits("user-1", 1000);
      store.setSpendCap({ userId: "user-1", type: "daily", limit: 10, action: "warn" });

      const result = await mgr.deduct("user-1", { model: "gpt-4", inputTokens: 11 });
      expect(result.transactionId).toBeTruthy();
    });

    it("notify action allows deduction through", async () => {
      const store = new MemoryStore();
      const mgr = new CreditManager(store);
      mgr.publishPricingFromDict({ version: 1, models: { _default: "input_tokens * 1" } });

      await mgr.addCredits("user-1", 1000);
      store.setSpendCap({ userId: "user-1", type: "daily", limit: 10, action: "notify" });

      const result = await mgr.deduct("user-1", { model: "gpt-4", inputTokens: 11 });
      expect(result.transactionId).toBeTruthy();
    });

    it("spend cap does not affect deductions within limit", async () => {
      const store = new MemoryStore();
      const mgr = new CreditManager(store);
      mgr.publishPricingFromDict({ version: 1, models: { _default: "input_tokens * 1" } });

      await mgr.addCredits("user-1", 1000);
      store.setSpendCap({ userId: "user-1", type: "daily", limit: 100, action: "deny" });

      const result = await mgr.deduct("user-1", { model: "gpt-4", inputTokens: 5 });
      expect(result.transactionId).toBeTruthy();
    });
  });

  describe("event system", () => {
    it("emits credits.deducted event on deduct", async () => {
      const store = new MemoryStore();
      const emitter = new (await import("../src/stores/events.js")).CreditEventEmitter();
      const mgr = new CreditManager(store, undefined, emitter);

      mgr.publishPricingFromDict({ version: 1, models: { _default: "input_tokens * 1" } });
      await mgr.addCredits("user-1", 100);

      const events: Array<{ type: string; userId: string }> = [];
      emitter.on("credits.deducted", (e) => events.push({ type: e.type, userId: e.userId }));

      await mgr.deduct("user-1", { inputTokens: 10 });
      expect(events).toHaveLength(1);
      expect(events[0].type).toBe("credits.deducted");
      expect(events[0].userId).toBe("user-1");
    });

    it("credits.added event includes amount and newBalance", async () => {
      const emitter = new (await import("../src/stores/events.js")).CreditEventEmitter();
      const mgr = new CreditManager(new MemoryStore(), undefined, emitter);

      const events: Array<{ type: string; data?: Record<string, unknown> }> = [];
      emitter.on("credits.added", (e) => events.push({ type: e.type, data: e.data }));

      mgr.publishPricingFromDict({ version: 1, models: { _default: "input_tokens * 1" } });
      await mgr.addCredits("user-1", 50);

      expect(events).toHaveLength(1);
      expect(events[0].data?.amount).toBe(50);
    });

    it("emits credits.refunded event on refund", async () => {
      const store = new MemoryStore();
      const emitter = new (await import("../src/stores/events.js")).CreditEventEmitter();
      const mgr = new CreditManager(store, undefined, emitter);

      mgr.publishPricingFromDict({ version: 1, models: { _default: "input_tokens * 1" } });
      await mgr.addCredits("user-1", 100);

      const events: Array<{ type: string }> = [];
      emitter.on("credits.refunded", (e) => events.push({ type: e.type }));

      const deduct = await mgr.deduct("user-1", { inputTokens: 10 });
      await mgr.refundCredits(deduct.transactionId);

      expect(events).toHaveLength(1);
      expect(events[0].type).toBe("credits.refunded");
    });

    it("emits credits.cap_reached event when deny cap blocks", async () => {
      const store = new MemoryStore();
      const emitter = new (await import("../src/stores/events.js")).CreditEventEmitter();
      const mgr = new CreditManager(store, undefined, emitter);

      mgr.publishPricingFromDict({ version: 1, models: { _default: "input_tokens * 1" } });
      await mgr.addCredits("user-1", 100);
      store.setSpendCap({ userId: "user-1", type: "daily", limit: 5, action: "deny" });

      const events: Array<{ type: string; data?: Record<string, unknown> }> = [];
      emitter.on("credits.cap_reached", (e) => events.push({ type: e.type, data: e.data }));

      await expect(() => mgr.deduct("user-1", { inputTokens: 10 })).rejects.toThrow(
        "Spend cap exceeded",
      );
      expect(events).toHaveLength(1);
      expect(events[0].type).toBe("credits.cap_reached");
    });

    it("emits credits.cap_warning event when warn cap allows through", async () => {
      const store = new MemoryStore();
      const emitter = new (await import("../src/stores/events.js")).CreditEventEmitter();
      const mgr = new CreditManager(store, undefined, emitter);

      mgr.publishPricingFromDict({ version: 1, models: { _default: "input_tokens * 1" } });
      await mgr.addCredits("user-1", 100);
      store.setSpendCap({ userId: "user-1", type: "daily", limit: 5, action: "warn" });

      const events: Array<{ type: string }> = [];
      emitter.on("credits.cap_warning", (e) => events.push({ type: e.type }));

      await mgr.deduct("user-1", { inputTokens: 10 });
      expect(events).toHaveLength(1);
      expect(events[0].type).toBe("credits.cap_warning");
    });

    it("emits credits.expired event on sweep", async () => {
      const store = new MemoryStore();
      const emitter = new (await import("../src/stores/events.js")).CreditEventEmitter();
      const mgr = new CreditManager(store, undefined, emitter);

      mgr.publishPricingFromDict({ version: 1, models: { _default: "input_tokens * 1" } });

      const events: Array<{ type: string; data?: Record<string, unknown> }> = [];
      emitter.on("credits.expired", (e) => events.push({ type: e.type, data: e.data }));

      await mgr.addCredits("user-1", 100, "purchase", null, new Date(Date.now() + 1));
      await new Promise((r) => setTimeout(r, 10));
      await mgr.sweepExpiredCredits();

      expect(events).toHaveLength(1);
      expect(events[0].type).toBe("credits.expired");
      expect(events[0].data?.expiredCount).toBe(1);
    });

    it("multiple handlers all fire for same event", async () => {
      const emitter = new (await import("../src/stores/events.js")).CreditEventEmitter();
      const mgr = new CreditManager(new MemoryStore(), undefined, emitter);

      const called: number[] = [];
      emitter.on("credits.deducted", () => called.push(1));
      emitter.on("credits.deducted", () => called.push(2));

      mgr.publishPricingFromDict({ version: 1, models: { _default: "input_tokens * 1" } });
      await mgr.addCredits("user-1", 100);
      await mgr.deduct("user-1", { inputTokens: 10 });

      expect(called).toHaveLength(2);
    });

    it("unregistered event type does not throw", async () => {
      const emitter = new (await import("../src/stores/events.js")).CreditEventEmitter();
      expect(() =>
        emitter.emit({ type: "credits.deducted", timestamp: new Date(), userId: "u1" }),
      ).not.toThrow();
    });
  });
});
