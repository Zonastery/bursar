import { describe, expect, it, vi } from "vitest";
import { LeaseNotFoundError } from "../src/errors.js";
import { PricingRuntime } from "../src/credits/pricing-runtime.js";
import type { CreditStore } from "../src/credits/store.js";
import { noopLogger } from "../src/shared/logger.js";

const CONFIG = {
  version: 1,
  pricing: {
    operations: {
      completion: {
        measures: { tokens: { unit: "token" } },
        dimensions: {},
      },
    },
    rate_cards: {
      captured: {
        operations: {
          completion: {
            rules: [],
            unmatched: {
              action: "charge",
              charge: { type: "expression", formula: "tokens * 2" },
            },
          },
        },
      },
      current: {
        operations: {
          completion: {
            rules: [],
            unmatched: {
              action: "charge",
              charge: { type: "expression", formula: "tokens * 10" },
            },
          },
        },
      },
    },
  },
  credits: {
    accounting: { unit: "credit", scale: 6, rounding: "half_up" },
    buckets: { default: { priority: 1, expiry: { type: "never" } } },
    default_bucket: "default",
  },
  plans: {
    captured_plan: {
      display_name: "Captured",
      rank: 1,
      rate_card: "captured",
      allowed_operations: ["completion"],
    },
  },
};

describe("PricingRuntime lease pricing", () => {
  it("uses the catalog and rate card captured at lease admission", async () => {
    const getLeasePricingContext = vi.fn().mockResolvedValue({
      catalogVersion: 7,
      planId: "plan-id",
      planKey: "captured_plan",
      rateCard: "captured",
    });
    const getBursarConfig = vi.fn().mockResolvedValue({
      id: "revision-7",
      version: 7,
      config: CONFIG,
    });
    const getUserPlan = vi.fn().mockResolvedValue({ rateCard: "current" });
    const store = {
      getLeasePricingContext,
      getBursarConfig,
      getUserPlan,
    } as unknown as CreditStore;
    const runtime = new PricingRuntime(store, null, noopLogger, 0);

    const result = await runtime.costOf(
      { operation: "completion", measures: { tokens: 3 } },
      "user-1",
      "lease-1",
    );

    expect(result.amount.toString()).toBe("6");
    expect(getLeasePricingContext).toHaveBeenCalledWith("user-1", "lease-1");
    expect(getBursarConfig).toHaveBeenCalledWith(7);
    expect(getUserPlan).not.toHaveBeenCalled();
  });

  it("rejects metric settlement when the subject-owned lease is missing", async () => {
    const store = {
      getLeasePricingContext: vi.fn().mockResolvedValue(null),
    } as unknown as CreditStore;
    const runtime = new PricingRuntime(store, null, noopLogger, 0);

    await expect(
      runtime.costOf(
        { operation: "completion", measures: { tokens: 1 } },
        "user-1",
        "lease-missing",
      ),
    ).rejects.toBeInstanceOf(LeaseNotFoundError);
  });
});
