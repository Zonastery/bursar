import Decimal from "decimal.js";
import { describe, expect, it, vi } from "vitest";

import { makeCostBreakdown } from "../src/breakdown.js";
import { CreditEventEmitter } from "../src/credits/events.js";
import { CreditsService } from "../src/credits/service.js";
import type { CreditStore } from "../src/credits/store.js";
import type { PricingEngine } from "../src/engine.js";

describe("CreditsService mirror regressions", () => {
  it("short-circuits a zero-cost deduction without calling the charge RPC", async () => {
    const deductWithAllowance = vi.fn();
    const store = {
      getUserPlan: vi.fn().mockResolvedValue({
        userId: "user-1",
        configVersion: null,
        catalogVersion: null,
        rateCard: null,
      }),
      getBalance: vi.fn().mockResolvedValue({
        userId: "user-1",
        balance: new Decimal(25),
        lifetimePurchased: new Decimal(25),
      }),
      deductWithAllowance,
    } as unknown as CreditStore;
    const engine = {
      minBalance: new Decimal(0),
      calculate: vi.fn().mockReturnValue(makeCostBreakdown()),
    } as unknown as PricingEngine;
    const emitter = new CreditEventEmitter();
    const events: Array<Record<string, unknown> | undefined> = [];
    emitter.on("credits.deducted", (event) => events.push(event.data));

    const result = await new CreditsService(store, engine, emitter).deduct("user-1", {
      operation: "free_operation",
      measures: {},
      dimensions: {},
    });

    expect(deductWithAllowance).not.toHaveBeenCalled();
    expect(result.amount.eq(0)).toBe(true);
    expect(result.balanceAfter.eq(25)).toBe(true);
    expect(events).toEqual([
      expect.objectContaining({
        planCovered: true,
      }),
    ]);
  });
});
