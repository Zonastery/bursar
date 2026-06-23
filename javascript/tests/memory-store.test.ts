import { describe, it, expect, beforeEach } from "vitest";
import { MemoryStore } from "../src/stores/memory-store.js";
import type { PlanDefinition, PricingConfigData } from "../src/types.js";

describe("MemoryStore", () => {
  let store: MemoryStore;

  beforeEach(() => {
    store = new MemoryStore();
  });

  describe("setup", () => {
    it("returns setup result with table names", async () => {
      const result = await store.setup();
      expect(result.success).toBe(true);
      expect(result.tablesCreated).toHaveLength(5);
    });
  });

  describe("getBalance / addCredits", () => {
    it("returns zero balance for new user", async () => {
      const result = await store.getBalance("user-1");
      expect(result.balance).toBe(0);
      expect(result.lifetimePurchased).toBe(0);
    });

    it("adds credits", async () => {
      const result = await store.addCredits("user-1", 100);
      expect(result.newBalance).toBe(100);
      expect(result.userId).toBe("user-1");
    });

    it("tracks lifetime purchases", async () => {
      await store.addCredits("user-1", 100, "purchase");
      const result = await store.getBalance("user-1");
      expect(result.lifetimePurchased).toBe(100);
    });

    it("does not count adjustments toward lifetime", async () => {
      await store.addCredits("user-1", 50, "adjustment");
      const result = await store.getBalance("user-1");
      expect(result.lifetimePurchased).toBe(0);
    });

    it("accumulates multiple adds", async () => {
      await store.addCredits("user-1", 50);
      await store.addCredits("user-1", 75);
      const result = await store.getBalance("user-1");
      expect(result.balance).toBe(125);
    });
  });

  describe("credit lifecycle: add → reserve → deduct", () => {
    it("completes full lifecycle", async () => {
      await store.addCredits("user-1", 100, "purchase");
      const reserve = await store.reserveCredits("user-1", 30, "usage");
      expect(reserve.error).toBeUndefined();
      expect(reserve.amount).toBe(30);

      const deduct = await store.deductCredits("user-1", reserve.reservationId, 30);
      expect(deduct.error).toBeUndefined();
      expect(deduct.amount).toBe(-30);

      const balance = await store.getBalance("user-1");
      expect(balance.balance).toBe(70);
    });

    it("rejects insufficient credits", async () => {
      await store.addCredits("user-1", 10);
      const result = await store.reserveCredits("user-1", 100, "usage");
      expect(result.error).toBe("insufficient_credits");
    });

    it("rejects reservation below min_balance threshold", async () => {
      await store.addCredits("user-1", 20);
      const result = await store.reserveCredits("user-1", 10, "usage", null, 15);
      expect(result.error).toBe("insufficient_credits");
    });

    it("handles idempotent deductions", async () => {
      await store.addCredits("user-1", 100);
      const reserve = await store.reserveCredits("user-1", 50, "usage");
      const deduct1 = await store.deductCredits("user-1", reserve.reservationId, 50, "idem-1");
      expect(deduct1.idempotent).toBe(false);

      // Replay with same idempotency key
      const reserve2 = await store.reserveCredits("user-1", 50, "usage");
      const deduct2 = await store.deductCredits("user-1", reserve2.reservationId, 50, "idem-1");
      expect(deduct2.idempotent).toBe(true);
    });

    it("handles concurrent reservations", async () => {
      await store.addCredits("user-1", 100);
      const r1 = await store.reserveCredits("user-1", 60, "usage");
      expect(r1.error).toBeUndefined();

      // Second reservation should fail — only 40 available, needs 60
      const r2 = await store.reserveCredits("user-1", 60, "usage");
      expect(r2.error).toBe("insufficient_credits");
    });
  });

  describe("pricing config", () => {
    it("returns null when no pricing set", async () => {
      expect(await store.getActivePricing()).toBeNull();
    });

    it("stores and retrieves pricing config", async () => {
      const config: PricingConfigData = {
        version: 1,
        models: { "gpt-4": "input_tokens * 0.01" },
      };
      await store.setActivePricing(config);
      const result = await store.getActivePricing();
      expect(result).not.toBeNull();
      expect(result!.config.models["gpt-4"]).toBe("input_tokens * 0.01");
    });

    it("increments version on each set", async () => {
      const config: PricingConfigData = { version: 1, models: { a: "1" } };
      const id1 = await store.setActivePricing(config);
      const id2 = await store.setActivePricing(config);
      expect(id1).not.toBe(id2);
    });
  });

  describe("plan management", () => {
    it("getUserPlan returns null plan for user with no plan", async () => {
      const result = await store.getUserPlan("user-1");
      expect(result.planId).toBeNull();
      expect(result.planName).toBeNull();
      expect(result.freeAllowance).toBe(0);
    });

    it("setUserPlan and getUserPlan round-trips", async () => {
      // Seed plan definition via v2 pricing config
      const v2Config: PricingConfigData & { plans: Record<string, PlanDefinition> } = {
        version: 2,
        models: { _default: "1" },
        plans: {
          free: { id: "plan-free", name: "Free Plan", freeAllowance: 100 },
        },
      };
      await store.setActivePricing(v2Config);

      await store.setUserPlan("user-1", "plan-free");
      const result = await store.getUserPlan("user-1");
      expect(result.planId).toBe("plan-free");
      expect(result.planName).toBe("Free Plan");
      expect(result.freeAllowance).toBe(100);
    });

    it("checkAllowance returns remaining allowance", async () => {
      const v2Config: PricingConfigData & { plans: Record<string, PlanDefinition> } = {
        version: 2,
        models: { _default: "1" },
        plans: {
          pro: { id: "plan-pro", name: "Pro Plan", freeAllowance: 500 },
        },
      };
      await store.setActivePricing(v2Config);
      await store.setUserPlan("user-1", "plan-pro");

      const allowance = await store.checkAllowance("user-1");
      expect(allowance.planId).toBe("plan-pro");
      expect(allowance.allowanceRemaining).toBe(500);
      expect(allowance.periodStart).toBeTruthy();
      expect(allowance.periodEnd).toBeTruthy();
    });

    it("checkAllowance returns zero for user with no plan", async () => {
      const allowance = await store.checkAllowance("no-plan-user");
      expect(allowance.allowanceRemaining).toBe(0);
    });

    it("incrementUsageWindow reduces remaining allowance", async () => {
      const v2Config: PricingConfigData & { plans: Record<string, PlanDefinition> } = {
        version: 2,
        models: { _default: "1" },
        plans: {
          basic: { id: "plan-basic", name: "Basic", freeAllowance: 200 },
        },
      };
      await store.setActivePricing(v2Config);
      await store.setUserPlan("user-1", "plan-basic");

      await store.incrementUsageWindow("user-1", "plan-basic", 50);
      const allowance = await store.checkAllowance("user-1");
      expect(allowance.allowanceRemaining).toBe(150);

      await store.incrementUsageWindow("user-1", "plan-basic", 30);
      const allowance2 = await store.checkAllowance("user-1");
      expect(allowance2.allowanceRemaining).toBe(120);
    });
  });

  describe("refunds", () => {
    it("refunds a full deduction and restores balance", async () => {
      await store.addCredits("user-1", 100, "purchase");
      // Full lifecycle: reserve + deduct
      const reserve = await store.reserveCredits("user-1", 30, "usage");
      const deduct = await store.deductCredits("user-1", reserve.reservationId, 30);
      const balanceAfterDeduct = (await store.getBalance("user-1")).balance;
      expect(balanceAfterDeduct).toBe(70);

      // Refund
      const refund = await store.refundCredits(deduct.transactionId);
      expect(refund.error).toBeUndefined();
      expect(refund.amount).toBe(30);
      const balanceAfterRefund = await store.getBalance("user-1");
      expect(balanceAfterRefund.balance).toBe(100);
    });

    it("partial refund restores partial amount", async () => {
      await store.addCredits("user-1", 100);
      const reserve = await store.reserveCredits("user-1", 50, "usage");
      const deduct = await store.deductCredits("user-1", reserve.reservationId, 50);

      const refund = await store.refundCredits(deduct.transactionId, 20);
      expect(refund.error).toBeUndefined();
      expect(refund.amount).toBe(20);
      expect((await store.getBalance("user-1")).balance).toBe(70); // 50 + 20
    });

    it("double refund returns error", async () => {
      await store.addCredits("user-1", 100);
      const reserve = await store.reserveCredits("user-1", 30, "usage");
      const deduct = await store.deductCredits("user-1", reserve.reservationId, 30);

      const refund1 = await store.refundCredits(deduct.transactionId);
      expect(refund1.error).toBeUndefined();

      const refund2 = await store.refundCredits(deduct.transactionId);
      expect(refund2.error).toBe("already_refunded");
    });

    it("unknown transaction returns error", async () => {
      const refund = await store.refundCredits("non-existent-id");
      expect(refund.error).toBe("transaction_not_found");
    });
  });
});
