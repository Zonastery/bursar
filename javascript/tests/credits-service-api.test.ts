import Decimal from "decimal.js";
import { describe, expect, it, vi } from "vitest";

import { CreditsService } from "../src/credits/service.js";
import type { CreditStore } from "../src/credits/store.js";

function service(store: Record<string, unknown>): CreditsService {
  return new CreditsService(store as unknown as CreditStore);
}

describe("CreditsService public amount validation", () => {
  it.each([
    ["addCredits", (credits: CreditsService) => credits.addCredits("user-1", -1)],
    ["deductCredits", (credits: CreditsService) => credits.deductCredits("user-1", -1)],
    [
      "grantSubscriptionCycle",
      (credits: CreditsService) => credits.grantSubscriptionCycle("user-1", 0),
    ],
    ["refundCredits", (credits: CreditsService) => credits.refundCredits("entry-1", { amount: 0 })],
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

    await expect(credits.reserve("user-1", new Decimal(-1))).rejects.toThrow(
      /finite and non-negative/,
    );
    expect(createLease).not.toHaveBeenCalled();
  });

  it("does not treat a boolean as one credit", async () => {
    const addCredits = vi.fn();
    const credits = service({ addCredits });

    await expect(credits.addCredits("user-1", true as never)).rejects.toThrow(/Decimal or number/);
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

    await credits.grantSubscriptionCycle("user-1", 10, {
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

    await credits.grantSubscriptionCycle("user-1", 10, {
      planKey: "pro",
      idempotencyKey: "invoice-1",
    });

    expect(setUserPlan).toHaveBeenCalledWith("user-1", "pro", undefined);
  });
});
