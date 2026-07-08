import { describe, it, expect, beforeEach } from "vitest";
import { CreditStore } from "../src/stores/credit-store.js";
import { MemoryStore } from "../src/stores/memory-store.js";
import type { PricingConfigData, PricingConfigResult } from "../src/types.js";

// ── Minimal store that counts loads ────────────────────────────────────

let loadCount = 0;
let fakeResult: PricingConfigResult | null = null;

class FakeStore extends CreditStore {
  constructor(pricingCacheTtl: number = 300) {
    super(pricingCacheTtl);
  }

  async getActivePricing(): Promise<PricingConfigResult | null> {
    return this._getCachedPricing(async () => {
      loadCount++;
      return fakeResult;
    });
  }

  setResult(r: PricingConfigResult | null): void {
    fakeResult = r;
    this.invalidatePricingCache();
  }

  // Stubs
  async setup(): Promise<any> {
    return {};
  }
  async teardown(): Promise<void> {}
  async getBalance(): Promise<never> {
    throw new Error("stub");
  }
  async addCredits(): Promise<never> {
    throw new Error("stub");
  }
  async deductWithAllowance(): Promise<never> {
    throw new Error("stub");
  }
  async deduct(): Promise<never> {
    throw new Error("stub");
  }
  async createLease(): Promise<never> {
    throw new Error("stub");
  }
  async settleLease(): Promise<never> {
    throw new Error("stub");
  }
  async releaseLease(): Promise<never> {
    throw new Error("stub");
  }
  async renewLease(): Promise<never> {
    throw new Error("stub");
  }
  async getAvailable(): Promise<never> {
    throw new Error("stub");
  }
  async getCreditTiers(): Promise<never> {
    throw new Error("stub");
  }
  async setUserPlan(): Promise<never> {
    throw new Error("stub");
  }
  async unsetUserPlan(): Promise<never> {
    throw new Error("stub");
  }
  async getUserPlan(): Promise<never> {
    throw new Error("stub");
  }
  async checkFeature(): Promise<never> {
    throw new Error("stub");
  }
  async checkAllowance(): Promise<never> {
    throw new Error("stub");
  }
  async checkSpendCap(): Promise<never> {
    throw new Error("stub");
  }
  async checkFeatureLimit(): Promise<never> {
    throw new Error("stub");
  }
  async refundCredits(): Promise<never> {
    throw new Error("stub");
  }
  async sweepExpiredCredits(): Promise<never> {
    throw new Error("stub");
  }
  async setActivePricing(): Promise<string> {
    throw new Error("stub");
  }
  async activatePricing(): Promise<string> {
    throw new Error("stub");
  }
  async getPricingHistory(): Promise<never> {
    throw new Error("stub");
  }
  async getPricingConfig(): Promise<never> {
    throw new Error("stub");
  }
  async addPlanDefinition(): Promise<never> {
    throw new Error("stub");
  }
  async getPlanDefinitions(): Promise<never> {
    throw new Error("stub");
  }
  async getPlanDefinition(): Promise<never> {
    throw new Error("stub");
  }
  async removePlanDefinition(): Promise<never> {
    throw new Error("stub");
  }
  async incrementUsageWindow(): Promise<never> {
    throw new Error("stub");
  }
  async listUserTransactions(): Promise<never> {
    throw new Error("stub");
  }
  async listUsageEvents(): Promise<never> {
    throw new Error("stub");
  }
  async getAggregateStats(): Promise<never> {
    throw new Error("stub");
  }
  async getTopUsers(): Promise<never> {
    throw new Error("stub");
  }
  async getDailySpend(): Promise<never> {
    throw new Error("stub");
  }
}

function makeResult(ver: number = 1): PricingConfigResult {
  return {
    id: ver === 1 ? "cfg-a" : "cfg-b",
    config: {
      version: 1,
      models: { _default: "input_tokens * 1" },
    } as unknown as PricingConfigData,
    version: ver,
  };
}

// ── Tests ──────────────────────────────────────────────────────────────

describe("Pricing cache", () => {
  beforeEach(() => {
    loadCount = 0;
    fakeResult = null;
  });

  it("returns cached instance within TTL", async () => {
    const store = new FakeStore(300);
    store.setResult(makeResult(1));

    const r1 = await store.getActivePricing();
    const r2 = await store.getActivePricing();

    expect(r1).toBe(r2);
    expect(loadCount).toBe(1);
  });

  it("misses after invalidation", async () => {
    const store = new FakeStore(300);
    store.setResult(makeResult(1));

    const r1 = await store.getActivePricing();
    expect(r1?.version).toBe(1);

    store.setResult(makeResult(2));

    const r2 = await store.getActivePricing();
    expect(r2?.version).toBe(2);
    expect(loadCount).toBe(2);
  });

  it("TTL=0 disables caching", async () => {
    const store = new FakeStore(0);
    store.setResult(makeResult(1));

    await store.getActivePricing();
    await store.getActivePricing();

    expect(loadCount).toBe(2);
  });

  it("invalidatePricingCache forces reload", async () => {
    const store = new FakeStore(300);
    store.setResult(makeResult(1));

    await store.getActivePricing(); // warm
    store.invalidatePricingCache();
    await store.getActivePricing();

    expect(loadCount).toBe(2);
  });

  it("respects TTL expiry", async () => {
    const store = new FakeStore(1);
    store.setResult(makeResult(1));

    await store.getActivePricing(); // miss
    await store.getActivePricing(); // hit
    expect(loadCount).toBe(1);

    await new Promise((r) => setTimeout(r, 1500));

    await store.getActivePricing(); // expired
    expect(loadCount).toBe(2);
  });

  it("does not cache null results", async () => {
    const store = new FakeStore(300);
    fakeResult = null;
    store.invalidatePricingCache();

    const r1 = await store.getActivePricing();
    const r2 = await store.getActivePricing();

    expect(r1).toBeNull();
    expect(r2).toBeNull();
    expect(loadCount).toBe(2);
  });

  it("MemoryStore constructor accepts pricingCacheTtl", () => {
    const store = new MemoryStore(600);
    expect(store).toBeInstanceOf(MemoryStore);
  });
});
