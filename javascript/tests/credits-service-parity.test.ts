import Decimal from "decimal.js";
import { describe, expect, it, vi } from "vitest";

import { makeCostBreakdown } from "../src/breakdown.js";
import { CreditEventEmitter } from "../src/credits/events.js";
import { CreditsService } from "../src/credits/service.js";
import type { CreditStore } from "../src/credits/store.js";
import type { PricingEngine } from "../src/engine.js";
import {
  CapReachedError,
  CapabilityNotSupportedError,
  StoreError,
  isRetryableBursarError,
} from "../src/errors.js";
import { retryBursarOperation } from "../src/retry.js";

describe("CreditsService mirror regressions", () => {
  it("only classifies transient store failures as retryable", () => {
    expect(isRetryableBursarError(new StoreError("temporary"))).toBe(true);
    expect(isRetryableBursarError(new CapReachedError("permanent"))).toBe(false);
    expect(isRetryableBursarError(new CapabilityNotSupportedError("permanent"))).toBe(false);
  });

  it("retries only transient Bursar failures", async () => {
    const transient = vi
      .fn<() => Promise<string>>()
      .mockRejectedValueOnce(new StoreError("temporary"))
      .mockResolvedValue("ok");
    await expect(retryBursarOperation(transient, { maxAttempts: 3, baseDelayMs: 0 })).resolves.toBe(
      "ok",
    );
    expect(transient).toHaveBeenCalledTimes(2);

    const permanent = vi
      .fn<() => Promise<string>>()
      .mockRejectedValue(new CapReachedError("permanent"));
    await expect(
      retryBursarOperation(permanent, { maxAttempts: 3, baseDelayMs: 0 }),
    ).rejects.toThrow(CapReachedError);
    expect(permanent).toHaveBeenCalledOnce();
  });

  it("uses one stable operation key for reservation and settlement", async () => {
    const createLease = vi.fn().mockResolvedValue({
      leaseId: "lease-1",
      userId: "user-1",
      amount: new Decimal(10),
      available: new Decimal(90),
      minimumBalance: new Decimal(0),
      billingMode: "strict",
      expiresAt: new Date(Date.now() + 60_000).toISOString(),
    });
    const settleLease = vi.fn().mockResolvedValue({
      entryId: "entry-1",
      userId: "user-1",
      amount: new Decimal(8),
      allowanceConsumed: new Decimal(0),
      balanceAfter: new Decimal(92),
      idempotent: true,
      capWarning: null,
      featureLimitWarning: null,
    });
    const store = {
      getUserPlan: vi.fn().mockResolvedValue({
        userId: "user-1",
        planId: null,
        billingMode: "strict",
      }),
      createLease,
      settleLease,
      listQuotaEvents: vi.fn().mockResolvedValue([]),
    } as unknown as CreditStore;
    const service = new CreditsService(store);

    const operation = await service.beginBilledOperation("user-1", {
      estimate: new Decimal(10),
      operationKey: "job:42",
      metadata: { referenceType: "job", referenceId: "42" },
    });
    await operation.settle(new Decimal(8));

    expect(createLease).toHaveBeenCalledWith(
      "user-1",
      new Decimal(10),
      "usage",
      expect.objectContaining({
        idempotencyKey: "job:42:reserve",
        metadata: { referenceType: "job", referenceId: "42" },
      }),
    );
    expect(settleLease).toHaveBeenCalledWith(
      "user-1",
      "lease-1",
      new Decimal(8),
      expect.objectContaining({
        idempotencyKey: "job:42:settle",
      }),
    );
  });

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
