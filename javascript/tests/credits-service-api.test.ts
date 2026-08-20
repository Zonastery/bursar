import { Decimal } from "decimal.js";
import { describe, expect, it, vi } from "vitest";

import { CreditsService } from "../src/credits/service.js";
import type { CreditStore } from "../src/credits/store.js";
import { CreditEventEmitter } from "../src/credits/events.js";
import { RefundError } from "../src/errors.js";
import { requireStableKey, scopedStableKey } from "../src/shared/idempotency.js";

function service(store: Partial<CreditStore>): CreditsService {
  // SAFETY: Each fixture implements the CreditStore methods exercised by its scenario.
  return new CreditsService(store as CreditStore);
}

function testStore(store: Partial<CreditStore>): CreditStore {
  // SAFETY: Each fixture implements the CreditStore methods exercised by its scenario.
  return store as CreditStore;
}

describe("CreditsService public amount validation", () => {
  it("counts Unicode code points at the idempotency-key boundary", () => {
    const maximumKey = "😀".repeat(255);
    expect(requireStableKey(maximumKey)).toBe(maximumKey);
    expect(() => requireStableKey("😀".repeat(256))).toThrow(/at most 255/);

    const readableBoundary = "x".repeat(247);
    expect(scopedStableKey(readableBoundary, "reserve")).toBe(`${readableBoundary}:reserve`);
    const firstScoped = scopedStableKey(`${"😀".repeat(254)}a`, "reserve");
    const secondScoped = scopedStableKey(`${"😀".repeat(254)}b`, "reserve");
    expect(scopedStableKey(`${"😀".repeat(254)}a`, "reserve")).toBe(firstScoped);
    expect(firstScoped).not.toBe(secondScoped);
    expect(Array.from(firstScoped)).toHaveLength(79);

    const componentSets = [
      ["a", "b:c"],
      ["a:b", "c"],
    ] as const;
    expect(scopedStableKey("key", "cancel-all", componentSets[0])).not.toBe(
      scopedStableKey("key", "cancel-all", componentSets[1]),
    );
    expect(scopedStableKey("x".repeat(255), "cancel-all", componentSets[0])).not.toBe(
      scopedStableKey("x".repeat(255), "cancel-all", componentSets[1]),
    );
  });

  it.each([
    [
      "addCredits",
      (credits: CreditsService) => credits.addCredits("user-1", "-1", { idempotencyKey: "add-1" }),
    ],
    [
      "deductCredits",
      (credits: CreditsService) =>
        credits.deductCredits("user-1", "-1", { idempotencyKey: "deduct-1" }),
    ],
    [
      "grantSubscriptionCycle",
      (credits: CreditsService) =>
        credits.grantSubscriptionCycle("user-1", "0", { idempotencyKey: "cycle-1" }),
    ],
    [
      "refundCredits",
      (credits: CreditsService) =>
        credits.refundCredits("entry-1", { amount: "0", idempotencyKey: "refund-1" }),
    ],
  ])("rejects invalid %s amounts before calling the store", async (_name, invoke) => {
    const addCredits = vi.fn();
    const refundCredits = vi.fn();
    const credits = service({ addCredits, refundCredits });

    await expect(invoke(credits)).rejects.toThrow(/finite and greater than zero/);
    expect(addCredits).not.toHaveBeenCalled();
    expect(refundCredits).not.toHaveBeenCalled();
  });

  it("rejects negative raw lease amounts before admission", async () => {
    const createLease = vi.fn();
    const credits = service({
      getUserPlan: vi.fn().mockResolvedValue({ planId: null }),
      createLease,
    });

    await expect(
      credits.reserve("user-1", new Decimal(-1), { idempotencyKey: "reserve-1" }),
    ).rejects.toThrow(/finite and non-negative/);
    expect(createLease).not.toHaveBeenCalled();
  });

  it("does not treat a boolean as one credit", async () => {
    const addCredits = vi.fn();
    const credits = service({ addCredits });

    await expect(
      // SAFETY: This deliberately invalid runtime value exercises amount validation.
      credits.addCredits("user-1", true as never, { idempotencyKey: "add-boolean" }),
    ).rejects.toThrow(/Decimal or decimal string/);
    expect(addCredits).not.toHaveBeenCalled();
  });

  it("rejects a missing caller-stable mutation key before calling the store", async () => {
    const addCredits = vi.fn();
    const credits = service({ addCredits });

    // SAFETY: This deliberately invalid runtime value exercises required-key validation.
    await expect(credits.addCredits("user-1", "1", undefined as never)).rejects.toThrow(
      /idempotencyKey/,
    );
    expect(addCredits).not.toHaveBeenCalled();
  });

  it("rejects native numbers instead of silently rounding monetary input", async () => {
    const addCredits = vi.fn();
    const credits = service({ addCredits });

    await expect(
      // SAFETY: This deliberately invalid runtime value exercises exact-money validation.
      credits.addCredits("user-1", 0.1 as never, { idempotencyKey: "add-number" }),
    ).rejects.toThrow(/Decimal or decimal string/);
    expect(addCredits).not.toHaveBeenCalled();
  });

  it("rejects native-number lease estimates before querying account state", async () => {
    const getUserPlan = vi.fn();
    const credits = service({ getUserPlan });

    await expect(
      // SAFETY: This deliberately invalid runtime value exercises exact-money validation.
      credits.reserve("user-1", 0.1 as never, { idempotencyKey: "reserve-number" }),
    ).rejects.toThrow(/Decimal or decimal string/);
    expect(getUserPlan).not.toHaveBeenCalled();
  });

  it("rejects malformed decimal strings before calling the store", async () => {
    const addCredits = vi.fn();
    const credits = service({ addCredits });

    await expect(
      credits.addCredits("user-1", "ten credits", { idempotencyKey: "add-malformed" }),
    ).rejects.toThrow(/valid decimal string/);
    expect(addCredits).not.toHaveBeenCalled();
  });
});

describe("grantSubscriptionCycle replay", () => {
  const replay = {
    entryId: "entry-1",
    userId: "user-1",
    amount: new Decimal(10),
    newBalance: new Decimal(10),
    lifetimePurchased: new Decimal(10),
    bucket: "subscription",
    idempotent: true,
  };

  it("does not re-anchor an already assigned plan", async () => {
    const setUserPlan = vi.fn();
    const credits = service({
      addCredits: vi.fn().mockResolvedValue(replay),
      getUserPlan: vi.fn().mockResolvedValue({ planKey: "pro" }),
      setUserPlan,
    });

    await credits.grantSubscriptionCycle("user-1", "10", {
      planKey: "pro",
      idempotencyKey: "invoice-1",
    });

    expect(setUserPlan).not.toHaveBeenCalled();
  });

  it("repairs an interrupted grant-to-plan assignment", async () => {
    const setUserPlan = vi.fn().mockResolvedValue({});
    const credits = service({
      addCredits: vi.fn().mockResolvedValue(replay),
      getUserPlan: vi.fn().mockResolvedValue({ planKey: "free" }),
      setUserPlan,
    });

    await credits.grantSubscriptionCycle("user-1", "10", {
      planKey: "pro",
      idempotencyKey: "invoice-1",
    });

    expect(setUserPlan).toHaveBeenCalledWith("user-1", "pro", undefined);
  });
});

describe("administrative credit mutations", () => {
  it("deducts an exact decimal amount and preserves the caller's accounting context", async () => {
    const addCredits = vi.fn().mockResolvedValue({
      entryId: "debit-entry-1",
      userId: "user-1",
      amount: new Decimal("-12.345678"),
      newBalance: new Decimal("87.654322"),
      lifetimePurchased: new Decimal("100"),
      bucket: "purchased",
      idempotent: false,
    });
    const emitter = new CreditEventEmitter();
    const events: { type: string; amount?: unknown }[] = [];
    emitter.on("credits.deducted", (event) => {
      events.push({ type: event.type, amount: event.data?.amount });
    });
    const credits = new CreditsService(testStore({ addCredits }), null, emitter);

    const result = await credits.deductCredits("user-1", "12.345678", {
      entryType: "refund_clawback",
      bucket: "purchased",
      metadata: { providerRefundId: "refund-1" },
      idempotencyKey: "refund-clawback-1",
    });

    expect(addCredits).toHaveBeenCalledWith("user-1", new Decimal("-12.345678"), {
      type: "refund_clawback",
      metadata: { providerRefundId: "refund-1" },
      bucket: "purchased",
      idempotencyKey: "refund-clawback-1",
    });
    expect(result.amount.toString()).toBe("-12.345678");
    expect(events).toEqual([{ type: "credits.deducted", amount: result.amount }]);
  });

  it("refunds an exact partial amount and emits only the committed refund event", async () => {
    const refundCredits = vi.fn().mockResolvedValue({
      originalEntryId: "usage-entry-1",
      userId: "user-1",
      error: null,
      refundEntryId: "refund-entry-1",
      amount: new Decimal("2.345678"),
      newBalance: new Decimal("52.345678"),
      bucketBreakdown: { purchased: new Decimal("52.345678") },
    });
    const emitter = new CreditEventEmitter();
    const events: string[] = [];
    emitter.on("credits.refunded", (event) => {
      events.push(event.type);
    });
    emitter.on("credits.refund_failed", (event) => {
      events.push(event.type);
    });
    const credits = new CreditsService(testStore({ refundCredits }), null, emitter);

    const result = await credits.refundCredits("usage-entry-1", {
      amount: "2.345678",
      reason: "provider partial refund",
      metadata: { providerRefundId: "provider-refund-1" },
      idempotencyKey: "refund-operation-1",
    });

    expect(refundCredits).toHaveBeenCalledWith("usage-entry-1", {
      amount: new Decimal("2.345678"),
      reason: "provider partial refund",
      metadata: { providerRefundId: "provider-refund-1" },
      idempotencyKey: "refund-operation-1",
    });
    expect(result.amount.toString()).toBe("2.345678");
    expect(events).toEqual(["credits.refunded"]);
  });

  it("raises a typed error and emits no success event when a refund is rejected", async () => {
    const refundCredits = vi.fn().mockResolvedValue({
      originalEntryId: "usage-entry-1",
      userId: "user-1",
      error: "refund_exceeds_original",
      refundEntryId: null,
      amount: null,
      newBalance: null,
      bucketBreakdown: null,
    });
    const emitter = new CreditEventEmitter();
    const events: string[] = [];
    emitter.on("credits.refunded", (event) => {
      events.push(event.type);
    });
    emitter.on("credits.refund_failed", (event) => {
      events.push(event.type);
    });
    const credits = new CreditsService(testStore({ refundCredits }), null, emitter);

    await expect(
      credits.refundCredits("usage-entry-1", {
        amount: "99.000000",
        reason: "duplicate provider refund",
        idempotencyKey: "refund-operation-2",
      }),
    ).rejects.toBeInstanceOf(RefundError);

    expect(events).toEqual(["credits.refund_failed"]);
    expect(refundCredits).toHaveBeenCalledOnce();
  });
});
