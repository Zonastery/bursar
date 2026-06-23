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
  });
});
