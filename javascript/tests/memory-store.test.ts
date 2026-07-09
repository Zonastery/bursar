import { describe, it, expect, beforeEach } from "vitest";
import Decimal from "decimal.js";
import { MemoryStore } from "../src/stores/memory-store.js";
import { CreditStore } from "../src/stores/credit-store.js";
import { CapabilityNotSupportedError, StoreError } from "../src/errors.js";
import type { FeatureLimitResult } from "../src/types.js";
import { resolveAllowanceWindow } from "../src/allowance.js";
import type {
  AddCreditsResult,
  AllowanceResult,
  AvailableResult,
  BalanceResult,
  CapCheckResult,
  CheckFeatureResult,
  DeductionResult,
  GetUserPlanResult,
  LeaseResult,
  PaginatedTransactions,
  RefundResult,
  ReleaseResult,
  SetUserPlanResult,
  SetupResult,
  SweepResult,
  BucketBalancesResult,
} from "../src/types.js";

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
      expect(result.tablesCreated).toHaveLength(11);
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

  describe("addCredits idempotency", () => {
    it("idempotency replays original (one grant)", async () => {
      const r1 = await store.addCredits("user-1", D(100), "purchase", null, null, null, "k1");
      expect(r1.idempotent).toBe(false);
      const r2 = await store.addCredits("user-1", D(100), "purchase", null, null, null, "k1");
      expect(r2.idempotent).toBe(true);
      expect(r2.transactionId).toBe(r1.transactionId);
      // Only granted once.
      expect((await store.getBalance("user-1")).balance.toString()).toBe("100");
    });

    it("different idempotencyKey grants again", async () => {
      await store.addCredits("user-1", D(100), "purchase", null, null, null, "k1");
      await store.addCredits("user-1", D(100), "purchase", null, null, null, "k2");
      expect((await store.getBalance("user-1")).balance.toString()).toBe("200");
    });

    it("idempotencyKey is user-scoped — a different user's grant with the same key is not a replay", async () => {
      await store.addCredits("user-1", D(100), "purchase", null, null, null, "shared-key");
      const r2 = await store.addCredits(
        "user-2",
        D(50),
        "purchase",
        null,
        null,
        null,
        "shared-key",
      );
      expect(r2.idempotent).toBe(false);
      expect((await store.getBalance("user-2")).balance.toString()).toBe("50");
    });

    it("omitted idempotencyKey never replays (each call grants)", async () => {
      await store.addCredits("user-1", D(100), "purchase");
      await store.addCredits("user-1", D(100), "purchase");
      expect((await store.getBalance("user-1")).balance.toString()).toBe("200");
    });
  });

  describe("deductWithAllowance (atomic charge)", () => {
    async function seedPlan(allowanceAmount: number, userId = "user-1") {
      const config = {
        version: 1,
        metering: { models: { "*": "1" } },
        plans: {
          "plan-1": {
            label: "Plan",
            allowance: { amount: D(allowanceAmount), period: "calendar_month" },
          },
        },
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
      store.setSpendCap({ userId: "user-1", type: "daily", limit: D(10), onExceed: "deny" });
      // net = 20 - 5 = 15 > cap 10 → deny
      const r = await store.deductWithAllowance("user-1", D(20));
      expect(r.error).toBe("cap_reached");
      const allowance = await store.checkAllowance("user-1");
      expect(allowance.allowanceRemaining.toString()).toBe("5");
      expect((await store.getBalance("user-1")).balance.toString()).toBe("1000");
    });

    it("warn cap sets capWarning but proceeds", async () => {
      await store.addCredits("user-1", D(1000));
      store.setSpendCap({ userId: "user-1", type: "daily", limit: D(10), onExceed: "warn" });
      const r = await store.deductWithAllowance("user-1", D(20));
      expect(r.error).toBeUndefined();
      expect(r.capWarning).toBe("warn");
      expect(r.amount.toString()).toBe("20");
    });

    it("cap accumulates across prior window spend", async () => {
      await store.addCredits("user-1", D(1000));
      store.setSpendCap({ userId: "user-1", type: "daily", limit: D(30), onExceed: "deny" });
      const a = await store.deductWithAllowance("user-1", D(20));
      expect(a.error).toBeUndefined();
      // Prior 20 + this 20 = 40 > 30 → deny
      const b = await store.deductWithAllowance("user-1", D(20));
      expect(b.error).toBe("cap_reached");
    });

    it("cap boundary: amount == limit is allowed", async () => {
      await store.addCredits("user-1", D(1000));
      store.setSpendCap({ userId: "user-1", type: "daily", limit: D(10), onExceed: "deny" });
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
      const config = {
        version: 1,
        metering: { models: { "gpt-4": "input_tokens * 0.01" } },
        ledger: { minBalance: 0 },
      };
      await store.setActivePricing(config);
      const result = await store.getActivePricing();
      expect(result).not.toBeNull();
      expect(result!.config.metering.models["gpt-4"]).toBe("input_tokens * 0.01");
    });

    it("increments version on each set", async () => {
      const config = { version: 1, metering: { models: { a: "1" } }, ledger: { minBalance: 0 } };
      const id1 = await store.setActivePricing(config);
      const id2 = await store.setActivePricing(config);
      expect(id1).not.toBe(id2);
    });
  });

  describe("plan management", () => {
    it("getUserPlan returns null plan for user with no plan", async () => {
      const result = await store.getUserPlan("user-1");
      expect(result.planId).toBeNull();
      expect(result.planLabel).toBeNull();
      expect(result.allowanceAmount.toString()).toBe("0");
    });

    it("setUserPlan and getUserPlan round-trips", async () => {
      const config = {
        version: 1,
        metering: { models: { "*": "1" } },
        plans: {
          "plan-free": {
            label: "Free Plan",
            allowance: { amount: D(100), period: "calendar_month" },
          },
        },
      };
      await store.setActivePricing(config);

      await store.setUserPlan("user-1", "plan-free");
      const result = await store.getUserPlan("user-1");
      expect(result.planId).toBe("plan-free");
      expect(result.planLabel).toBe("Free Plan");
      expect(result.allowanceAmount.toString()).toBe("100");
      expect(result.entitlements).toEqual({});
    });

    it("getUserPlan returns features from plan definition", async () => {
      const config = {
        version: 1,
        metering: { models: { "*": "1" } },
        plans: {
          premium: {
            label: "Premium",
            allowance: { amount: D(2000), period: "calendar_month" },
            entitlements: { aiChat: true, maxRoadmaps: 20 },
          },
        },
      };
      await store.setActivePricing(config);
      await store.setUserPlan("user-1", "premium");

      const result = await store.getUserPlan("user-1");
      expect(result.planId).toBe("premium");
      expect(result.entitlements["aiChat"].value).toBe(true);
      expect(result.entitlements["maxRoadmaps"].value).toBe(20);
    });

    it("checkFeature distinguishes presence from truthiness (M6)", async () => {
      const config = {
        version: 1,
        metering: { models: { "*": "1" } },
        plans: {
          free: {
            label: "Free",
            allowance: { amount: D(0), period: "calendar_month" },
            entitlements: {},
          },
          premium: {
            label: "Premium",
            allowance: { amount: D(2000), period: "calendar_month" },
            entitlements: { aiChat: true, maxRoadmaps: 20, quota: 0, label: "", disabled: false },
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
      const config = {
        version: 1,
        metering: { models: { "*": "1" } },
        plans: {
          "plan-pro": {
            label: "Pro Plan",
            allowance: { amount: D(500), period: "calendar_month" },
          },
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
      const config = {
        version: 1,
        metering: { models: { "*": "1" } },
        plans: {
          "plan-basic": { label: "Basic", allowance: { amount: D(200), period: "calendar_month" } },
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

    it("unsetUserPlan clears plan_id and plan_assigned_at", async () => {
      await store.setUserPlan("user-1", "plan-basic");
      let plan = await store.getUserPlan("user-1");
      expect(plan.planId).toBe("plan-basic");
      expect(plan.planAssignedAt).not.toBeNull();

      await store.unsetUserPlan("user-1");
      plan = await store.getUserPlan("user-1");
      expect(plan.planId).toBeNull();
      expect(plan.planAssignedAt).toBeNull();
    });

    it("unsetUserPlan is idempotent for user with no plan", async () => {
      const result = await store.unsetUserPlan("no-plan-user");
      expect(result.userId).toBe("no-plan-user");
      const plan = await store.getUserPlan("no-plan-user");
      expect(plan.planId).toBeNull();
    });
  });

  describe("refunds", () => {
    async function makeUsageTx(userId: string, amount: number) {
      await store.addCredits(userId, D(1000), "purchase");
      return store.deductWithAllowance(userId, D(amount), { minBalance: D(0) });
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
      await store.addCredits(
        "user-1",
        D(100),
        "purchase",
        null,
        new Date("2026-01-01T00:00:00.500Z"),
      );
      store.setClock(() => LATER);

      const result = await store.sweepExpiredCredits();
      expect(result.expiredCount).toBe(1);
      expect(result.expiredAmount.toString()).toBe("100");
      expect(result.dryRun).toBe(false);
      expect((await store.getBalance("user-1")).balance.toString()).toBe("0");
    });

    it("dryRun reports without modifying balance", async () => {
      await store.addCredits(
        "user-1",
        D(100),
        "purchase",
        null,
        new Date("2026-01-01T00:00:00.500Z"),
      );
      store.setClock(() => LATER);

      const result = await store.sweepExpiredCredits(true);
      expect(result.expiredCount).toBe(1);
      expect(result.expiredAmount.toString()).toBe("100");
      expect(result.dryRun).toBe(true);
      expect((await store.getBalance("user-1")).balance.toString()).toBe("100");
    });

    it("double-sweep reports zero and does not double-debit (H4)", async () => {
      await store.addCredits(
        "user-1",
        D(100),
        "purchase",
        null,
        new Date("2026-01-01T00:00:00.500Z"),
      );
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
      await store.addCredits(
        "user-1",
        D(50),
        "purchase",
        null,
        new Date("2026-01-01T00:00:00.500Z"),
      );
      await store.addCredits("user-1", D(30), "purchase"); // no expiry
      store.setClock(() => LATER);

      const result = await store.sweepExpiredCredits();
      expect(result.expiredAmount.toString()).toBe("50");
      expect((await store.getBalance("user-1")).balance.toString()).toBe("30");
    });

    // ── Per-user scoping (`userId` param) ────────────────────────────────
    it("userId scopes the sweep to a single user, leaving other users untouched", async () => {
      await store.addCredits(
        "user-1",
        D(100),
        "purchase",
        null,
        new Date("2026-01-01T00:00:00.500Z"),
      );
      await store.addCredits(
        "user-2",
        D(200),
        "purchase",
        null,
        new Date("2026-01-01T00:00:00.500Z"),
      );
      store.setClock(() => LATER);

      const result = await store.sweepExpiredCredits(false, "user-1");
      expect(result.expiredCount).toBe(1);
      expect(result.expiredAmount.toString()).toBe("100");
      expect((await store.getBalance("user-1")).balance.toString()).toBe("0");
      // user-2's expired grant is untouched by the scoped sweep.
      expect((await store.getBalance("user-2")).balance.toString()).toBe("200");
    });

    it("omitted userId preserves the original global-sweep behaviour exactly", async () => {
      await store.addCredits(
        "user-1",
        D(100),
        "purchase",
        null,
        new Date("2026-01-01T00:00:00.500Z"),
      );
      await store.addCredits(
        "user-2",
        D(200),
        "purchase",
        null,
        new Date("2026-01-01T00:00:00.500Z"),
      );
      store.setClock(() => LATER);

      const result = await store.sweepExpiredCredits();
      expect(result.expiredCount).toBe(2);
      expect(result.expiredAmount.toString()).toBe("300");
      expect((await store.getBalance("user-1")).balance.toString()).toBe("0");
      expect((await store.getBalance("user-2")).balance.toString()).toBe("0");
    });

    it("scoped dryRun reports without modifying the target user's balance", async () => {
      await store.addCredits(
        "user-1",
        D(100),
        "purchase",
        null,
        new Date("2026-01-01T00:00:00.500Z"),
      );
      store.setClock(() => LATER);

      const result = await store.sweepExpiredCredits(true, "user-1");
      expect(result.expiredCount).toBe(1);
      expect(result.dryRun).toBe(true);
      expect((await store.getBalance("user-1")).balance.toString()).toBe("100");
    });

    it("scoped sweep for a user with nothing expired reports zero", async () => {
      await store.addCredits("user-1", D(100)); // no expiry
      store.setClock(() => LATER);

      const result = await store.sweepExpiredCredits(false, "user-1");
      expect(result.expiredCount).toBe(0);
      expect(result.expiredAmount.toString()).toBe("0");
    });
  });

  // ── Credit tiers: MemoryStore-level edge cases (see also tests/tiers.test.ts
  // and tests/tiers-adversarial.test.ts for the main feature coverage) ──────
  describe("credit tiers — MemoryStore edge cases", () => {
    it("tier walk breaks a priority tie by tier key ascending", async () => {
      await store.setActivePricing({
        version: 1,
        metering: { models: { "*": "input_tokens * 1" } },
        ledger: {
          buckets: {
            z: { label: "Z", priority: 10, expires: false },
            a: { label: "A", priority: 10, expires: false },
          },
        },
      });
      await store.addCredits("user-1", D(10), "adjustment", null, null, "z");
      await store.addCredits("user-1", D(10), "adjustment", null, null, "a");

      const r = await store.deductWithAllowance("user-1", D(15));
      expect(r.error).toBeUndefined();
      // Same priority (10) → tie-break by tier key ascending: "a" drains
      // fully before "z" is touched.
      expect(r.bucketBreakdown?.a?.toString()).toBe("10");
      expect(r.bucketBreakdown?.z?.toString()).toBe("5");
    });

    it("refund of a legacy debit lacking a stored bucketBreakdown falls back to the 'default' bucket", async () => {
      // deductWithAllowance/settleLease always stamp `metadata.bucketBreakdown`
      // today, so this state (a ledger row with none) can only arise from
      // data that predates the credit-tiers migration. Simulate it via
      // direct (white-box) ledger injection, mirroring the fallback documented
      // in memory-store.ts's refundCredits: `?? { default: originalDebit }`.
      await store.addCredits("user-1", D(100), "purchase");
      const transactions = (
        store as unknown as {
          transactions: Array<{
            id: string;
            userId: string;
            amount: Decimal;
            type: string;
            metadata?: Record<string, unknown>;
            createdAt: Date;
          }>;
        }
      ).transactions;
      const legacyTxId = "legacy-tx-1";
      transactions.push({
        id: legacyTxId,
        userId: "user-1",
        amount: D(-30),
        type: "usage",
        metadata: {},
        createdAt: new Date(),
      });
      const balances = (store as unknown as { balances: Map<string, Decimal> }).balances;
      balances.set("user-1", balances.get("user-1")!.minus(D(30)));

      const refund = await store.refundCredits(legacyTxId);
      expect(refund.error).toBeUndefined();
      expect(refund.bucketBreakdown?.default?.toString()).toBe("30");
      expect(Object.keys(refund.bucketBreakdown ?? {})).toEqual(["default"]);
    });
  });

  describe("usage analytics (Decimal)", () => {
    async function deduct(userId: string, amount: number, model?: string) {
      await store.addCredits(userId, D(amount + 100), "purchase");
      return store.deductWithAllowance(userId, D(amount), {
        minBalance: D(0),
        model: model ?? null,
      });
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
      store.setSpendCap({ userId: "user-1", type: "daily", limit: D(100), onExceed: "deny" });
      const result = await store.checkSpendCap("user-1", null, D(101));
      expect(result.capped).toBe(true);
      expect(result.action).toBe("deny");
    });

    it("allows when spend is within daily cap", async () => {
      store.setSpendCap({ userId: "user-1", type: "daily", limit: D(100), onExceed: "deny" });
      const result = await store.checkSpendCap("user-1", null, D(50));
      expect(result.capped).toBe(false);
    });

    it("boundary: amount == limit is not capped", async () => {
      store.setSpendCap({ userId: "user-1", type: "daily", limit: D(100), onExceed: "deny" });
      const result = await store.checkSpendCap("user-1", null, D(100));
      expect(result.capped).toBe(false);
    });

    it("warn action allows through", async () => {
      store.setSpendCap({ userId: "user-1", type: "daily", limit: D(100), onExceed: "warn" });
      const result = await store.checkSpendCap("user-1", null, D(101));
      expect(result.capped).toBe(false);
      expect(result.action).toBe("warn");
    });

    it("notify action allows through", async () => {
      store.setSpendCap({ userId: "user-1", type: "daily", limit: D(100), onExceed: "notify" });
      const result = await store.checkSpendCap("user-1", null, D(101));
      expect(result.capped).toBe(false);
      expect(result.action).toBe("notify");
    });

    it("monthly cap type accumulates over the month", async () => {
      store.setSpendCap({ userId: "user-1", type: "monthly", limit: D(100), onExceed: "deny" });
      const result = await store.checkSpendCap("user-1", null, D(150));
      expect(result.capped).toBe(true);
      expect(result.action).toBe("deny");
    });

    it("per-model cap is independent of global cap", async () => {
      store.setSpendCap({
        userId: "user-1",
        type: "daily",
        limit: D(50),
        onExceed: "deny",
        model: "gpt-4",
      });
      store.setSpendCap({ userId: "user-1", type: "daily", limit: D(200), onExceed: "deny" });

      const r1 = await store.checkSpendCap("user-1", "gpt-4", D(30));
      expect(r1.capped).toBe(false);

      const r2 = await store.checkSpendCap("user-1", "gpt-4", D(60));
      expect(r2.capped).toBe(true);
      expect(r2.model).toBe("gpt-4");

      const r3 = await store.checkSpendCap("user-1", "claude-3", D(150));
      expect(r3.capped).toBe(false);
    });

    it("caps only apply to matching user", async () => {
      store.setSpendCap({ userId: "user-1", type: "daily", limit: D(100), onExceed: "deny" });
      const result = await store.checkSpendCap("user-2", null, D(200));
      expect(result.capped).toBe(false);
    });

    it("accounts for existing spend in current window", async () => {
      store.setSpendCap({ userId: "user-1", type: "daily", limit: D(100), onExceed: "deny" });
      const result = await store.checkSpendCap("user-1", null, D(110));
      expect(result.capped).toBe(true);
      expect(result.currentSpend.toString()).toBe("0");
      expect(result.limit.toString()).toBe("100");
    });
  });

  describe("listUserTransactions", () => {
    beforeEach(async () => {
      await store.addCredits("user-1", D(1500), "purchase", { ref: "purchase-1" });
      await store.addCredits("user-1", D(500), "signup_bonus", { ref: "bonus-1" });
      await store.deductWithAllowance("user-1", D(200), { minBalance: D(0), model: "gpt-4" });
      await store.deductWithAllowance("user-1", D(50), { minBalance: D(0), model: "claude-3" });
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
      await store.deductWithAllowance("user-1", D(50), { minBalance: D(0) });
      const result = await store.listUsageEvents("user-1");
      expect(result.total).toBe(1);
      expect(result.items[0].type).toBe("usage");
      expect(result.items[0].amount.toString()).toBe("-50");
    });
  });

  // ── MS2: Cap deny does NOT consume allowance ──────────────────────────
  describe("MS2 — deny cap does not consume plan allowance", () => {
    it("cap_reached error leaves allowanceRemaining unchanged", async () => {
      // Plan covers first 5 credits free; charge 10 → net = 5 which is > cap limit 3
      const config = {
        version: 1,
        metering: { models: { "*": "1" } },
        plans: {
          "plan-ms2": { label: "Plan MS2", allowance: { amount: D(5), period: "calendar_month" } },
        },
      };
      await store.setActivePricing(config);
      await store.setUserPlan("user-1", "plan-ms2");
      await store.addCredits("user-1", D(1000), "adjustment");

      // Deny cap: limit=3 on the net amount (10-5=5 > 3 → deny)
      store.setSpendCap({ userId: "user-1", type: "daily", limit: D(3), onExceed: "deny" });

      const r = await store.deductWithAllowance("user-1", D(10));
      expect(r.error).toBe("cap_reached");

      // Allowance must NOT have been consumed
      const allowance = await store.checkAllowance("user-1");
      expect(allowance.allowanceRemaining.toString()).toBe("5");
    });
  });

  // ── MS3: Refund does NOT restore allowance ────────────────────────────
  describe("MS3 — refund does not restore plan allowance", () => {
    it("allowanceRemaining stays reduced after refund", async () => {
      // Plan has 30 free allowance. Charge 50 → allowance covers 30, net = 20 debited from balance.
      // The transaction has amount = -20 (the net debit), so it is refundable.
      const config = {
        version: 1,
        metering: { models: { "*": "1" } },
        plans: {
          "plan-ms3": { label: "Plan MS3", allowance: { amount: D(30), period: "calendar_month" } },
        },
      };
      await store.setActivePricing(config);
      await store.setUserPlan("user-1", "plan-ms3");
      await store.addCredits("user-1", D(500), "adjustment");

      const initialAllowance = (await store.checkAllowance("user-1")).allowanceRemaining;
      expect(initialAllowance.toString()).toBe("30");

      // Charge 50: allowance=30 consumed, net=20 debited from balance
      const deduct = await store.deductWithAllowance("user-1", D(50));
      expect(deduct.error).toBeUndefined();
      const allowanceConsumed = deduct.allowanceConsumed;
      expect(allowanceConsumed.toString()).toBe("30");

      // Refund the net-debit portion (20 credits)
      const refund = await store.refundCredits(deduct.transactionId);
      expect(refund.error).toBeUndefined();

      // Allowance should still show 0 remaining (30 consumed, not restored)
      const afterRefund = await store.checkAllowance("user-1");
      const expected = initialAllowance.minus(allowanceConsumed);
      expect(afterRefund.allowanceRemaining.toString()).toBe(expected.toString()); // "0"
    });
  });

  // ── MS5: Sweep when balance < total expired ───────────────────────────
  describe("MS5 — sweep clamps to current balance (never goes negative)", () => {
    it("sweep result is clamped and balance stays non-negative", async () => {
      const T0 = new Date("2026-06-01T00:00:00.000Z");
      const AFTER = new Date("2026-06-01T00:00:01.000Z"); // past the 1ms expiry

      store.setClock(() => T0);

      // 100 credits expiring in 1ms from T0
      const expiry = new Date(T0.getTime() + 1);
      await store.addCredits("user-1", D(100), "purchase", null, expiry);
      // 50 credits with no expiry
      await store.addCredits("user-1", D(50), "purchase");
      // Deduct 80: balance goes from 150 to 70
      await store.deductWithAllowance("user-1", D(80), { minBalance: D(0) });
      expect((await store.getBalance("user-1")).balance.toString()).toBe("70");

      // Advance past expiry and sweep
      store.setClock(() => AFTER);
      const sweep = await store.sweepExpiredCredits();

      const balance = (await store.getBalance("user-1")).balance;
      expect(balance.gte(0)).toBe(true);
      // min(100, 70) = 70 swept; balance = 70 - 70 = 0
      expect(balance.toString()).toBe("0");
      expect(sweep.expiredAmount.lte(D(100))).toBe(true);
    });
  });

  // ── MS6: Team member per-user spend cap (independent caps) ────────────
  describe("MS6 — team per-user spend caps are independent", () => {
    it("each member's cap is enforced independently", async () => {
      const team = await store.createTeam("TestTeam", D(1000));
      await store.addTeamMember(team.teamId, "u1", "member", D(200));
      await store.addTeamMember(team.teamId, "u2", "member", D(150));

      // u1: first charge 150 → OK
      const r1 = await store.deductTeam(team.teamId, "u1", D(150));
      expect(r1.error).toBeUndefined();

      // u1: second charge 80 → denied (150+80=230 > 200)
      const r2 = await store.deductTeam(team.teamId, "u1", D(80));
      expect(r2.error).toBe("spend_cap_exceeded");

      // u2: charge 149 → OK (under 150)
      const r3 = await store.deductTeam(team.teamId, "u2", D(149));
      expect(r3.error).toBeUndefined();

      // u2: charge 2 → denied (149+2=151 > 150)
      const r4 = await store.deductTeam(team.teamId, "u2", D(2));
      expect(r4.error).toBe("spend_cap_exceeded");

      // Team balance reflects only two successful charges: 1000 - 150 - 149 = 701
      const teamBalance = await store.getTeamBalance(team.teamId);
      expect(teamBalance.balance.toString()).toBe("701");
    });
  });

  // ── MS7: listUserTransactions type filter ─────────────────────────────
  describe("MS7 — listUserTransactions type filter", () => {
    it("filters by usage type and by purchase type independently", async () => {
      await store.addCredits("user-1", D(500), "purchase");
      await store.deductWithAllowance("user-1", D(10), { minBalance: D(0) });

      const usageOnly = await store.listUserTransactions("user-1", { types: ["usage"] });
      expect(usageOnly.items.every((t) => t.type === "usage")).toBe(true);
      expect(usageOnly.total).toBe(1);

      const purchaseOnly = await store.listUserTransactions("user-1", { types: ["purchase"] });
      expect(purchaseOnly.items.every((t) => t.type === "purchase")).toBe(true);
      expect(purchaseOnly.total).toBe(1);
    });
  });

  // ── MS8: listUserTransactions pagination boundary ─────────────────────
  describe("MS8 — listUserTransactions pagination boundary", () => {
    it("handles limit/offset at, near, and beyond the total count", async () => {
      // Create 5 deductions
      await store.addCredits("user-1", D(1000), "purchase");
      for (let i = 0; i < 5; i++) {
        await store.deductWithAllowance("user-1", D(10), { minBalance: D(0) });
      }
      // Seed some purchases too — filter to only usage for a clean count
      const allUsage = await store.listUserTransactions("user-1", { types: ["usage"] });
      expect(allUsage.total).toBe(5);

      const page1 = await store.listUserTransactions("user-1", {
        types: ["usage"],
        limit: 2,
        offset: 0,
      });
      expect(page1.items).toHaveLength(2);
      expect(page1.total).toBe(5);

      const page3 = await store.listUserTransactions("user-1", {
        types: ["usage"],
        limit: 2,
        offset: 4,
      });
      expect(page3.items).toHaveLength(1);
      expect(page3.total).toBe(5);

      const beyond = await store.listUserTransactions("user-1", {
        types: ["usage"],
        limit: 2,
        offset: 10,
      });
      expect(beyond.items).toHaveLength(0);
      expect(beyond.total).toBe(5);
    });
  });

  // ── C3: Team member per-user spend cap enforcement ───────────────────
  describe("C3 — team member per-user spend cap is enforced", () => {
    it("team member per-user spend cap is enforced", async () => {
      const team = await store.createTeam("C3 Team", D(1000));
      await store.addTeamMember(team.teamId, "capped-user", "member", D(5));
      await store.addTeamMember(team.teamId, "uncapped-user", "member");

      // First deduction of 3 succeeds (cumulative 3 <= 5)
      const r1 = await store.deductTeam(team.teamId, "capped-user", D(3));
      expect(r1.error).toBeUndefined();
      expect(r1.teamBalanceAfter.toString()).toBe("997");

      // Second deduction of 3 fails: cumulative 3+3=6 > 5
      const r2 = await store.deductTeam(team.teamId, "capped-user", D(3));
      expect(r2.error).toBe("spend_cap_exceeded");
      // Team balance unchanged from failed deduction
      expect((await store.getTeamBalance(team.teamId)).balance.toString()).toBe("997");

      // Deduction for a different member with no cap succeeds
      const r3 = await store.deductTeam(team.teamId, "uncapped-user", D(3));
      expect(r3.error).toBeUndefined();
      expect((await store.getTeamBalance(team.teamId)).balance.toString()).toBe("994");
    });
  });

  // ── H8: incrementUsageWindow reduces available allowance ─────────────
  describe("H8 — incrementUsageWindow reduces available allowance", () => {
    it("incrementUsageWindow reduces available allowance", async () => {
      const config = {
        version: 1,
        metering: { models: { "*": "1" } },
        plans: {
          "plan-h8": { label: "Plan H8", allowance: { amount: D(10), period: "calendar_month" } },
        },
      };
      await store.setActivePricing(config);
      await store.setUserPlan("user-1", "plan-h8");
      await store.addCredits("user-1", D(100), "adjustment");

      // Initial allowance = 10
      const before = await store.checkAllowance("user-1");
      expect(before.allowanceRemaining.toString()).toBe("10");

      // Consume 4 from the window
      await store.incrementUsageWindow("user-1", "plan-h8", D(4));

      // Remaining = 6
      const after = await store.checkAllowance("user-1");
      expect(after.allowanceRemaining.toString()).toBe("6");

      // deductWithAllowance 8: only 6 from allowance, 2 from balance
      const r = await store.deductWithAllowance("user-1", D(8));
      expect(r.error).toBeUndefined();
      expect(r.allowanceConsumed.toString()).toBe("6");
      expect(r.amount.toString()).toBe("2");
      expect(r.balanceAfter.toString()).toBe("98");
    });
  });

  // ── H9: Team member role storage ─────────────────────────────────────
  describe("H9 — team member role is stored and retrievable", () => {
    it("team member role is stored and retrievable", async () => {
      const team = await store.createTeam("Role Team", D(500));
      await store.addTeamMember(team.teamId, "admin-user", "admin");

      // Verify single member has correct role
      const members1 = await store.getTeamMembers(team.teamId);
      expect(members1).toHaveLength(1);
      expect(members1[0].userId).toBe("admin-user");
      expect(members1[0].role).toBe("admin");

      // Add viewer
      await store.addTeamMember(team.teamId, "viewer-user", "viewer");
      const members2 = await store.getTeamMembers(team.teamId);
      expect(members2).toHaveLength(2);

      const admin = members2.find((m) => m.userId === "admin-user");
      const viewer = members2.find((m) => m.userId === "viewer-user");
      expect(admin!.role).toBe("admin");
      expect(viewer!.role).toBe("viewer");
    });
  });

  // ── H11: Metadata preserved in transactions ───────────────────────────
  describe("H11 — metadata is stored and returned in transactions", () => {
    it("metadata is stored and returned in transactions", async () => {
      // addCredits with metadata
      await store.addCredits("user-1", D(100), "adjustment", {
        source: "promo",
        campaign_id: "camp-1",
      });

      const txList = await store.listUserTransactions("user-1");
      const addTx = txList.items.find((t) => t.type === "adjustment");
      expect(addTx).toBeDefined();
      expect(addTx!.metadata).toBeDefined();
      expect(addTx!.metadata!["source"]).toBe("promo");
      expect(addTx!.metadata!["campaign_id"]).toBe("camp-1");

      // deductWithAllowance with metadata
      const r = await store.deductWithAllowance("user-1", D(5), {
        metadata: { model: "gpt-4", custom: "value" },
      });
      expect(r.error).toBeUndefined();

      const txList2 = await store.listUserTransactions("user-1");
      const deductTx = txList2.items.find((t) => t.id === r.transactionId);
      expect(deductTx).toBeDefined();
      expect(deductTx!.metadata!["custom"]).toBe("value");
    });
  });

  // ── M1: Allowance resets across billing periods ───────────────────────
  describe("M1 — allowance resets across billing periods", () => {
    it("allowance resets when billing period advances to the next month", async () => {
      const PERIOD_1 = new Date("2026-01-15T12:00:00.000Z");
      const PERIOD_2 = new Date("2026-02-15T12:00:00.000Z");

      store.setClock(() => PERIOD_1);

      const config = {
        version: 1,
        metering: { models: { "*": "1" } },
        plans: {
          "plan-m1": { label: "Plan M1", allowance: { amount: D(5), period: "calendar_month" } },
        },
      };
      await store.setActivePricing(config);
      await store.setUserPlan("user-1", "plan-m1");
      await store.addCredits("user-1", D(100), "adjustment");

      // Period 1: deduct 4 → allowance consumed = 4
      const r1 = await store.deductWithAllowance("user-1", D(4));
      expect(r1.allowanceConsumed.toString()).toBe("4");
      const a1 = await store.checkAllowance("user-1");
      expect(a1.allowanceRemaining.toString()).toBe("1");

      // Advance to period 2
      store.setClock(() => PERIOD_2);

      // Period 2: fresh 5-credit allowance
      const a2 = await store.checkAllowance("user-1");
      expect(a2.allowanceRemaining.toString()).toBe("5");

      // deduct 4 in period 2 → consumes 4 from the fresh allowance
      const r2 = await store.deductWithAllowance("user-1", D(4));
      expect(r2.allowanceConsumed.toString()).toBe("4");
      const a3 = await store.checkAllowance("user-1");
      expect(a3.allowanceRemaining.toString()).toBe("1");
    });
  });

  // ── M2: Spend cap accumulates across deductions ───────────────────────
  describe("M2 — spend cap accumulates and blocks correctly", () => {
    it("spend cap accumulates and blocks correctly", async () => {
      await store.addCredits("user-1", D(1000), "purchase");
      store.setSpendCap({ userId: "user-1", type: "daily", limit: D(10), onExceed: "deny" });

      // First deduction of 4 → allowed
      const r1 = await store.deductWithAllowance("user-1", D(4));
      expect(r1.error).toBeUndefined();

      // Second deduction of 4 → allowed (cumulative 8 <= 10)
      const r2 = await store.deductWithAllowance("user-1", D(4));
      expect(r2.error).toBeUndefined();

      // Third deduction of 4 → cap_reached (cumulative 8+4=12 > 10)
      const r3 = await store.deductWithAllowance("user-1", D(4));
      expect(r3.error).toBe("cap_reached");

      // Only two deductions went through → balance = 1000 - 4 - 4 = 992
      const bal = await store.getBalance("user-1");
      expect(bal.balance.toString()).toBe("992");
    });
  });

  // ── M3: Partial expiry ─────────────────────────────────────────────────
  describe("M3 — only expired credits are swept, permanent credits remain", () => {
    it("only expired credits are swept, permanent credits remain", async () => {
      const T0 = new Date("2026-03-01T00:00:00.000Z");
      const YESTERDAY = new Date("2026-02-28T00:00:00.000Z"); // before T0
      const LATER = new Date("2026-03-01T00:00:01.000Z");

      store.setClock(() => T0);

      // 10 credits that already expired (expiry is before T0)
      await store.addCredits("user-1", D(10), "purchase", null, YESTERDAY);
      // 5 permanent credits
      await store.addCredits("user-1", D(5), "purchase");

      // Sweep → 10 expired
      const sweep1 = await store.sweepExpiredCredits();
      expect(sweep1.expiredAmount.toString()).toBe("10");

      // Balance = 5 (permanent credits remain)
      const bal = await store.getBalance("user-1");
      expect(bal.balance.toString()).toBe("5");

      // Advance clock and sweep again → idempotent, 0 more expired
      store.setClock(() => LATER);
      const sweep2 = await store.sweepExpiredCredits();
      expect(sweep2.expiredAmount.toString()).toBe("0");
    });
  });

  // ── M7: checkSpendCap direct test ─────────────────────────────────────
  describe("M7 — checkSpendCap direct test", () => {
    it("no cap returns action null; deny cap exceeded returns deny; warn cap exceeded returns warn", async () => {
      const noCapUser = "user-nocap-m7";
      const result = await store.checkSpendCap(noCapUser);
      expect(result.action).toBeNull();
      expect(result.capped).toBe(false);

      // Set up a deny cap at 10, with current spend of 8
      const denyUser = "user-deny-m7";
      await store.addCredits(denyUser, D(500), "purchase");
      store.setSpendCap({ userId: denyUser, type: "daily", limit: D(10), onExceed: "deny" });
      // Spend 8 first via deductWithAllowance
      await store.deductWithAllowance(denyUser, D(8));
      // Check if adding 3 more would exceed the cap (8 + 3 = 11 > 10)
      const denyResult = await store.checkSpendCap(denyUser, null, D(3));
      expect(denyResult.action).toBe("deny");
      expect(denyResult.capped).toBe(true);

      // Set up a warn cap at 10, with current spend of 8
      const warnUser = "user-warn-m7";
      await store.addCredits(warnUser, D(500), "purchase");
      store.setSpendCap({ userId: warnUser, type: "daily", limit: D(10), onExceed: "warn" });
      // Spend 8 first via deductWithAllowance
      await store.deductWithAllowance(warnUser, D(8));
      // Check if adding 3 more would exceed the cap (8 + 3 = 11 > 10)
      const warnResult = await store.checkSpendCap(warnUser, null, D(3));
      expect(warnResult.action).toBe("warn");
      expect(warnResult.capped).toBe(false);
    });
  });

  // ── M8: Refund of allowance-covered deduction ─────────────────────────
  describe("M8 — refund of allowance-covered deduction", () => {
    it("refund of a deduction fully covered by allowance returns over_refund (net charge was zero)", async () => {
      const config = {
        version: 1,
        metering: { models: { "*": "1" } },
        plans: {
          "plan-m8": { label: "Plan M8", allowance: { amount: D(100), period: "calendar_month" } },
        },
      };
      await store.setActivePricing(config);
      await store.setUserPlan("user-1", "plan-m8");
      await store.addCredits("user-1", D(50), "adjustment");

      // Deduct 5 fully covered by allowance (net charge = 0, balance unchanged)
      const r = await store.deductWithAllowance("user-1", D(5));
      expect(r.error).toBeUndefined();
      expect(r.allowanceConsumed.toString()).toBe("5");
      expect(r.amount.toString()).toBe("0");

      // The recorded transaction has amount=0 (negated of net=0), so it is non-negative
      // → refundCredits treats it as over_refund (nothing was charged from balance)
      const refund = await store.refundCredits(r.transactionId);
      expect(refund.error).toBe("over_refund");
    });
  });

  // ── Pagination: listUserTransactions (12 transactions) ────────────────
  describe("listUserTransactions pagination (12 transactions)", () => {
    it("correctly pages through 12 transactions", async () => {
      await store.addCredits("user-tx", D(10000), "purchase");
      for (let i = 0; i < 12; i++) {
        await store.deductWithAllowance("user-tx", D(1));
      }

      // Page 1: limit=5, offset=0 → 5 results
      const page1 = await store.listUserTransactions("user-tx", {
        types: ["usage"],
        limit: 5,
        offset: 0,
      });
      expect(page1.items).toHaveLength(5);
      expect(page1.total).toBe(12);

      // Page 2: limit=5, offset=5 → 5 different results
      const page2 = await store.listUserTransactions("user-tx", {
        types: ["usage"],
        limit: 5,
        offset: 5,
      });
      expect(page2.items).toHaveLength(5);
      const page1Ids = new Set(page1.items.map((t) => t.id));
      for (const item of page2.items) {
        expect(page1Ids.has(item.id)).toBe(false);
      }

      // Page 3: limit=5, offset=10 → 2 results (last page, smaller than limit)
      const page3 = await store.listUserTransactions("user-tx", {
        types: ["usage"],
        limit: 5,
        offset: 10,
      });
      expect(page3.items).toHaveLength(2);

      // Beyond: limit=5, offset=12 → 0 results, no error
      const page4 = await store.listUserTransactions("user-tx", {
        types: ["usage"],
        limit: 5,
        offset: 12,
      });
      expect(page4.items).toHaveLength(0);
      expect(page4.total).toBe(12);
    });
  });

  // ── MS9: checkFeature: float(0) and Decimal("0") are present ─────────
  describe("MS9 — checkFeature treats numeric 0 and Decimal(0) as present, false as absent", () => {
    it("numeric 0 is present, Decimal(0) is present, false is absent", async () => {
      const config = {
        version: 1,
        metering: { models: { "*": "1" } },
        plans: {
          "plan-ms9": {
            label: "Plan MS9",
            allowance: { amount: D(0), period: "calendar_month" },
            entitlements: {
              quota: 0,
              rate: new Decimal("0"),
              active: false,
            },
          },
        },
      };
      await store.setActivePricing(config);
      await store.setUserPlan("user-1", "plan-ms9");

      // numeric 0 → present
      const quota = await store.checkFeature("user-1", "quota");
      expect(quota.hasFeature).toBe(true);
      expect(quota.value).toBe(0);

      // Decimal("0") → present (it is not null/undefined/false)
      const rate = await store.checkFeature("user-1", "rate");
      expect(rate.hasFeature).toBe(true);
      expect(rate.value).toEqual(new Decimal("0"));

      // false → absent
      const active = await store.checkFeature("user-1", "active");
      expect(active.hasFeature).toBe(false);
      expect(active.value).toBe(false);
    });
  });
});

// ── WS9: configurable free-allowance reset window ─────────────────────────
describe("WS9 — configurable allowance reset window", () => {
  let store: MemoryStore;

  beforeEach(() => {
    store = new MemoryStore();
  });

  async function setupPlan(
    allowancePeriod: "calendar_month" | "rolling_30d" | "anniversary",
    allowanceAmount = 5,
  ): Promise<void> {
    const config = {
      version: 1,
      metering: { models: { "*": "1" } },
      plans: {
        "plan-ws9": {
          label: "Plan WS9",
          allowance: { amount: D(allowanceAmount), period: allowancePeriod },
        },
      },
    };
    await store.setActivePricing(config);
  }

  describe("rolling_30d — deductWithAllowance path", () => {
    it("resets exactly at the 30-day boundary", async () => {
      const anchor = new Date("2026-01-01T00:00:00.000Z");
      store.setClock(() => anchor);
      await setupPlan("rolling_30d");
      await store.setUserPlan("user-1", "plan-ws9"); // anchors userPlanAssignedAt at `anchor`
      await store.addCredits("user-1", D(100), "adjustment");

      const plan = await store.getUserPlan("user-1");
      expect(plan.allowancePeriod).toBe("rolling_30d");
      expect(plan.planAssignedAt?.toISOString()).toBe(anchor.toISOString());

      // Window 1: consume all 5 credits of allowance.
      const w1Start = resolveAllowanceWindow(anchor, "rolling_30d", anchor).start;
      const r1 = await store.deductWithAllowance("user-1", D(5), {
        periodStart: w1Start,
      });
      expect(r1.allowanceConsumed.toString()).toBe("5");
      const a1 = await store.checkAllowance("user-1");
      expect(a1.allowanceRemaining.toString()).toBe("0");

      // Still within window 1 (day 29) — allowance stays exhausted.
      const day29 = new Date(anchor.getTime() + 29 * 86_400_000);
      store.setClock(() => day29);
      const stillWindow1 = await store.checkAllowance("user-1");
      expect(stillWindow1.allowanceRemaining.toString()).toBe("0");

      // Day 30 — new 30-day window begins; allowance resets to full.
      const day30 = new Date(anchor.getTime() + 30 * 86_400_000);
      store.setClock(() => day30);
      const resetAllowance = await store.checkAllowance("user-1");
      expect(resetAllowance.allowanceRemaining.toString()).toBe("5");

      // Consuming in the new window uses the fresh allowance.
      const w2Start = resolveAllowanceWindow(day30, "rolling_30d", anchor).start;
      const r2 = await store.deductWithAllowance("user-1", D(3), {
        periodStart: w2Start,
      });
      expect(r2.allowanceConsumed.toString()).toBe("3");
      const a2 = await store.checkAllowance("user-1");
      expect(a2.allowanceRemaining.toString()).toBe("2");
    });
  });

  describe("rolling_30d — settleLease path", () => {
    it("resets exactly at the 30-day boundary", async () => {
      const anchor = new Date("2026-01-01T00:00:00.000Z");
      store.setClock(() => anchor);
      await setupPlan("rolling_30d");
      await store.setUserPlan("user-1", "plan-ws9");
      await store.addCredits("user-1", D(100), "adjustment");

      const w1Start = resolveAllowanceWindow(anchor, "rolling_30d", anchor).start;
      const lease1 = await store.createLease("user-1", D(5), "usage", { periodStart: w1Start });
      const s1 = await store.settleLease("user-1", lease1.leaseId, D(5), {
        periodStart: w1Start,
      });
      expect(s1.allowanceConsumed.toString()).toBe("5");
      expect((await store.checkAllowance("user-1")).allowanceRemaining.toString()).toBe("0");

      // Roll past the 30-day boundary.
      const day30 = new Date(anchor.getTime() + 30 * 86_400_000);
      store.setClock(() => day30);
      expect((await store.checkAllowance("user-1")).allowanceRemaining.toString()).toBe("5");

      const w2Start = resolveAllowanceWindow(day30, "rolling_30d", anchor).start;
      const lease2 = await store.createLease("user-1", D(2), "usage", { periodStart: w2Start });
      const s2 = await store.settleLease("user-1", lease2.leaseId, D(2), {
        periodStart: w2Start,
      });
      expect(s2.allowanceConsumed.toString()).toBe("2");
      expect((await store.checkAllowance("user-1")).allowanceRemaining.toString()).toBe("3");
    });
  });

  describe("anniversary — deductWithAllowance and settleLease paths", () => {
    it("resets on the anchor's day-of-month (clamped)", async () => {
      // Anchor day 31 in a non-leap February clamps: windows are
      // [Jan 31, Feb 28) → [Feb 28, Mar 31) → [Mar 31, Apr 30) → ...
      const anchor = new Date("2026-01-31T00:00:00.000Z");
      store.setClock(() => anchor);
      await setupPlan("anniversary");
      await store.setUserPlan("user-1", "plan-ws9");
      await store.addCredits("user-1", D(100), "adjustment");

      // Window [Jan 31, Feb 28): consume via deductWithAllowance.
      let win = resolveAllowanceWindow(anchor, "anniversary", anchor);
      const r1 = await store.deductWithAllowance("user-1", D(5), { periodStart: win.start });
      expect(r1.allowanceConsumed.toString()).toBe("5");
      expect((await store.checkAllowance("user-1")).allowanceRemaining.toString()).toBe("0");

      // Feb 15 is still inside [Jan 31, Feb 28) — no reset yet.
      const febMid = new Date("2026-02-15T00:00:00.000Z");
      store.setClock(() => febMid);
      expect((await store.checkAllowance("user-1")).allowanceRemaining.toString()).toBe("0");

      // Feb 28 crosses into [Feb 28, Mar 31) — fresh allowance.
      const feb28 = new Date("2026-02-28T00:00:00.000Z");
      store.setClock(() => feb28);
      expect((await store.checkAllowance("user-1")).allowanceRemaining.toString()).toBe("5");

      // Consume via the lease (settleLease) path this time.
      win = resolveAllowanceWindow(feb28, "anniversary", anchor);
      const lease = await store.createLease("user-1", D(4), "usage", { periodStart: win.start });
      const s1 = await store.settleLease("user-1", lease.leaseId, D(4), {
        periodStart: win.start,
      });
      expect(s1.allowanceConsumed.toString()).toBe("4");
      expect((await store.checkAllowance("user-1")).allowanceRemaining.toString()).toBe("1");

      // March 30 — still within the [Feb 28, Mar 31) window, no reset yet.
      const mar30 = new Date("2026-03-30T00:00:00.000Z");
      store.setClock(() => mar30);
      expect((await store.checkAllowance("user-1")).allowanceRemaining.toString()).toBe("1");

      // March 31 — crosses into [Mar 31, Apr 30) → allowance resets to full.
      const mar31 = new Date("2026-03-31T00:00:00.000Z");
      store.setClock(() => mar31);
      expect((await store.checkAllowance("user-1")).allowanceRemaining.toString()).toBe("5");
    });
  });

  describe("switching plans mid-window re-anchors future windows", () => {
    it("re-anchors the anniversary/rolling window to the NEW plan-assignment time", async () => {
      const t0 = new Date("2026-01-10T00:00:00.000Z");
      store.setClock(() => t0);

      const config = {
        version: 1,
        metering: { models: { "*": "1" } },
        plans: {
          "plan-a": {
            label: "Plan A",
            allowance: { amount: D(5), period: "rolling_30d" },
          },
          "plan-b": {
            label: "Plan B",
            allowance: { amount: D(9), period: "rolling_30d" },
          },
        },
      };
      await store.setActivePricing(config);
      await store.setUserPlan("user-1", "plan-a");
      const planAAssignedAt = (await store.getUserPlan("user-1")).planAssignedAt;
      expect(planAAssignedAt?.toISOString()).toBe(t0.toISOString());

      // Switch plans later — the anchor must move to the NEW assignment time.
      const t1 = new Date("2026-01-20T00:00:00.000Z");
      store.setClock(() => t1);
      await store.setUserPlan("user-1", "plan-b");
      const plan = await store.getUserPlan("user-1");
      expect(plan.planId).toBe("plan-b");
      expect(plan.allowanceAmount.toString()).toBe("9");
      expect(plan.planAssignedAt?.toISOString()).toBe(t1.toISOString());

      // The window resolved for "now" must be anchored at t1, not t0.
      const window = resolveAllowanceWindow(t1, "rolling_30d", plan.planAssignedAt ?? null);
      expect(window.start.toISOString()).toBe(t1.toISOString().slice(0, 10) + "T00:00:00.000Z");
    });
  });
});

// ── WS8: CreditStore core/optional-capability split ──────────────────────
describe("CreditStore optional capabilities (WS8)", () => {
  it("a minimal store implementing only core methods rejects an optional capability with CapabilityNotSupportedError", async () => {
    // Deliberately implements ONLY the abstract (core) methods, none of the
    // optional analytics/transaction-listing/teams methods. Every abstract
    // method just needs to satisfy the type checker; bodies are unused here.
    class MinimalStore extends CreditStore {
      async setup(): Promise<SetupResult> {
        return { tablesCreated: [], rpcsCreated: [], errors: [], success: true };
      }
      async getBalance(userId: string): Promise<BalanceResult> {
        return { userId, balance: D(0), lifetimePurchased: D(0) };
      }
      async addCredits(userId: string, amount: Decimal): Promise<AddCreditsResult> {
        return {
          transactionId: "",
          userId,
          amount,
          newBalance: amount,
          lifetimePurchased: D(0),
          bucket: "default",
        };
      }
      async deductWithAllowance(userId: string, amount: Decimal): Promise<DeductionResult> {
        return {
          transactionId: "",
          userId,
          amount,
          allowanceConsumed: D(0),
          balanceAfter: D(0),
          idempotent: false,
          capWarning: null,
        };
      }
      async createLease(userId: string, amount: Decimal): Promise<LeaseResult> {
        return {
          leaseId: "",
          userId,
          amount,
          available: D(0),
          reservedTotal: D(0),
          billingMode: "strict",
          expiresAt: "",
        };
      }
      async settleLease(userId: string): Promise<DeductionResult> {
        return {
          transactionId: "",
          userId,
          amount: D(0),
          allowanceConsumed: D(0),
          balanceAfter: D(0),
          idempotent: false,
          capWarning: null,
        };
      }
      async releaseLease(userId: string, leaseId: string): Promise<ReleaseResult> {
        return { leaseId, userId, released: true, reason: "released" };
      }
      async renewLease(userId: string, leaseId: string): Promise<LeaseResult> {
        return {
          leaseId,
          userId,
          amount: D(0),
          available: D(0),
          reservedTotal: D(0),
          billingMode: "strict",
          expiresAt: "",
        };
      }
      async getAvailable(userId: string): Promise<AvailableResult> {
        return { userId, balance: D(0), reserved: D(0), available: D(0) };
      }
      async getActivePricing() {
        return null;
      }
      async setActivePricing(): Promise<string> {
        return "";
      }
      async getPricingHistory() {
        return [];
      }
      async getPricingConfig() {
        return null;
      }
      async activatePricing(): Promise<string> {
        return "";
      }
      async getUserPlan(userId: string): Promise<GetUserPlanResult> {
        return {
          userId,
          planId: null,
          planLabel: null,
          allowanceAmount: D(0),
          allowancePeriod: null,
          entitlements: {},
          billingMode: "strict",
          perOperation: {},
          maxConcurrent: null,
          overdraftFloor: null,
          planAssignedAt: null,
        };
      }
      async setUserPlan(userId: string, planId: string): Promise<SetUserPlanResult> {
        return { userId, planId };
      }
      async unsetUserPlan(userId: string): Promise<{ userId: string }> {
        return { userId };
      }
      async checkFeatureLimit(userId: string, feature: string): Promise<FeatureLimitResult> {
        return { feature, limit: D(0), used: D(0), remaining: D(0), periodStart: "" };
      }
      async checkFeature(userId: string, feature: string): Promise<CheckFeatureResult> {
        return { userId, feature, value: null, hasFeature: false };
      }
      async checkAllowance(): Promise<AllowanceResult> {
        return { planId: "", allowanceRemaining: D(0), periodStart: "", periodEnd: "" };
      }
      async incrementUsageWindow(): Promise<void> {
        return;
      }
      async checkSpendCap(): Promise<CapCheckResult> {
        return { capped: false, currentSpend: D(0), limit: D(0), onExceed: null };
      }
      async refundCredits(transactionId: string): Promise<RefundResult> {
        return {
          refundTransactionId: "",
          originalTransactionId: transactionId,
          userId: "",
          amount: D(0),
          newBalance: D(0),
        };
      }
      async sweepExpiredCredits(dryRun = false): Promise<SweepResult> {
        return { expiredCount: 0, expiredAmount: D(0), dryRun };
      }
      async listUsageEvents(): Promise<PaginatedTransactions> {
        return { items: [], total: 0 };
      }
      // Credit tiers: getCreditTiers is a CORE (required) abstract method, not
      // an optional capability — a minimal store must implement it too.
      async getCreditTiers(userId: string): Promise<BucketBalancesResult> {
        return { userId, buckets: [], totalBalance: D(0) };
      }
    }

    const minimal = new MinimalStore();
    await expect(minimal.createTeam("t1")).rejects.toThrow(CapabilityNotSupportedError);
    await expect(minimal.getTeamBalance("t1")).rejects.toThrow(CapabilityNotSupportedError);
    await expect(minimal.addTeamMember("t1", "u1")).rejects.toThrow(CapabilityNotSupportedError);
    await expect(minimal.getTeamMembers("t1")).rejects.toThrow(CapabilityNotSupportedError);
    await expect(minimal.deductTeam("t1", "u1", D(1))).rejects.toThrow(CapabilityNotSupportedError);
    await expect(minimal.spendByUser(new Date(), new Date())).rejects.toThrow(
      CapabilityNotSupportedError,
    );
    await expect(minimal.spendByModel(new Date(), new Date())).rejects.toThrow(
      CapabilityNotSupportedError,
    );
    await expect(minimal.topUsers(10, new Date(), new Date())).rejects.toThrow(
      CapabilityNotSupportedError,
    );
    await expect(minimal.dailySpend(new Date(), new Date())).rejects.toThrow(
      CapabilityNotSupportedError,
    );
    await expect(minimal.aggregateStats(new Date(), new Date())).rejects.toThrow(
      CapabilityNotSupportedError,
    );
    await expect(minimal.listUserTransactions("u1")).rejects.toThrow(CapabilityNotSupportedError);
  });

  it("MemoryStore overrides every optional capability concretely (no CapabilityNotSupportedError)", async () => {
    // MemoryStore implements all optional methods, so calling them must NOT throw.
    const full = new MemoryStore();
    await expect(full.spendByUser(new Date(0), new Date())).resolves.toBeDefined();
    const team = await full.createTeam("full-team");
    await expect(full.getTeamBalance(team.teamId)).resolves.toBeDefined();
  });
});

// ── Feature limits (per-feature invocation-count limits) ───────────────────
//
// Mirrors the Python `TestFeatureLimits` track: counting is ledger-derived
// (committed `usage` transactions tagged `metadata.feature`), exactly like
// spend caps — no separate counter storage. `feature`/`featureLimit`/
// `featurePeriodStart` are resolved by the manager in production; here we
// exercise the store directly (mirrors how spend caps are tested above).
describe("Feature limits (per-feature invocation-count limits)", () => {
  let store: MemoryStore;
  // Default clock: any instant inside [WINDOW_START, WINDOW_START + 1 month) so
  // transactions committed in tests without their own `setClock` land inside
  // the fixed WINDOW_START/WINDOW_END used throughout. Window-rollover tests
  // override the clock explicitly per period.
  const DEFAULT_NOW = new Date("2026-03-15T00:00:00.000Z");

  beforeEach(() => {
    store = new MemoryStore();
    store.setClock(() => DEFAULT_NOW);
  });

  const WINDOW_START = new Date("2026-03-01T00:00:00.000Z"); // aligned "monthly" start

  describe("deductWithAllowance", () => {
    it("no featureLimit given: feature is tagged but never enforced", async () => {
      await store.addCredits("user-1", D(100), "purchase");
      for (let i = 0; i < 10; i++) {
        const r = await store.deductWithAllowance("user-1", D(1), { feature: "export" });
        expect(r.error).toBeUndefined();
      }
      const bal = await store.getBalance("user-1");
      expect(bal.balance.toString()).toBe("90");
    });

    it("under the limit: deduction succeeds, no warning", async () => {
      await store.addCredits("user-1", D(100), "purchase");
      const featureLimit = { maxCalls: 3, period: "monthly" as const, onExceed: "deny" as const };
      const r1 = await store.deductWithAllowance("user-1", D(1), {
        feature: "export",
        featureLimit,
        featurePeriodStart: WINDOW_START,
      });
      expect(r1.error).toBeUndefined();
      expect(r1.featureLimitWarning ?? null).toBeNull();
    });

    it("at the limit: deny blocks with feature_limit_reached, nothing consumed", async () => {
      await store.addCredits("user-1", D(100), "purchase");
      const featureLimit = { maxCalls: 2, period: "monthly" as const, onExceed: "deny" as const };
      const opts = { feature: "export", featureLimit, featurePeriodStart: WINDOW_START };
      await store.deductWithAllowance("user-1", D(1), opts);
      await store.deductWithAllowance("user-1", D(1), opts);
      // Third call: count (2) >= maxCalls (2) → deny.
      const r3 = await store.deductWithAllowance("user-1", D(1), opts);
      expect(r3.error).toBe("feature_limit_reached");
      // Nothing consumed by the blocked call.
      const bal = await store.getBalance("user-1");
      expect(bal.balance.toString()).toBe("98");
    });

    it("over the limit (maxCalls: 0): every call is denied", async () => {
      await store.addCredits("user-1", D(100), "purchase");
      const featureLimit = { maxCalls: 0, period: "monthly" as const, onExceed: "deny" as const };
      const r = await store.deductWithAllowance("user-1", D(1), {
        feature: "export",
        featureLimit,
        featurePeriodStart: WINDOW_START,
      });
      expect(r.error).toBe("feature_limit_reached");
    });

    it("warn onExceed: breach surfaces featureLimitWarning but does NOT block", async () => {
      await store.addCredits("user-1", D(100), "purchase");
      const featureLimit = { maxCalls: 1, period: "monthly" as const, onExceed: "warn" as const };
      const opts = { feature: "export", featureLimit, featurePeriodStart: WINDOW_START };
      const r1 = await store.deductWithAllowance("user-1", D(1), opts);
      expect(r1.error).toBeUndefined();
      expect(r1.featureLimitWarning ?? null).toBeNull();
      // Second call: count (1) >= maxCalls (1) → warn, but still succeeds.
      const r2 = await store.deductWithAllowance("user-1", D(1), opts);
      expect(r2.error).toBeUndefined();
      expect(r2.featureLimitWarning).toBe("warn");
      const bal = await store.getBalance("user-1");
      expect(bal.balance.toString()).toBe("98");
    });

    it("notify onExceed: breach surfaces featureLimitWarning='notify', does NOT block", async () => {
      await store.addCredits("user-1", D(100), "purchase");
      const featureLimit = { maxCalls: 1, period: "monthly" as const, onExceed: "notify" as const };
      const opts = { feature: "export", featureLimit, featurePeriodStart: WINDOW_START };
      await store.deductWithAllowance("user-1", D(1), opts);
      const r2 = await store.deductWithAllowance("user-1", D(1), opts);
      expect(r2.error).toBeUndefined();
      expect(r2.featureLimitWarning).toBe("notify");
    });

    it("isolation: a different feature name has an independent count", async () => {
      await store.addCredits("user-1", D(100), "purchase");
      const featureLimit = { maxCalls: 1, period: "monthly" as const, onExceed: "deny" as const };
      await store.deductWithAllowance("user-1", D(1), {
        feature: "export",
        featureLimit,
        featurePeriodStart: WINDOW_START,
      });
      // "import" is a different feature — its own count starts at 0.
      const r = await store.deductWithAllowance("user-1", D(1), {
        feature: "import",
        featureLimit,
        featurePeriodStart: WINDOW_START,
      });
      expect(r.error).toBeUndefined();
    });

    it("isolation: a different user has an independent count", async () => {
      await store.addCredits("user-1", D(100), "purchase");
      await store.addCredits("user-2", D(100), "purchase");
      const featureLimit = { maxCalls: 1, period: "monthly" as const, onExceed: "deny" as const };
      const opts = { feature: "export", featureLimit, featurePeriodStart: WINDOW_START };
      await store.deductWithAllowance("user-1", D(1), opts);
      // user-1 is now at the limit; user-2 is unaffected.
      const blocked = await store.deductWithAllowance("user-1", D(1), opts);
      expect(blocked.error).toBe("feature_limit_reached");
      const other = await store.deductWithAllowance("user-2", D(1), opts);
      expect(other.error).toBeUndefined();
    });

    it("accumulation-then-block across N deducts", async () => {
      await store.addCredits("user-1", D(100), "purchase");
      const featureLimit = { maxCalls: 5, period: "monthly" as const, onExceed: "deny" as const };
      const opts = { feature: "export", featureLimit, featurePeriodStart: WINDOW_START };
      for (let i = 0; i < 5; i++) {
        const r = await store.deductWithAllowance("user-1", D(1), opts);
        expect(r.error).toBeUndefined();
      }
      const blocked = await store.deductWithAllowance("user-1", D(1), opts);
      expect(blocked.error).toBe("feature_limit_reached");
      const bal = await store.getBalance("user-1");
      expect(bal.balance.toString()).toBe("95");
    });

    it("a call fully covered by free allowance (net amount 0) still counts as one invocation", async () => {
      const config = {
        version: 1,
        metering: { models: { "*": "1" } },
        plans: {
          free: { label: "Free", allowance: { amount: D(100), period: "calendar_month" } },
        },
      };
      await store.setActivePricing(config);
      await store.setUserPlan("user-1", "free");
      const featureLimit = { maxCalls: 1, period: "monthly" as const, onExceed: "deny" as const };
      const opts = { feature: "export", featureLimit, featurePeriodStart: WINDOW_START };

      // Fully covered by the free allowance — net charged amount is 0, but the
      // feature was still invoked once (unlike spend-cap counting, which only
      // cares about actual dollars spent).
      const r1 = await store.deductWithAllowance("user-1", D(1), opts);
      expect(r1.error).toBeUndefined();
      expect(r1.amount.toString()).toBe("0");
      expect(r1.allowanceConsumed.toString()).toBe("1");

      const r2 = await store.deductWithAllowance("user-1", D(1), opts);
      expect(r2.error).toBe("feature_limit_reached");
    });

    it("always tags metadata.feature even when no limit is configured (accurate future history)", async () => {
      await store.addCredits("user-1", D(100), "purchase");
      await store.deductWithAllowance("user-1", D(1), { feature: "export" });
      // Now enable a limit for the SAME window: the untagged-limit call above
      // must already count toward it (it was tagged at deduction time).
      const featureLimit = { maxCalls: 1, period: "monthly" as const, onExceed: "deny" as const };
      const r = await store.deductWithAllowance("user-1", D(1), {
        feature: "export",
        featureLimit,
        featurePeriodStart: WINDOW_START,
      });
      expect(r.error).toBe("feature_limit_reached");
    });

    describe("window rollover", () => {
      it("daily: resets at the next UTC midnight", async () => {
        await store.addCredits("user-1", D(100), "purchase");
        const day1 = new Date("2026-03-15T00:00:00.000Z");
        const day2 = new Date("2026-03-16T00:00:00.000Z");
        const featureLimit = { maxCalls: 1, period: "daily" as const, onExceed: "deny" as const };

        store.setClock(() => day1);
        const r1 = await store.deductWithAllowance("user-1", D(1), {
          feature: "export",
          featureLimit,
          featurePeriodStart: day1,
        });
        expect(r1.error).toBeUndefined();
        const blocked = await store.deductWithAllowance("user-1", D(1), {
          feature: "export",
          featureLimit,
          featurePeriodStart: day1,
        });
        expect(blocked.error).toBe("feature_limit_reached");

        store.setClock(() => day2);
        const r2 = await store.deductWithAllowance("user-1", D(1), {
          feature: "export",
          featureLimit,
          featurePeriodStart: day2,
        });
        expect(r2.error).toBeUndefined();
      });

      it("weekly: resets on the next Monday (ISO week)", async () => {
        await store.addCredits("user-1", D(100), "purchase");
        const week1 = new Date("2026-03-16T00:00:00.000Z"); // Monday
        const week2 = new Date("2026-03-23T00:00:00.000Z"); // next Monday
        const featureLimit = { maxCalls: 1, period: "weekly" as const, onExceed: "deny" as const };

        store.setClock(() => week1);
        await store.deductWithAllowance("user-1", D(1), {
          feature: "export",
          featureLimit,
          featurePeriodStart: week1,
        });
        const blocked = await store.deductWithAllowance("user-1", D(1), {
          feature: "export",
          featureLimit,
          featurePeriodStart: week1,
        });
        expect(blocked.error).toBe("feature_limit_reached");

        store.setClock(() => week2);
        const r2 = await store.deductWithAllowance("user-1", D(1), {
          feature: "export",
          featureLimit,
          featurePeriodStart: week2,
        });
        expect(r2.error).toBeUndefined();
      });

      it("monthly: resets on the 1st of the next UTC month", async () => {
        await store.addCredits("user-1", D(100), "purchase");
        const month1 = new Date("2026-03-01T00:00:00.000Z");
        const month2 = new Date("2026-04-01T00:00:00.000Z");
        const featureLimit = { maxCalls: 1, period: "monthly" as const, onExceed: "deny" as const };

        store.setClock(() => month1);
        await store.deductWithAllowance("user-1", D(1), {
          feature: "export",
          featureLimit,
          featurePeriodStart: month1,
        });
        const blocked = await store.deductWithAllowance("user-1", D(1), {
          feature: "export",
          featureLimit,
          featurePeriodStart: month1,
        });
        expect(blocked.error).toBe("feature_limit_reached");

        store.setClock(() => month2);
        const r2 = await store.deductWithAllowance("user-1", D(1), {
          feature: "export",
          featureLimit,
          featurePeriodStart: month2,
        });
        expect(r2.error).toBeUndefined();
      });

      it("yearly: resets on Jan 1 of the next year", async () => {
        await store.addCredits("user-1", D(100), "purchase");
        const year1 = new Date("2026-01-01T00:00:00.000Z");
        const year2 = new Date("2027-01-01T00:00:00.000Z");
        const featureLimit = { maxCalls: 1, period: "yearly" as const, onExceed: "deny" as const };

        store.setClock(() => year1);
        await store.deductWithAllowance("user-1", D(1), {
          feature: "export",
          featureLimit,
          featurePeriodStart: year1,
        });
        const blocked = await store.deductWithAllowance("user-1", D(1), {
          feature: "export",
          featureLimit,
          featurePeriodStart: year1,
        });
        expect(blocked.error).toBe("feature_limit_reached");

        store.setClock(() => year2);
        const r2 = await store.deductWithAllowance("user-1", D(1), {
          feature: "export",
          featureLimit,
          featurePeriodStart: year2,
        });
        expect(r2.error).toBeUndefined();
      });
    });
  });

  describe("createLease (deny-only admission)", () => {
    it("deny onExceed: admission is blocked once the limit is reached", async () => {
      await store.addCredits("user-1", D(100), "purchase");
      const featureLimit = { maxCalls: 1, period: "monthly" as const, onExceed: "deny" as const };
      const opts = { feature: "export", featureLimit, featurePeriodStart: WINDOW_START };
      // Commit one usage transaction tagged `feature` via a direct deduct (the
      // ledger-derived count only sees committed `usage` rows).
      await store.deductWithAllowance("user-1", D(1), opts);
      const lease = await store.createLease("user-1", D(1), "usage", opts);
      expect(lease.error).toBe("feature_limit_reached");
    });

    it("warn/notify are NOT checked at admission (nothing to warn about yet)", async () => {
      await store.addCredits("user-1", D(100), "purchase");
      const featureLimit = { maxCalls: 1, period: "monthly" as const, onExceed: "warn" as const };
      const opts = { feature: "export", featureLimit, featurePeriodStart: WINDOW_START };
      await store.deductWithAllowance("user-1", D(1), opts);
      // Count is already at the limit, but action='warn' is never enforced at
      // admission — the lease must be granted.
      const lease = await store.createLease("user-1", D(1), "usage", opts);
      expect(lease.error).toBeUndefined();
    });

    it("under the limit: admission succeeds", async () => {
      await store.addCredits("user-1", D(100), "purchase");
      const featureLimit = { maxCalls: 5, period: "monthly" as const, onExceed: "deny" as const };
      const lease = await store.createLease("user-1", D(1), "usage", {
        feature: "export",
        featureLimit,
        featurePeriodStart: WINDOW_START,
      });
      expect(lease.error).toBeUndefined();
    });
  });

  describe("settleLease (advisory only, never blocks)", () => {
    it("deny action breach at settle: featureLimitWarning='deny', settle still succeeds", async () => {
      await store.addCredits("user-1", D(100), "purchase");
      const featureLimit = { maxCalls: 1, period: "monthly" as const, onExceed: "deny" as const };
      const opts = { feature: "export", featureLimit, featurePeriodStart: WINDOW_START };
      // Pre-fill the count to the limit via a committed deduction.
      await store.deductWithAllowance("user-1", D(1), opts);
      // Reserve+settle without a feature limit on the lease itself (create_lease
      // deny would otherwise reject it) to reach settle with the count already
      // at the limit.
      const lease = await store.createLease("user-1", D(1), "usage");
      const settled = await store.settleLease("user-1", lease.leaseId, D(1), opts);
      expect(settled.error).toBeUndefined();
      expect(settled.featureLimitWarning).toBe("deny");
    });

    it("tags metadata.feature on the settled transaction (countable for future checks)", async () => {
      await store.addCredits("user-1", D(100), "purchase");
      const featureLimit = { maxCalls: 5, period: "monthly" as const, onExceed: "deny" as const };
      const opts = { feature: "export", featureLimit, featurePeriodStart: WINDOW_START };
      const lease = await store.createLease("user-1", D(1), "usage", opts);
      await store.settleLease("user-1", lease.leaseId, D(1), opts);

      const usage = await store.checkFeatureLimit(
        "user-1",
        "export",
        5,
        WINDOW_START,
        new Date("2026-04-01T00:00:00.000Z"),
      );
      expect(usage.used).toBe(1);
    });
  });

  describe("release/refund do NOT restore quota", () => {
    it("release_lease never counted in the first place (no usage row inserted)", async () => {
      await store.addCredits("user-1", D(100), "purchase");
      const featureLimit = { maxCalls: 1, period: "monthly" as const, onExceed: "deny" as const };
      const opts = { feature: "export", featureLimit, featurePeriodStart: WINDOW_START };

      // Reserve then release near the limit — a lease never inserts a usage
      // row, so it was never counted, and releasing changes nothing.
      const lease = await store.createLease("user-1", D(1), "usage", opts);
      expect(lease.error).toBeUndefined();
      await store.releaseLease("user-1", lease.leaseId);

      // The count is still 0 — a fresh deduct against the same limit succeeds.
      const r = await store.deductWithAllowance("user-1", D(1), opts);
      expect(r.error).toBeUndefined();
    });

    it("refund_credits does not free up quota (the counted row is untouched)", async () => {
      await store.addCredits("user-1", D(100), "purchase");
      const featureLimit = { maxCalls: 1, period: "monthly" as const, onExceed: "deny" as const };
      const opts = { feature: "export", featureLimit, featurePeriodStart: WINDOW_START };

      const deduct = await store.deductWithAllowance("user-1", D(1), opts);
      expect(deduct.error).toBeUndefined();
      const refund = await store.refundCredits(deduct.transactionId);
      expect(refund.error).toBeUndefined();

      // The original usage row (and therefore the count) is unaffected by the
      // refund — a subsequent call still sees count >= maxCalls.
      const blocked = await store.deductWithAllowance("user-1", D(1), opts);
      expect(blocked.error).toBe("feature_limit_reached");
    });
  });

  describe("checkFeatureLimit (advisory read)", () => {
    it("counts committed usage rows tagged with the feature in the window", async () => {
      await store.addCredits("user-1", D(100), "purchase");
      await store.deductWithAllowance("user-1", D(1), { feature: "export" });
      await store.deductWithAllowance("user-1", D(1), { feature: "export" });
      await store.deductWithAllowance("user-1", D(1), { feature: "import" }); // different feature

      const result = await store.checkFeatureLimit(
        "user-1",
        "export",
        5,
        WINDOW_START,
        new Date("2026-04-01T00:00:00.000Z"),
      );
      expect(result.limited).toBe(true);
      expect(result.limit).toBe(5);
      expect(result.used).toBe(2);
      expect(result.remaining).toBe(3);
    });

    it("remaining floors at 0 when used exceeds max", async () => {
      await store.addCredits("user-1", D(100), "purchase");
      await store.deductWithAllowance("user-1", D(1), { feature: "export" });
      await store.deductWithAllowance("user-1", D(1), { feature: "export" });

      const result = await store.checkFeatureLimit(
        "user-1",
        "export",
        1,
        WINDOW_START,
        new Date("2026-04-01T00:00:00.000Z"),
      );
      expect(result.used).toBe(2);
      expect(result.remaining).toBe(0);
    });

    it("has no side effects and never blocks", async () => {
      await store.addCredits("user-1", D(100), "purchase");
      const before = await store.checkFeatureLimit(
        "user-1",
        "export",
        0,
        WINDOW_START,
        new Date("2026-04-01T00:00:00.000Z"),
      );
      expect(before.used).toBe(0);
      // A subsequent deduct is unaffected by the check above.
      const r = await store.deductWithAllowance("user-1", D(1), { feature: "export" });
      expect(r.error).toBeUndefined();
    });
  });

  describe("PlanDefinition.featureLimits / GetUserPlanResult.featureLimits round-trip", () => {
    it("setActivePricing + getUserPlan surfaces configured feature limits", async () => {
      const config = {
        version: 1,
        metering: { models: { "*": "1" } },
        plans: {
          free: {
            label: "Free",
            allowance: { amount: D(0), period: "calendar_month" },
            entitlements: {
              export: { maxCalls: 5, period: "monthly", onExceed: "deny" },
              hdExport: { maxCalls: 2, period: "weekly", onExceed: "warn" },
            },
          },
        },
      };
      await store.setActivePricing(config);
      await store.setUserPlan("user-1", "free");

      const plan = await store.getUserPlan("user-1");
      expect(plan.entitlements?.["export"]).toEqual({
        maxCalls: 5,
        period: "monthly",
        onExceed: "deny",
      });
      expect(plan.entitlements?.["hdExport"]).toEqual({
        maxCalls: 2,
        period: "weekly",
        onExceed: "warn",
      });
    });

    it("plan with no featureLimits configured returns an empty object", async () => {
      const config = {
        version: 1,
        metering: { models: { "*": "1" } },
        plans: {
          free: { label: "Free", allowance: { amount: D(0), period: "calendar_month" } },
        },
      };
      await store.setActivePricing(config);
      await store.setUserPlan("user-1", "free");

      const plan = await store.getUserPlan("user-1");
      expect(plan.entitlements).toEqual({});
    });
  });
});
