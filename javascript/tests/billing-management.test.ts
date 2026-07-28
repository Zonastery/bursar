import { describe, expect, it, vi } from "vitest";
import type { BillingStore } from "../src/billing/billing-store.js";
import { BillingManagement } from "../src/billing/management.js";

describe("BillingManagement", () => {
  it("returns provider-aware cancellable subscriptions while preserving the ID-only API", async () => {
    const getUserSubscriptions = vi.fn().mockResolvedValue([
      {
        userId: "user-1",
        provider: "dodo",
        providerSubscriptionId: "sub-dodo",
        status: "active",
      },
      {
        userId: "user-1",
        provider: "stripe",
        providerSubscriptionId: "sub-stripe",
        status: "trialing",
      },
      {
        userId: "user-1",
        provider: "dodo",
        providerSubscriptionId: "sub-ended",
        status: "canceled",
      },
    ]);
    const management = new BillingManagement({
      getUserSubscriptions,
    } as unknown as BillingStore);

    await expect(management.listCancellableSubscriptions("user-1")).resolves.toEqual([
      expect.objectContaining({ provider: "dodo", providerSubscriptionId: "sub-dodo" }),
      expect.objectContaining({ provider: "stripe", providerSubscriptionId: "sub-stripe" }),
    ]);
    await expect(management.listCancellableProviderSubscriptionIds("user-1")).resolves.toEqual([
      "sub-dodo",
      "sub-stripe",
    ]);
  });
});
