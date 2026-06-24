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
      expect(result.tablesCreated).toHaveLength(9);
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

  describe("credit expiry", () => {
    it("credits with 1s TTL expire on sweep", async () => {
      const expiresAt = new Date(Date.now() + 1);
      await store.addCredits("user-1", 100, "purchase", null, expiresAt);

      // Wait for expiry
      await new Promise((r) => setTimeout(r, 10));

      const result = await store.sweepExpiredCredits();
      expect(result.expiredCount).toBe(1);
      expect(result.expiredAmount).toBe(100);
      expect(result.dryRun).toBe(false);
      expect((await store.getBalance("user-1")).balance).toBe(0);
    });

    it("dryRun reports without modifying balance", async () => {
      const expiresAt = new Date(Date.now() + 1);
      await store.addCredits("user-1", 100, "purchase", null, expiresAt);

      await new Promise((r) => setTimeout(r, 10));

      const result = await store.sweepExpiredCredits(true);
      expect(result.expiredCount).toBe(1);
      expect(result.expiredAmount).toBe(100);
      expect(result.dryRun).toBe(true);
      // Balance unchanged
      expect((await store.getBalance("user-1")).balance).toBe(100);
    });

    it("credits without expiry never expire", async () => {
      await store.addCredits("user-1", 100);

      const result = await store.sweepExpiredCredits();
      expect(result.expiredCount).toBe(0);
      expect(result.expiredAmount).toBe(0);
      expect((await store.getBalance("user-1")).balance).toBe(100);
    });

    it("sweep with no expired returns zero", async () => {
      const result = await store.sweepExpiredCredits();
      expect(result.expiredCount).toBe(0);
      expect(result.expiredAmount).toBe(0);
    });

    it("partial expiry caps at current balance", async () => {
      const expiresAt = new Date(Date.now() + 1);
      await store.addCredits("user-1", 50, "purchase", null, expiresAt);
      await store.addCredits("user-1", 30, "purchase"); // no expiry

      await new Promise((r) => setTimeout(r, 10));

      const result = await store.sweepExpiredCredits();
      // 50 expired, balance is 80, so expire min(50, 80) = 50
      expect(result.expiredAmount).toBe(50);
      expect((await store.getBalance("user-1")).balance).toBe(30);
    });
  });

  describe("usage analytics", () => {
    it("spendByUser returns correct totals", async () => {
      await store.addCredits("user-1", 1000);
      await store.addCredits("user-2", 2000);

      // Create usage transactions via reserve + deduct
      const r1 = await store.reserveCredits("user-1", 100, "usage");
      await store.deductCredits("user-1", r1.reservationId, 100);
      const r2 = await store.reserveCredits("user-1", 50, "usage");
      await store.deductCredits("user-1", r2.reservationId, 50);
      const r3 = await store.reserveCredits("user-2", 200, "usage");
      await store.deductCredits("user-2", r3.reservationId, 200);

      const start = new Date(Date.now() - 1000);
      const end = new Date(Date.now() + 1000);
      const rows = await store.spendByUser(start, end);
      expect(rows).toHaveLength(2);

      const u1 = rows.find((r) => r.userId === "user-1");
      expect(u1).toBeDefined();
      expect(u1!.totalSpend).toBe(150); // 100 + 50
      expect(u1!.transactionCount).toBe(2);

      const u2 = rows.find((r) => r.userId === "user-2");
      expect(u2).toBeDefined();
      expect(u2!.totalSpend).toBe(200);
      expect(u2!.transactionCount).toBe(1);
    });

    it("spendByModel returns correct totals", async () => {
      await store.addCredits("user-1", 1000);
      const r1 = await store.reserveCredits("user-1", 100, "usage");
      await store.deductCredits("user-1", r1.reservationId, 100);
      const r2 = await store.reserveCredits("user-1", 50, "usage");
      await store.deductCredits("user-1", r2.reservationId, 50);

      const now = new Date();
      const rows = await store.spendByModel(
        new Date(now.getTime() - 1000),
        new Date(now.getTime() + 1000),
      );
      expect(rows.length).toBeGreaterThanOrEqual(1);
      // Both deductions have no model metadata in MemoryStore deductCredits
      const unknown = rows.find((r) => r.model === "unknown");
      expect(unknown).toBeDefined();
      expect(unknown!.totalSpend).toBe(150);
    });

    it("empty time window returns empty", async () => {
      await store.addCredits("user-1", 100);
      const r = await store.reserveCredits("user-1", 10, "usage");
      await store.deductCredits("user-1", r.reservationId, 10);

      const result = await store.spendByUser(new Date("2020-01-01"), new Date("2020-01-02"));
      expect(result).toHaveLength(0);
    });

    it("topUsers respects limit", async () => {
      await store.addCredits("user-1", 1000);
      await store.addCredits("user-2", 1000);
      await store.addCredits("user-3", 1000);

      const r1 = await store.reserveCredits("user-1", 300, "usage");
      await store.deductCredits("user-1", r1.reservationId, 300);
      const r2 = await store.reserveCredits("user-2", 200, "usage");
      await store.deductCredits("user-2", r2.reservationId, 200);
      const r3 = await store.reserveCredits("user-3", 100, "usage");
      await store.deductCredits("user-3", r3.reservationId, 100);

      const now = new Date();
      const top = await store.topUsers(
        2,
        new Date(now.getTime() - 1000),
        new Date(now.getTime() + 1000),
      );
      expect(top).toHaveLength(2);
      expect(top[0].totalSpend).toBeGreaterThanOrEqual(top[1].totalSpend);
    });

    it("dailySpend bucketing correct", async () => {
      await store.addCredits("user-1", 1000);
      const r = await store.reserveCredits("user-1", 75, "usage");
      await store.deductCredits("user-1", r.reservationId, 75);

      const now = new Date();
      const rows = await store.dailySpend(
        new Date(now.getTime() - 86400000),
        new Date(now.getTime() + 86400000),
      );
      expect(rows.length).toBeGreaterThanOrEqual(1);
      expect(rows[0].totalSpend).toBe(75);
      expect(rows[0].transactionCount).toBe(1);
    });
  });

  describe("team balance pools", () => {
    it("creates a team and returns its balance", async () => {
      const team = await store.createTeam("Engineering");
      expect(team.teamId).toBeTruthy();
      expect(team.name).toBe("Engineering");

      const balance = await store.getTeamBalance(team.teamId);
      expect(balance.name).toBe("Engineering");
      expect(balance.balance).toBe(0);
      expect(balance.memberCount).toBe(0);
    });

    it("createTeam with initial balance", async () => {
      const team = await store.createTeam("Pro Team", 1000);
      const balance = await store.getTeamBalance(team.teamId);
      expect(balance.balance).toBe(1000);
    });

    it("adds member and tracks member count", async () => {
      const team = await store.createTeam("Team A", 500);
      await store.addTeamMember(team.teamId, "user-1", "admin");
      await store.addTeamMember(team.teamId, "user-2", "member");

      const balance = await store.getTeamBalance(team.teamId);
      expect(balance.memberCount).toBe(2);

      const members = await store.getTeamMembers(team.teamId);
      expect(members).toHaveLength(2);
      expect(members[0].role).toBe("admin");
    });

    it("getTeamMembers with spend cap", async () => {
      const team = await store.createTeam("Capped Team", 5000);
      await store.addTeamMember(team.teamId, "user-1", "member", 100);
      const members = await store.getTeamMembers(team.teamId);
      expect(members[0].spendCap).toBe(100);
    });

    it("deductTeam debits team pool not user balance", async () => {
      await store.addCredits("user-1", 100); // user balance
      const team = await store.createTeam("Pool", 500);
      await store.addTeamMember(team.teamId, "user-1", "member");

      const result = await store.deductTeam(team.teamId, "user-1", 50);
      expect(result.error).toBeUndefined();
      expect(result.amount).toBe(-50);
      expect(result.teamBalanceAfter).toBe(450);

      // User balance unchanged
      const userBal = await store.getBalance("user-1");
      expect(userBal.balance).toBe(100);
    });

    it("deductTeam insufficient team balance returns error", async () => {
      const team = await store.createTeam("Poor Team", 10);
      await store.addTeamMember(team.teamId, "user-1", "member");
      const result = await store.deductTeam(team.teamId, "user-1", 100);
      expect(result.error).toBe("insufficient_team_balance");
    });

    it("deductTeam user not in team returns error", async () => {
      const team = await store.createTeam("Closed Team", 500);
      const result = await store.deductTeam(team.teamId, "user-1", 10);
      expect(result.error).toBe("user_not_in_team");
    });

    it("deductTeam spend cap blocks overspend", async () => {
      const team = await store.createTeam("Capped", 1000);
      await store.addTeamMember(team.teamId, "user-1", "member", 50);

      // First deduct: 30 (within cap)
      const r1 = await store.deductTeam(team.teamId, "user-1", 30);
      expect(r1.error).toBeUndefined();
      expect(r1.teamBalanceAfter).toBe(970);

      // Second deduct: 30 (50 cap - 30 spent = 20 remaining, 30 > 20)
      const r2 = await store.deductTeam(team.teamId, "user-1", 30);
      expect(r2.error).toBe("spend_cap_exceeded");
    });

    it("deductTeam non-existent team returns error", async () => {
      const result = await store.deductTeam("no-such-team", "user-1", 10);
      expect(result.error).toBe("team_not_found");
    });
  });

  describe("checkSpendCap", () => {
    it("returns no cap when no caps configured", async () => {
      const result = await store.checkSpendCap("user-1");
      expect(result.capped).toBe(false);
      expect(result.action).toBeNull();
    });

    it("denies when spend exceeds daily cap", async () => {
      store.setSpendCap({ userId: "user-1", type: "daily", limit: 100, action: "deny" });
      // Simulate existing spend via transactions
      const result = await store.checkSpendCap("user-1", null, 101);
      expect(result.capped).toBe(true);
      expect(result.action).toBe("deny");
    });

    it("allows when spend is within daily cap", async () => {
      store.setSpendCap({ userId: "user-1", type: "daily", limit: 100, action: "deny" });
      const result = await store.checkSpendCap("user-1", null, 50);
      expect(result.capped).toBe(false);
    });

    it("warn action allows through", async () => {
      store.setSpendCap({ userId: "user-1", type: "daily", limit: 100, action: "warn" });
      const result = await store.checkSpendCap("user-1", null, 101);
      expect(result.capped).toBe(false);
      expect(result.action).toBe("warn");
    });

    it("notify action allows through", async () => {
      store.setSpendCap({ userId: "user-1", type: "daily", limit: 100, action: "notify" });
      const result = await store.checkSpendCap("user-1", null, 101);
      expect(result.capped).toBe(false);
      expect(result.action).toBe("notify");
    });

    it("per-model cap is independent of global cap", async () => {
      store.setSpendCap({
        userId: "user-1",
        type: "daily",
        limit: 50,
        action: "deny",
        model: "gpt-4",
      });
      store.setSpendCap({ userId: "user-1", type: "daily", limit: 200, action: "deny" });

      // Striped model within per-model cap
      const r1 = await store.checkSpendCap("user-1", "gpt-4", 30);
      expect(r1.capped).toBe(false);

      // Exceeds per-model cap but within global
      const r2 = await store.checkSpendCap("user-1", "gpt-4", 60);
      expect(r2.capped).toBe(true);
      expect(r2.model).toBe("gpt-4");

      // Other model within global cap only
      const r3 = await store.checkSpendCap("user-1", "claude-3", 150);
      expect(r3.capped).toBe(false);
    });

    it("caps only apply to matching user", async () => {
      store.setSpendCap({ userId: "user-1", type: "daily", limit: 100, action: "deny" });
      const result = await store.checkSpendCap("user-2", null, 200);
      expect(result.capped).toBe(false);
    });

    it("accounts for existing spend in current window", async () => {
      store.setSpendCap({ userId: "user-1", type: "daily", limit: 100, action: "deny" });
      const result = await store.checkSpendCap("user-1", null, 110);
      // With no existing transactions, 110 > 100 → denied
      expect(result.capped).toBe(true);
      expect(result.currentSpend).toBe(0);
      expect(result.limit).toBe(100);
    });
  });
});
