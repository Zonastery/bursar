import { describe, expect, it, vi } from "vitest";
import { LeaseNotFoundError } from "../src/errors.js";
import { CatalogRuntime } from "../src/credits/catalog-runtime.js";
import { PricingEngine } from "../src/engine.js";
import type { CreditStore } from "../src/credits/store.js";
import { noopLogger } from "../src/shared/logger.js";
import type { BursarConfigData } from "../src/config.js";

const CONFIG = {
  version: 1,
  catalog: { default_plan: "captured_plan" },
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
} satisfies BursarConfigData;

function testStore(value: Partial<CreditStore>): CreditStore {
  // SAFETY: Each fixture implements the CreditStore methods exercised by its scenario.
  return value as CreditStore;
}

describe("CatalogRuntime lease pricing", () => {
  it("installs a published catalog only after persistence succeeds", async () => {
    const original = PricingEngine.fromDict(CONFIG);
    const publishAndActivateCatalog = vi.fn().mockResolvedValue("revision-2");
    const store = testStore({ publishAndActivateCatalog });
    const runtime = new CatalogRuntime(store, original, noopLogger, 0);

    await expect(runtime.publishAndActivate(CONFIG, "release-2")).resolves.toBe("revision-2");

    expect(runtime.currentEngine).not.toBe(original);
    expect(publishAndActivateCatalog).toHaveBeenCalledOnce();
  });

  it("keeps the committed catalog when publication fails", async () => {
    const original = PricingEngine.fromDict(CONFIG);
    const store = testStore({
      publishAndActivateCatalog: vi.fn().mockRejectedValue(new Error("write failed")),
    });
    const runtime = new CatalogRuntime(store, original, noopLogger, 0);

    await expect(runtime.publishAndActivate(CONFIG)).rejects.toThrow("write failed");

    expect(runtime.currentEngine).toBe(original);
  });

  it("blocks stale callers until the shared catalog refresh completes", async () => {
    const response = Promise.withResolvers<{
      id: string;
      version: number;
      config: typeof CONFIG;
    }>();
    const getActiveCatalog = vi.fn(() => response.promise);
    const store = testStore({ getActiveCatalog });
    const runtime = new CatalogRuntime(store, PricingEngine.fromDict(CONFIG), noopLogger, 1);
    await new Promise((resolve) => setTimeout(resolve, 5));

    let settled = false;
    const first = runtime.refreshIfStale().then(() => {
      settled = true;
    });
    const second = runtime.refreshIfStale();
    await Promise.resolve();

    expect(settled).toBe(false);
    expect(getActiveCatalog).toHaveBeenCalledOnce();
    response.resolve({ id: "revision-2", version: 2, config: CONFIG });
    await Promise.all([first, second]);
  });

  it("uses the catalog and rate card captured at lease admission", async () => {
    const getLeasePricingContext = vi.fn().mockResolvedValue({
      catalogVersion: 7,
      planId: "plan-id",
      planKey: "captured_plan",
      rateCard: "captured",
    });
    const getCatalogRevision = vi.fn().mockResolvedValue({
      id: "revision-7",
      version: 7,
      config: CONFIG,
    });
    const getUserPlan = vi.fn().mockResolvedValue({ rateCard: "current" });
    const store = testStore({
      getLeasePricingContext,
      getCatalogRevision,
      getUserPlan,
    });
    const runtime = new CatalogRuntime(store, null, noopLogger, 0);

    const result = await runtime.costOf(
      { operation: "completion", measures: { tokens: 3 } },
      "user-1",
      "lease-1",
    );

    expect(result.amount.toString()).toBe("6");
    expect(getLeasePricingContext).toHaveBeenCalledWith("user-1", "lease-1");
    expect(getCatalogRevision).toHaveBeenCalledWith(7);
    expect(getUserPlan).not.toHaveBeenCalled();
  });

  it("rejects metric settlement when the subject-owned lease is missing", async () => {
    const store = testStore({
      getLeasePricingContext: vi.fn().mockResolvedValue(null),
    });
    const runtime = new CatalogRuntime(store, null, noopLogger, 0);

    await expect(
      runtime.costOf(
        { operation: "completion", measures: { tokens: 1 } },
        "user-1",
        "lease-missing",
      ),
    ).rejects.toBeInstanceOf(LeaseNotFoundError);
  });
});
