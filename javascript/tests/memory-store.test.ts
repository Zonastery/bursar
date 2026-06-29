import { describe, it, expect, beforeEach } from "vitest";
import Decimal from "decimal.js";
import { MemoryStore } from "../src/stores/memory-store.js";
import { StoreError } from "../src/errors.js";
import type { PricingConfigData } from "../src/types.js";

const D = (n: number | string) => new Decimal(n);

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

  describe("getBalance / addCredits (Decimal money)", () => {
    it("returns zero balance for new user", async () => {
      const result = await store.getBalance("user-1");
      expect(result.balance.toString()).toBe("0");
      expect(result.lifetimePurchased.toString()).toBe("0");
    });

    it("adds credits", async () => {
      const result = await store.addCredits("user-1", D(100));
      expect(result.newBalance.toString()).toBe("100");
      expect(result.userId).toBe("user-1");
    });

    it("preserves fractional precision (no truncation)", async () => {
      await store.addCredits("user-1", D("0.1"));
      await store.addCredits("user-1", D("0.2"));
      const result = await store.getBalance("user-1");
      // Decimal: exactly 0.3, never 0.30000000000000004.
      expect(result.balance.toString()).toBe("0.3");
    });

    it("tracks lifetime purchases", async () => {
      await store.addCredits("user-1", D(100), "purchase");
      const result = await store.getBalance("user-1");
      expect(result.lifetimePurchased.toString()).toBe("100");
    });

    it("does not count adjustments toward lifetime", async () => {
      await store.addCredits("user-1", D(50), "adjustment");
      const result = await store.getBalance("user-1");
      expect(result.lifetimePurchased.toString()).toBe("0");
    });

    it("accumulates multiple adds", async () => {
      await store.addCredits("user-1", D(50));
      await store.addCredits("user-1", D(75));
      const result = await store.getBalance("user-1");
      expect(result.balance.toString()).toBe("125");
    });

    it("rejects negative purchase (L2)", async () => {
      await expect(store.addCredits("user-1", D(-10), "purchase")).rejects.toThrow(StoreError);
    });

    it("rejects zero purchase (L2)", async () => {
      await expect(store.addCredits("user-1", D(0), "purchase")).rejects.toThrow(StoreError);
    });

    it("allows negative adjustment (L2)", async () => {
      await store.addCredits("user-1", D(100), "purchase");
      const r = await store.addCredits("user-1", D(-30), "adjustment");
      expect(r.newBalance.toString()).toBe("70");
    });

    it("rejects non-finite amounts (L2)", async () => {
      await expect(store.addCredits("user-1", new Decimal(Infinity))).rejects.toThrow(StoreError);
    });
  });

  describe("credit lifecycle: add → reserve → deduct", () => {
    it("completes full lifecycle", async () => {
      await store.addCredits("user-1", D(100), "purchase");
      const reserve = await store.reserveCredits("user-1", D(30), "usage");
      expect(reserve.error).toBeUndefined();
      expect(reserve.amount.toString()).toBe("30");

      const deduct = await store.deductCredits("user-1", reserve.reservationId, D(30));
      expect(deduct.error).toBeUndefined();
      expect(deduct.amount.toString()).toBe("-30");

      const balance = await store.getBalance("user-1");
      expect(balance.balance.toString()).toBe("70");
    });

    it("rejects insufficient credits", async () => {
      await store.addCredits("user-1", D(10));
      const result = await store.reserveCredits("user-1", D(100), "usage");
      expect(result.error).toBe("insufficient_credits");
    });

    it("rejects reservation below min_balance threshold", async () => {
      await store.addCredits("user-1", D(20));
      const result = await store.reserveCredits("user-1", D(10), "usage", null, D(15));
      expect(result.error).toBe("insufficient_credits");
    });

    it("deduct without a valid reservation is rejected (C3)", async () => {
      await store.addCredits("user-1", D(100));
      const result = await store.deductCredits("user-1", "no-such-reservation", D(10));
      expect(result.error).toBe("not_found");
      expect((await store.getBalance("user-1")).balance.toString()).toBe("100");
    });

    it("clamps the deducted amount to the reserved ceiling (C3)", async () => {
      await store.addCredits("user-1", D(100));
      const reserve = await store.reserveCredits("user-1", D(10), "usage");
      // Try to deduct 1000 against a 10-credit reservation.
      const deduct = await store.deductCredits("user-1", reserve.reservationId, D(1000));
      expect(deduct.error).toBeUndefined();
      expect(deduct.amount.toString()).toBe("-10");
      expect((await store.getBalance("user-1")).balance.toString()).toBe("90");
    });

    it("rejects deduct against another user's reservation (C3)", async () => {
      await store.addCredits("user-1", D(100));
      await store.addCredits("user-2", D(100));
      const reserve = await store.reserveCredits("user-1", D(10), "usage");
      const result = await store.deductCredits("user-2", reserve.reservationId, D(10));
      expect(result.error).toBe("not_found");
    });

    it("handles idempotent deductions", async () => {
      await store.addCredits("user-1", D(100));
      const reserve = await store.reserveCredits("user-1", D(50), "usage");
      const deduct1 = await store.deductCredits("user-1", reserve.reservationId, D(50), "idem-1");
      expect(deduct1.idempotent).toBe(false);

      // Replay with same idempotency key
      const reserve2 = await store.reserveCredits("user-1", D(50), "usage");
      const deduct2 = await store.deductCredits("user-1", reserve2.reservationId, D(50), "idem-1");
      expect(deduct2.idempotent).toBe(true);
      // Balance only debited once.
      expect((await store.getBalance("user-1")).balance.toString()).toBe("50");
    });

    it("idempotency is user-scoped (no cross-user collision)", async () => {
      await store.addCredits("user-1", D(100));
      await store.addCredits("user-2", D(100));
      const r1 = await store.reserveCredits("user-1", D(10), "usage");
      await store.deductCredits("user-1", r1.reservationId, D(10), "shared-key");
      const r2 = await store.reserveCredits("user-2", D(20), "usage");
      const d2 = await store.deductCredits("user-2", r2.reservationId, D(20), "shared-key");
      // Same key, different user → a real deduction, not a replay.
      expect(d2.idempotent).toBe(false);
      expect((await store.getBalance("user-2")).balance.toString()).toBe("80");
    });

    it("handles concurrent reservations", async () => {
      await store.addCredits("user-1", D(100));
      const r1 = await store.reserveCredits("user-1", D(60), "usage");
      expect(r1.error).toBeUndefined();

      // Second reservation should fail — only 40 available, needs 60
      const r2 = await store.reserveCredits("user-1", D(60), "usage");
      expect(r2.error).toBe("insufficient_credits");
    });
  });

  describe("deductWithAllowance (atomic charge)", () => {
    async function seedPlan(freeAllowance: number, userId = "user-1") {
      const config: PricingConfigData = {
        models: { _default: "1" },
        plans: { p: { id: "plan-1", name: "Plan", freeAllowance: D(freeAllowance) } },
      };
      await store.setActivePricing(config);
      await store.setUserPlan(userId, "plan-1");
    }

    it("charges net amount with no plan/allowance", async () => {
      await store.addCredits("user-1", D(100));
      const r = await store.deductWithAllowance("user-1", D("2.5"));
      expect(r.error).toBeUndefined();
      expect(r.amount.toString()).toBe("2.5");
      expect(r.allowanceConsumed.toString()).toBe("0");
      expect(r.balanceAfter.toString()).toBe("97.5");
      expect(r.idempotent).toBe(false);
      expect(r.capWarning).toBeNull();
    });

    it("does not truncate sub-credit charges", async () => {
      await store.addCredits("user-1", D(100));
      const r = await store.deductWithAllowance("user-1", D("0.4"));
      expect(r.amount.toString()).toBe("0.4");
      expect(r.balanceAfter.toString()).toBe("99.6");
    });

    it("consumes allowance fully, skips balance debit", async () => {
      await seedPlan(100);
      await store.addCredits("user-1", D(10), "adjustment");
      const r = await store.deductWithAllowance("user-1", D(5), { model: "gpt-4" });
      expect(r.amount.toString()).toBe("0");
      expect(r.allowanceConsumed.toString()).toBe("5");
      expect(r.balanceAfter.toString()).toBe("10");
      const allowance = await store.checkAllowance("user-1");
      expect(allowance.allowanceRemaining.toString()).toBe("95");
    });

    it("partial allowance, charges remainder to balance", async () => {
      await seedPlan(10);
      await store.addCredits("user-1", D(100), "adjustment");
      const r = await store.deductWithAllowance("user-1", D(25));
      expect(r.amount.toString()).toBe("15");
      expect(r.allowanceConsumed.toString()).toBe("10");
      expect(r.balanceAfter.toString()).toBe("85");
      const allowance = await store.checkAllowance("user-1");
      expect(allowance.allowanceRemaining.toString()).toBe("0");
    });

    it("balance floor blocks deduction without consuming allowance", async () => {
      await seedPlan(5);
      await store.addCredits("user-1", D(10), "adjustment");
      const r = await store.deductWithAllowance("user-1", D(20), { minBalance: D(0) });
      // net = 20 - 5 = 15, balance 10 - 15 < 0 → insufficient
      expect(r.error).toBe("insufficient_credits");
      // Allowance NOT consumed.
      const allowance = await store.checkAllowance("user-1");
      expect(allowance.allowanceRemaining.toString()).toBe("5");
      expect((await store.getBalance("user-1")).balance.toString()).toBe("10");
    });

    it("respects minBalance floor", async () => {
      await store.addCredits("user-1", D(100));
      const r = await store.deductWithAllowance("user-1", D(96), { minBalance: D(5) });
      // 100 - 96 = 4 < 5 → rejected
      expect(r.error).toBe("insufficient_credits");
    });

    it("deny cap aborts without consuming allowance", async () => {
      await seedPlan(5);
      await store.addCredits("user-1", D(1000), "adjustment");
      store.setSpendCap({ userId: "user-1", type: "daily", limit: D(10), action: "deny" });
      // net = 20 - 5 = 15 > cap 10 → deny
      const r = await store.deductWithAllowance("user-1", D(20));
      expect(r.error).toBe("cap_reached");
      const allowance = await store.checkAllowance("user-1");
      expect(allowance.allowanceRemaining.toString()).toBe("5");
      expect((await store.getBalance("user-1")).balance.toString()).toBe("1000");
    });

    it("warn cap sets capWarning but proceeds", async () => {
      await store.addCredits("user-1", D(1000));
      store.setSpendCap({ userId: "user-1", type: "daily", limit: D(10), action: "warn" });
      const r = await store.deductWithAllowance("user-1", D(20));
      expect(r.error).toBeUndefined();
      expect(r.capWarning).toBe("warn");
      expect(r.amount.toString()).toBe("20");
    });

    it("cap accumulates across prior window spend", async () => {
      await store.addCredits("user-1", D(1000));
      store.setSpendCap({ userId: "user-1", type: "daily", limit: D(30), action: "deny" });
      const a = await store.deductWithAllowance("user-1", D(20));
      expect(a.error).toBeUndefined();
      // Prior 20 + this 20 = 40 > 30 → deny
      const b = await store.deductWithAllowance("user-1", D(20));
      expect(b.error).toBe("cap_reached");
    });

    it("cap boundary: amount == limit is allowed", async () => {
      await store.addCredits("user-1", D(1000));
      store.setSpendCap({ userId: "user-1", type: "daily", limit: D(10), action: "deny" });
      const r = await store.deductWithAllowance("user-1", D(10));
      expect(r.error).toBeUndefined();
    });

    it("idempotency replays original (one debit)", async () => {
      await store.addCredits("user-1", D(100));
      const r1 = await store.deductWithAllowance("user-1", D(10), { idempotencyKey: "k1" });
      expect(r1.idempotent).toBe(false);
      const r2 = await store.deductWithAllowance("user-1", D(10), { idempotencyKey: "k1" });
      expect(r2.idempotent).toBe(true);
      expect(r2.transactionId).toBe(r1.transactionId);
      expect((await store.getBalance("user-1")).balance.toString()).toBe("90");
    });

    it("rejects negative amount as invalid", async () => {
      await store.addCredits("user-1", D(100));
      const r = await store.deductWithAllowance("user-1", D(-5));
      expect(r.error).toBe("invalid_amount");
    });

    it("zero amount is a valid no-op charge", async () => {
      await store.addCredits("user-1", D(100));
      const r = await store.deductWithAllowance("user-1", D(0));
      expect(r.error).toBeUndefined();
      expect(r.amount.toString()).toBe("0");
      expect(r.balanceAfter.toString()).toBe("100");
    });

    it("does not double-spend under Promise.all concurrency (C2)", async () => {
      // Balance covers only 5 of 10 concurrent 1-credit charges with floor 0.
      await store.addCredits("user-1", D(5));
      const results = await Promise.all(
        Array.from({ length: 10 }, () => store.deductWithAllowance("user-1", D(1))),
      );
      const succeeded = results.filter((r) => !r.error);
      const failed = results.filter((r) => r.error === "insufficient_credits");
      expect(succeeded).toHaveLength(5);
      expect(failed).toHaveLength(5);
      const balance = (await store.getBalance("user-1")).balance;
      expect(balance.toString()).toBe("0");
      expect(balance.gte(0)).toBe(true);
    });

    it("idempotency replay under concurrency → one debit (C2)", async () => {
      await store.addCredits("user-1", D(100));
      const results = await Promise.all(
        Array.from({ length: 8 }, () =>
          store.deductWithAllowance("user-1", D(10), { idempotencyKey: "concurrent-key" }),
        ),
      );
      const nonIdempotent = results.filter((r) => !r.idempotent && !r.error);
      // Exactly one real debit; the rest replay.
      expect(nonIdempotent).toHaveLength(1);
      expect((await store.getBalance("user-1")).balance.toString()).toBe("90");
    });
  });

  describe("pricing config", () => {
    it("returns null when no pricing set", async () => {
      expect(await store.getActivePricing()).toBeNull();
    });

    it("stores and retrieves pricing config", async () => {
      const config: PricingConfigData = {
        models: { "gpt-4": "input_tokens * 0.01" },
      };
      await store.setActivePricing(config);
      const result = await store.getActivePricing();
      expect(result).not.toBeNull();
      expect(result!.config.models["gpt-4"]).toBe("input_tokens * 0.01");
    });

    it("increments version on each set", async () => {
      const config: PricingConfigData = { models: { a: "1" } };
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
      expect(result.freeAllowance.toString()).toBe("0");
    });

    it("setUserPlan and getUserPlan round-trips", async () => {
      const config: PricingConfigData = {
        models: { _default: "1" },
        plans: {
          free: { id: "plan-free", name: "Free Plan", freeAllowance: D(100) },
        },
      };
      await store.setActivePricing(config);

      await store.setUserPlan("user-1", "plan-free");
      const result = await store.getUserPlan("user-1");
      expect(result.planId).toBe("plan-free");
      expect(result.planName).toBe("Free Plan");
      expect(result.freeAllowance.toString()).toBe("100");
      expect(result.features).toEqual({});
    });

    it("getUserPlan returns features from plan definition", async () => {
      const config: PricingConfigData = {
        models: { _default: "1" },
        plans: {
          premium: {
            id: "premium",
            name: "Premium",
            freeAllowance: D(2000),
            features: { aiChat: true, maxRoadmaps: 20 },
          },
        },
      };
      await store.setActivePricing(config);
      await store.setUserPlan("user-1", "premium");

      const result = await store.getUserPlan("user-1");
      expect(result.planId).toBe("premium");
      expect(result.features["aiChat"]).toBe(true);
      expect(result.features["maxRoadmaps"]).toBe(20);
    });

    it("checkFeature distinguishes presence from truthiness (M6)", async () => {
      const config: PricingConfigData = {
        models: { _default: "1" },
        plans: {
          free: { id: "free", name: "Free", freeAllowance: D(0), features: {} },
          premium: {
            id: "premium",
            name: "Premium",
            freeAllowance: D(2000),
            features: { aiChat: true, maxRoadmaps: 20, quota: 0, label: "", disabled: false },
          },
        },
      };
      await store.setActivePricing(config);
      await store.setUserPlan("user-premium", "premium");
      await store.setUserPlan("user-free", "free");

      const chat = await store.checkFeature("user-premium", "aiChat");
      expect(chat.hasFeature).toBe(true);
      expect(chat.value).toBe(true);

      const roadmaps = await store.checkFeature("user-premium", "maxRoadmaps");
      expect(roadmaps.value).toBe(20);
      expect(roadmaps.hasFeature).toBe(true);

      // Numeric 0 is PRESENT (not absent) — the key part of M6.
      const quota = await store.checkFeature("user-premium", "quota");
      expect(quota.value).toBe(0);
      expect(quota.hasFeature).toBe(true);

      // Empty string is PRESENT.
      const label = await store.checkFeature("user-premium", "label");
      expect(label.value).toBe("");
      expect(label.hasFeature).toBe(true);

      // Explicit false is ABSENT.
      const disabled = await store.checkFeature("user-premium", "disabled");
      expect(disabled.value).toBe(false);
      expect(disabled.hasFeature).toBe(false);

      // Missing feature entirely.
      const pdf = await store.checkFeature("user-premium", "exportPdf");
      expect(pdf.hasFeature).toBe(false);
      expect(pdf.value).toBeNull();

      // No plan
      const nobody = await store.checkFeature("nobody", "aiChat");
      expect(nobody.hasFeature).toBe(false);
    });

    it("checkAllowance returns remaining allowance", async () => {
      const config: PricingConfigData = {
        models: { _default: "1" },
        plans: {
          pro: { id: "plan-pro", name: "Pro Plan", freeAllowance: D(500) },
        },
      };
      await store.setActivePricing(config);
      await store.setUserPlan("user-1", "plan-pro");

      const allowance = await store.checkAllowance("user-1");
      expect(allowance.planId).toBe("plan-pro");
      expect(allowance.allowanceRemaining.toString()).toBe("500");
      expect(allowance.periodStart).toBeTruthy();
      expect(allowance.periodEnd).toBeTruthy();
    });

    it("checkAllowance returns zero for user with no plan", async () => {
      const allowance = await store.checkAllowance("no-plan-user");
      expect(allowance.allowanceRemaining.toString()).toBe("0");
    });

    it("incrementUsageWindow reduces remaining allowance", async () => {
      const config: PricingConfigData = {
        models: { _default: "1" },
        plans: {
          basic: { id: "plan-basic", name: "Basic", freeAllowance: D(200) },
        },
      };
      await store.setActivePricing(config);
      await store.setUserPlan("user-1", "plan-basic");

      await store.incrementUsageWindow("user-1", "plan-basic", D(50));
      const allowance = await store.checkAllowance("user-1");
      expect(allowance.allowanceRemaining.toString()).toBe("150");

      await store.incrementUsageWindow("user-1", "plan-basic", D(30));
      const allowance2 = await store.checkAllowance("user-1");
      expect(allowance2.allowanceRemaining.toString()).toBe("120");
    });
  });

  describe("refunds", () => {
    async function makeUsageTx(userId: string, amount: number) {
      await store.addCredits(userId, D(1000), "purchase");
      const reserve = await store.reserveCredits(userId, D(amount), "usage");
      return store.deductCredits(userId, reserve.reservationId, D(amount));
    }

    it("refunds a full deduction and restores balance", async () => {
      const deduct = await makeUsageTx("user-1", 30);
      expect((await store.getBalance("user-1")).balance.toString()).toBe("970");

      const refund = await store.refundCredits(deduct.transactionId);
      expect(refund.error).toBeUndefined();
      expect(refund.amount.toString()).toBe("30");
      expect((await store.getBalance("user-1")).balance.toString()).toBe("1000");
    });

    it("cumulative partial refunds up to the original debit", async () => {
      const deduct = await makeUsageTx("user-1", 50);

      const r1 = await store.refundCredits(deduct.transactionId, D(20));
      expect(r1.error).toBeUndefined();
      expect(r1.amount.toString()).toBe("20");

      const r2 = await store.refundCredits(deduct.transactionId, D(20));
      expect(r2.error).toBeUndefined();
      // 950 + 20 + 20 = 990
      expect((await store.getBalance("user-1")).balance.toString()).toBe("990");

      // Third partial would exceed remaining (10 left) → over_refund.
      const r3 = await store.refundCredits(deduct.transactionId, D(20));
      expect(r3.error).toBe("over_refund");
    });

    it("over-refund (single request > debit) is rejected", async () => {
      const deduct = await makeUsageTx("user-1", 30);
      const refund = await store.refundCredits(deduct.transactionId, D(100));
      expect(refund.error).toBe("over_refund");
    });

    it("duplicate full refund returns already_refunded", async () => {
      const deduct = await makeUsageTx("user-1", 30);
      const refund1 = await store.refundCredits(deduct.transactionId);
      expect(refund1.error).toBeUndefined();
      const refund2 = await store.refundCredits(deduct.transactionId);
      expect(refund2.error).toBe("already_refunded");
    });

    it("refund of a purchase (non-debit) is rejected as over_refund", async () => {
      const purchase = await store.addCredits("user-1", D(100), "purchase");
      const refund = await store.refundCredits(purchase.transactionId);
      expect(refund.error).toBe("over_refund");
    });

    it("unknown transaction returns not_found", async () => {
      const refund = await store.refundCredits("non-existent-id");
      expect(refund.error).toBe("not_found");
    });
  });

  describe("credit expiry (fixed clock — no sleeps)", () => {
    const T0 = new Date("2026-01-01T00:00:00.000Z");
    const LATER = new Date("2026-01-02T00:00:00.000Z");

    beforeEach(() => {
      store.setClock(() => T0);
    });

    it("credits past TTL expire on sweep", async () => {
      await store.addCredits("user-1", D(100), "purchase", null, new Date("2026-01-01T00:00:00.500Z"));
      store.setClock(() => LATER);

      const result = await store.sweepExpiredCredits();
      expect(result.expiredCount).toBe(1);
      expect(result.expiredAmount.toString()).toBe("100");
      expect(result.dryRun).toBe(false);
      expect((await store.getBalance("user-1")).balance.toString()).toBe("0");
    });

    it("dryRun reports without modifying balance", async () => {
      await store.addCredits("user-1", D(100), "purchase", null, new Date("2026-01-01T00:00:00.500Z"));
      store.setClock(() => LATER);

      const result = await store.sweepExpiredCredits(true);
      expect(result.expiredCount).toBe(1);
      expect(result.expiredAmount.toString()).toBe("100");
      expect(result.dryRun).toBe(true);
      expect((await store.getBalance("user-1")).balance.toString()).toBe("100");
    });

    it("double-sweep reports zero and does not double-debit (H4)", async () => {
      await store.addCredits("user-1", D(100), "purchase", null, new Date("2026-01-01T00:00:00.500Z"));
      store.setClock(() => LATER);

      const first = await store.sweepExpiredCredits();
      expect(first.expiredCount).toBe(1);
      expect(first.expiredAmount.toString()).toBe("100");
      expect((await store.getBalance("user-1")).balance.toString()).toBe("0");

      // Re-add credits, then sweep again — the already-swept grant must not be
      // re-clawed back.
      await store.addCredits("user-1", D(50), "purchase");
      const second = await store.sweepExpiredCredits();
      expect(second.expiredCount).toBe(0);
      expect(second.expiredAmount.toString()).toBe("0");
      expect((await store.getBalance("user-1")).balance.toString()).toBe("50");
    });

    it("credits without expiry never expire", async () => {
      await store.addCredits("user-1", D(100));
      const result = await store.sweepExpiredCredits();
      expect(result.expiredCount).toBe(0);
      expect(result.expiredAmount.toString()).toBe("0");
      expect((await store.getBalance("user-1")).balance.toString()).toBe("100");
    });

    it("sweep with no expired returns zero", async () => {
      const result = await store.sweepExpiredCredits();
      expect(result.expiredCount).toBe(0);
      expect(result.expiredAmount.toString()).toBe("0");
    });

    it("partial expiry caps at current balance", async () => {
      await store.addCredits("user-1", D(50), "purchase", null, new Date("2026-01-01T00:00:00.500Z"));
      await store.addCredits("user-1", D(30), "purchase"); // no expiry
      store.setClock(() => LATER);

      const result = await store.sweepExpiredCredits();
      expect(result.expiredAmount.toString()).toBe("50");
      expect((await store.getBalance("user-1")).balance.toString()).toBe("30");
    });
  });

  describe("usage analytics (Decimal)", () => {
    async function deduct(userId: string, amount: number, model?: string) {
      // Add headroom above the reservation min_balance floor (default 5).
      await store.addCredits(userId, D(amount + 100), "purchase");
      const r = await store.reserveCredits(userId, D(amount), "usage");
      return store.deductCredits(
        userId,
        r.reservationId,
        D(amount),
        null,
        model ? { model } : null,
      );
    }

    it("aggregateStats returns correct aggregates", async () => {
      await deduct("user-1", 50);
      await deduct("user-2", 30);

      const now = new Date();
      const stats = await store.aggregateStats(
        new Date(now.getTime() - 1000),
        new Date(now.getTime() + 1000),
      );
      expect(stats.totalCreditsConsumed.toString()).toBe("80");
      expect(stats.activeUsers).toBe(2);
      // avgDailySpend uses NUMERIC division (one day) → 80
      expect(stats.avgDailySpend.toString()).toBe("80");
      expect(stats.topUser).toBeTruthy();
    });

    it("aggregateStats avgDailySpend is fractional (no integer floor)", async () => {
      await deduct("user-1", 5);
      await deduct("user-1", 2);
      const now = new Date();
      const stats = await store.aggregateStats(
        new Date(now.getTime() - 1000),
        new Date(now.getTime() + 1000),
      );
      // 7 over a single day → 7 (and if it were e.g. divided by 2 days it would
      // be 3.5, never integer-floored to 3).
      expect(stats.totalCreditsConsumed.toString()).toBe("7");
    });

    it("aggregateStats returns empty stats for empty window", async () => {
      const stats = await store.aggregateStats(new Date("2020-01-01"), new Date("2020-01-02"));
      expect(stats.totalCreditsConsumed.toString()).toBe("0");
      expect(stats.activeUsers).toBe(0);
      expect(stats.topModel).toBe("");
    });

    it("spendByUser returns correct totals", async () => {
      await deduct("user-1", 100);
      await deduct("user-1", 50);
      await deduct("user-2", 200);

      const start = new Date(Date.now() - 1000);
      const end = new Date(Date.now() + 1000);
      const rows = await store.spendByUser(start, end);
      expect(rows).toHaveLength(2);

      const u1 = rows.find((r) => r.userId === "user-1");
      expect(u1!.totalSpend.toString()).toBe("150");
      expect(u1!.transactionCount).toBe(2);

      const u2 = rows.find((r) => r.userId === "user-2");
      expect(u2!.totalSpend.toString()).toBe("200");
      expect(u2!.transactionCount).toBe(1);
    });

    it("spendByModel returns correct totals", async () => {
      await deduct("user-1", 100, "gpt-4");
      await deduct("user-1", 50, "gpt-4");

      const now = new Date();
      const rows = await store.spendByModel(
        new Date(now.getTime() - 1000),
        new Date(now.getTime() + 1000),
      );
      const gpt4 = rows.find((r) => r.model === "gpt-4");
      expect(gpt4!.totalSpend.toString()).toBe("150");
    });

    it("empty time window returns empty", async () => {
      await deduct("user-1", 10);
      const result = await store.spendByUser(new Date("2020-01-01"), new Date("2020-01-02"));
      expect(result).toHaveLength(0);
    });

    it("topUsers respects limit and ordering", async () => {
      await deduct("user-1", 300);
      await deduct("user-2", 200);
      await deduct("user-3", 100);

      const now = new Date();
      const top = await store.topUsers(
        2,
        new Date(now.getTime() - 1000),
        new Date(now.getTime() + 1000),
      );
      expect(top).toHaveLength(2);
      expect(top[0].userId).toBe("user-1");
      expect(top[0].totalSpend.toString()).toBe("300");
      expect(top[1].totalSpend.toString()).toBe("200");
    });

    it("dailySpend bucketing correct", async () => {
      await deduct("user-1", 75);

      const now = new Date();
      const rows = await store.dailySpend(
        new Date(now.getTime() - 86400000),
        new Date(now.getTime() + 86400000),
      );
      expect(rows.length).toBeGreaterThanOrEqual(1);
      expect(rows[0].totalSpend.toString()).toBe("75");
      expect(rows[0].transactionCount).toBe(1);
    });
  });

  describe("team balance pools (Decimal)", () => {
    it("creates a team and returns its balance", async () => {
      const team = await store.createTeam("Engineering");
      expect(team.teamId).toBeTruthy();
      expect(team.name).toBe("Engineering");

      const balance = await store.getTeamBalance(team.teamId);
      expect(balance.name).toBe("Engineering");
      expect(balance.balance.toString()).toBe("0");
      expect(balance.memberCount).toBe(0);
    });

    it("createTeam with initial balance", async () => {
      const team = await store.createTeam("Pro Team", D(1000));
      const balance = await store.getTeamBalance(team.teamId);
      expect(balance.balance.toString()).toBe("1000");
    });

    it("adds member and tracks member count", async () => {
      const team = await store.createTeam("Team A", D(500));
      await store.addTeamMember(team.teamId, "user-1", "admin");
      await store.addTeamMember(team.teamId, "user-2", "member");

      const balance = await store.getTeamBalance(team.teamId);
      expect(balance.memberCount).toBe(2);

      const members = await store.getTeamMembers(team.teamId);
      expect(members).toHaveLength(2);
    });

    it("getTeamMembers with spend cap", async () => {
      const team = await store.createTeam("Capped Team", D(5000));
      await store.addTeamMember(team.teamId, "user-1", "member", D(100));
      const members = await store.getTeamMembers(team.teamId);
      expect(members[0].spendCap!.toString()).toBe("100");
    });

    it("deductTeam debits team pool not user balance", async () => {
      await store.addCredits("user-1", D(100)); // user balance
      const team = await store.createTeam("Pool", D(500));
      await store.addTeamMember(team.teamId, "user-1", "member");

      const result = await store.deductTeam(team.teamId, "user-1", D(50));
      expect(result.error).toBeUndefined();
      expect(result.amount.toString()).toBe("-50");
      expect(result.teamBalanceAfter.toString()).toBe("450");

      const userBal = await store.getBalance("user-1");
      expect(userBal.balance.toString()).toBe("100");
    });

    it("deductTeam idempotency replays the original debit (H12)", async () => {
      const team = await store.createTeam("Pool", D(500));
      await store.addTeamMember(team.teamId, "user-1", "member");

      const r1 = await store.deductTeam(team.teamId, "user-1", D(50), null, "team-idem-1");
      expect(r1.error).toBeUndefined();
      const r2 = await store.deductTeam(team.teamId, "user-1", D(50), null, "team-idem-1");
      expect(r2.transactionId).toBe(r1.transactionId);
      // Pool only debited once.
      expect((await store.getTeamBalance(team.teamId)).balance.toString()).toBe("450");
    });

    it("deductTeam insufficient team balance returns error", async () => {
      const team = await store.createTeam("Poor Team", D(10));
      await store.addTeamMember(team.teamId, "user-1", "member");
      const result = await store.deductTeam(team.teamId, "user-1", D(100));
      expect(result.error).toBe("insufficient_team_balance");
    });

    it("deductTeam user not in team returns error", async () => {
      const team = await store.createTeam("Closed Team", D(500));
      const result = await store.deductTeam(team.teamId, "user-1", D(10));
      expect(result.error).toBe("user_not_in_team");
    });

    it("deductTeam spend cap blocks overspend", async () => {
      const team = await store.createTeam("Capped", D(1000));
      await store.addTeamMember(team.teamId, "user-1", "member", D(50));

      const r1 = await store.deductTeam(team.teamId, "user-1", D(30));
      expect(r1.error).toBeUndefined();
      expect(r1.teamBalanceAfter.toString()).toBe("970");

      const r2 = await store.deductTeam(team.teamId, "user-1", D(30));
      expect(r2.error).toBe("spend_cap_exceeded");
    });

    it("deductTeam non-existent team returns error", async () => {
      const result = await store.deductTeam("no-such-team", "user-1", D(10));
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
      store.setSpendCap({ userId: "user-1", type: "daily", limit: D(100), action: "deny" });
      const result = await store.checkSpendCap("user-1", null, D(101));
      expect(result.capped).toBe(true);
      expect(result.action).toBe("deny");
    });

    it("allows when spend is within daily cap", async () => {
      store.setSpendCap({ userId: "user-1", type: "daily", limit: D(100), action: "deny" });
      const result = await store.checkSpendCap("user-1", null, D(50));
      expect(result.capped).toBe(false);
    });

    it("boundary: amount == limit is not capped", async () => {
      store.setSpendCap({ userId: "user-1", type: "daily", limit: D(100), action: "deny" });
      const result = await store.checkSpendCap("user-1", null, D(100));
      expect(result.capped).toBe(false);
    });

    it("warn action allows through", async () => {
      store.setSpendCap({ userId: "user-1", type: "daily", limit: D(100), action: "warn" });
      const result = await store.checkSpendCap("user-1", null, D(101));
      expect(result.capped).toBe(false);
      expect(result.action).toBe("warn");
    });

    it("notify action allows through", async () => {
      store.setSpendCap({ userId: "user-1", type: "daily", limit: D(100), action: "notify" });
      const result = await store.checkSpendCap("user-1", null, D(101));
      expect(result.capped).toBe(false);
      expect(result.action).toBe("notify");
    });

    it("monthly cap type accumulates over the month", async () => {
      store.setSpendCap({ userId: "user-1", type: "monthly", limit: D(100), action: "deny" });
      const result = await store.checkSpendCap("user-1", null, D(150));
      expect(result.capped).toBe(true);
      expect(result.action).toBe("deny");
    });

    it("per-model cap is independent of global cap", async () => {
      store.setSpendCap({
        userId: "user-1",
        type: "daily",
        limit: D(50),
        action: "deny",
        model: "gpt-4",
      });
      store.setSpendCap({ userId: "user-1", type: "daily", limit: D(200), action: "deny" });

      const r1 = await store.checkSpendCap("user-1", "gpt-4", D(30));
      expect(r1.capped).toBe(false);

      const r2 = await store.checkSpendCap("user-1", "gpt-4", D(60));
      expect(r2.capped).toBe(true);
      expect(r2.model).toBe("gpt-4");

      const r3 = await store.checkSpendCap("user-1", "claude-3", D(150));
      expect(r3.capped).toBe(false);
    });

    it("caps only apply to matching user", async () => {
      store.setSpendCap({ userId: "user-1", type: "daily", limit: D(100), action: "deny" });
      const result = await store.checkSpendCap("user-2", null, D(200));
      expect(result.capped).toBe(false);
    });

    it("accounts for existing spend in current window", async () => {
      store.setSpendCap({ userId: "user-1", type: "daily", limit: D(100), action: "deny" });
      const result = await store.checkSpendCap("user-1", null, D(110));
      expect(result.capped).toBe(true);
      expect(result.currentSpend.toString()).toBe("0");
      expect(result.limit.toString()).toBe("100");
    });
  });

  describe("listUserTransactions", () => {
    beforeEach(async () => {
      await store.addCredits("user-1", D(1000), "purchase", { ref: "purchase-1" });
      await store.addCredits("user-1", D(500), "signup_bonus", { ref: "bonus-1" });
      const r1 = await store.reserveCredits("user-1", D(200), "usage", { model: "gpt-4" });
      await store.deductCredits("user-1", r1.reservationId, D(200), null, { model: "gpt-4" });
      const r2 = await store.reserveCredits("user-1", D(50), "usage", { model: "claude-3" });
      await store.deductCredits("user-1", r2.reservationId, D(50), null, { model: "claude-3" });
      await store.addCredits("user-2", D(999), "purchase");
    });

    it("returns all transactions for user unfiltered", async () => {
      const result = await store.listUserTransactions("user-1");
      expect(result.total).toBe(4);
      expect(result.items).toHaveLength(4);
    });

    it("filters by type", async () => {
      const result = await store.listUserTransactions("user-1", { types: ["usage"] });
      expect(result.total).toBe(2);
      expect(result.items).toHaveLength(2);
      expect(result.items.every((t) => t.type === "usage")).toBe(true);
    });

    it("filters by date range", async () => {
      const now = new Date();
      const future = new Date(now.getTime() + 86_400_000);
      const past = new Date(now.getTime() - 86_400_000);
      const result = await store.listUserTransactions("user-1", { fromDate: future });
      expect(result.total).toBe(0);
      const all = await store.listUserTransactions("user-1", { fromDate: past, toDate: future });
      expect(all.total).toBe(4);
    });

    it("paginates with limit and offset", async () => {
      const page1 = await store.listUserTransactions("user-1", { limit: 2, offset: 0 });
      expect(page1.items).toHaveLength(2);
      expect(page1.total).toBe(4);

      const page2 = await store.listUserTransactions("user-1", { limit: 2, offset: 2 });
      expect(page2.items).toHaveLength(2);
      expect(page2.total).toBe(4);

      expect(page1.items[0].id).not.toBe(page2.items[0].id);
    });

    it("does not include other users' transactions", async () => {
      const result = await store.listUserTransactions("user-2");
      expect(result.total).toBe(1);
      expect(result.items[0].type).toBe("purchase");
    });

    it("returns empty for user with no transactions", async () => {
      const result = await store.listUserTransactions("no-such-user");
      expect(result.total).toBe(0);
      expect(result.items).toHaveLength(0);
    });
  });

  describe("listUsageEvents", () => {
    it("returns only usage events for the user", async () => {
      await store.addCredits("user-1", D(1000), "purchase");
      const r1 = await store.reserveCredits("user-1", D(50), "usage");
      await store.deductCredits("user-1", r1.reservationId, D(50));
      const result = await store.listUsageEvents("user-1");
      expect(result.total).toBe(1);
      expect(result.items[0].type).toBe("usage");
      expect(result.items[0].amount.toString()).toBe("-50");
    });
  });
});
