import { Decimal } from "decimal.js";
import { describe, expect, it, vi } from "vitest";

import { makeCostBreakdown } from "../src/breakdown.js";
import { CreditEventEmitter } from "../src/credits/events.js";
import { raiseDeductError } from "../src/credits/service-errors.js";
import { CreditsService } from "../src/credits/service.js";
import type { CreditStore } from "../src/credits/store.js";
import type { PricingEngine } from "../src/engine.js";
import {
  CapReachedError,
  CapabilityNotSupportedError,
  StoreError,
  StoreUnavailableError,
  isRetryableBursarError,
} from "../src/errors.js";
import { retryBursarOperation } from "../src/retry.js";

describe("CreditsService mirror regressions", () => {
  it("only classifies transient store failures as retryable", () => {
    expect(isRetryableBursarError(new StoreUnavailableError("temporary"))).toBe(true);
    expect(isRetryableBursarError(new StoreError("unclassified"))).toBe(false);
    expect(isRetryableBursarError(new CapReachedError("permanent"))).toBe(false);
    expect(isRetryableBursarError(new CapabilityNotSupportedError("permanent"))).toBe(false);
  });

  it("maps invalid deduction requests to a caller error", () => {
    expect(() => raiseDeductError("invalid_request", "user-1", new Decimal(1))).toThrow(RangeError);
  });

  it("retries only transient Bursar failures", async () => {
    const transient = vi
      .fn<() => Promise<string>>()
      .mockRejectedValueOnce(new StoreUnavailableError("temporary"))
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
      reservedTotal: new Decimal(10),
      minimumBalance: new Decimal(0),
      billingMode: "strict",
      expiresAt: new Date(Date.now() + 60_000).toISOString(),
      error: null,
    });
    const settleLease = vi.fn().mockResolvedValue({
      entryId: "entry-1",
      usageChargeId: "usage-1",
      userId: "user-1",
      amount: new Decimal(8),
      allowanceConsumed: new Decimal(0),
      balanceAfter: new Decimal(92),
      idempotent: true,
      error: null,
      bucketBreakdown: null,
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

  it("records zero-cost usage through the authoritative charge RPC", async () => {
    const deductWithAllowance = vi.fn().mockResolvedValue({
      entryId: null,
      usageChargeId: "usage-free-1",
      userId: "user-1",
      amount: new Decimal(0),
      allowanceConsumed: new Decimal(0),
      balanceAfter: new Decimal(25),
      idempotent: false,
      error: null,
      bucketBreakdown: null,
    });
    const store = {
      getUserPlan: vi.fn().mockResolvedValue({
        userId: "user-1",
        catalogVersion: null,
        rateCard: null,
      }),
      deductWithAllowance,
      listQuotaEvents: vi.fn().mockResolvedValue([]),
    } as unknown as CreditStore;
    const engine = {
      calculate: vi.fn().mockReturnValue(makeCostBreakdown()),
    } as unknown as PricingEngine;
    const emitter = new CreditEventEmitter();
    const events: Array<Record<string, unknown> | undefined> = [];
    emitter.on("credits.deducted", (event) => {
      events.push(event.data);
    });

    const result = await new CreditsService(store, engine, emitter).deduct(
      "user-1",
      {
        operation: "free_operation",
        measures: {},
        dimensions: {},
      },
      { idempotencyKey: "free-operation-1" },
    );

    expect(deductWithAllowance).toHaveBeenCalledWith(
      "user-1",
      new Decimal(0),
      expect.objectContaining({ operation: "free_operation" }),
    );
    if (result.error !== null) throw new Error(result.error);
    expect(result.amount.eq(0)).toBe(true);
    expect(result.balanceAfter.eq(25)).toBe(true);
    expect(events).toEqual([expect.objectContaining({ amount: new Decimal(0) })]);
  });
});
