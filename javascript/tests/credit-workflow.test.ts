import Decimal from "decimal.js";
import { describe, expect, it, vi } from "vitest";

import { CreditsService } from "../src/credits/service.js";
import type { CreditStore } from "../src/credits/store.js";
import type { PricingEngine } from "../src/engine.js";
import {
  ConfigError,
  LeaseExpiredError,
  LeaseNotFoundError,
  QuotaExceededError,
} from "../src/errors.js";

const leaseResult = (overrides: Record<string, unknown> = {}) => ({
  leaseId: "lease-1",
  userId: "user-1",
  amount: new Decimal(10),
  available: new Decimal(90),
  minimumBalance: new Decimal(0),
  billingMode: "strict",
  expiresAt: new Date(Date.now() + 60_000).toISOString(),
  ...overrides,
});

const deductionResult = (overrides: Record<string, unknown> = {}) => ({
  entryId: "entry-1",
  userId: "user-1",
  amount: new Decimal(8),
  allowanceConsumed: new Decimal(0),
  balanceAfter: new Decimal(92),
  idempotent: false,
  ...overrides,
});

function makeService(
  store: Record<string, unknown>,
  options?: ConstructorParameters<typeof CreditsService>[3],
) {
  return new CreditsService(store as unknown as CreditStore, null, null, options ?? null);
}

describe("credit lease workflow", () => {
  it("records child workflow usage in the shared journal without another debit", async () => {
    const recordUsage = vi.fn().mockResolvedValue({
      usageId: "usage-1",
      userId: "user-1",
      requested: new Decimal(12),
      idempotent: false,
      error: null,
    });
    const engine = { calculate: vi.fn().mockReturnValue({ total: new Decimal(12) }) };
    const service = new CreditsService(
      {
        getUserPlan: vi.fn().mockResolvedValue({ rateCard: "standard" }),
        recordUsage,
      } as unknown as CreditStore,
      engine as unknown as PricingEngine,
      null,
      null,
    );

    const result = await service.recordUsage(
      "user-1",
      {
        operation: "roadmap_gen",
        measures: { jobs: 0 },
        dimensions: { model: "linkup" },
      },
      "roadmap:usage:outline",
      {
        usageKind: "workflow_step",
        workflowKey: "roadmap-1",
      },
    );

    expect(result.usageId).toBe("usage-1");
    expect(recordUsage).toHaveBeenCalledWith(
      "user-1",
      "roadmap_gen",
      new Decimal(12),
      expect.objectContaining({
        idempotencyKey: "roadmap:usage:outline",
        metadata: expect.objectContaining({
          usageKind: "workflow_step",
          workflowKey: "roadmap-1",
          breakdownTotal: "12",
        }),
      }),
    );
  });

  it("derives overdraft policy from the preset when the user has no plan", async () => {
    const createLease = vi.fn().mockResolvedValue(leaseResult());
    const service = makeService(
      {
        getUserPlan: vi.fn().mockResolvedValue({
          userId: "user-1",
          planId: null,
          planKey: null,
          planLabel: null,
          allowance: null,
          entitlements: {},
          creditPolicy: null,
          admission: null,
        }),
        createLease,
        listQuotaEvents: vi.fn().mockResolvedValue([]),
      },
      { policy: "overdraft", overdraftFloor: new Decimal(-100) },
    );

    await service.reserve("user-1", new Decimal(10), { operationType: "usage" });

    expect(createLease).toHaveBeenCalledWith(
      "user-1",
      new Decimal(10),
      "usage",
      expect.objectContaining({ billingMode: "overdraft", floor: new Decimal(-100) }),
    );
  });

  it("honors credit-line limits and operation admission from the plan", async () => {
    const createLease = vi.fn().mockResolvedValue(leaseResult());
    const service = makeService({
      getUserPlan: vi.fn().mockResolvedValue({
        userId: "user-1",
        planId: "pro",
        planKey: "pro",
        planLabel: "Pro",
        allowance: null,
        entitlements: {},
        creditPolicy: { type: "credit_line", creditLimit: new Decimal(200) },
        admission: { maxInFlight: 5, operations: { completion: { maxInFlight: 3 } } },
      }),
      createLease,
      listQuotaEvents: vi.fn().mockResolvedValue([]),
    });

    await service.reserve("user-1", new Decimal(10), {
      operationType: "completion",
      ttl: 30,
      feature: "chat",
      metadata: { ref: "1" },
    });

    expect(createLease).toHaveBeenCalledWith(
      "user-1",
      new Decimal(10),
      "completion",
      expect.objectContaining({
        billingMode: "overdraft",
        floor: new Decimal(-200),
        maxConcurrent: 3,
        ttlSeconds: 30,
        feature: "chat",
        region: null,
      }),
    );
  });

  it("raises quota-exceeded on reserve and emits the failure event", async () => {
    const service = makeService({
      getUserPlan: vi.fn().mockResolvedValue({
        userId: "user-1",
        planId: null,
        planKey: null,
        planLabel: null,
        allowance: null,
        entitlements: {},
        creditPolicy: null,
        admission: null,
      }),
      createLease: vi.fn().mockResolvedValue({ error: "quota_exceeded" }),
      listQuotaEvents: vi.fn().mockResolvedValue([{ eventType: "blocked", quotaKey: "tokens" }]),
    });

    await expect(service.reserve("user-1", new Decimal(10))).rejects.toThrow(QuotaExceededError);
  });

  it("records usage metadata on settle with metrics", async () => {
    const config = {
      version: 1,
      pricing: {
        operations: {
          completion: {
            measures: { input_tokens: { unit: "token" } },
            dimensions: {
              model: { type: "string" },
              region: { type: "string" },
            },
          },
        },
        rate_cards: {
          standard: {
            operations: {
              completion: {
                rules: [
                  {
                    when: { model: { op: "prefix", value: "gpt-" } },
                    charge: { type: "per_unit", measure: "input_tokens", rate: "2.000000" },
                  },
                ],
                unmatched: { action: "reject" },
              },
            },
          },
        },
      },
      credits: {
        accounting: { unit: "credit", scale: 6, rounding: "half_up" },
        buckets: { purchased: { priority: 20 } },
        default_bucket: "purchased",
        policies: { prepaid: { type: "prepaid" } },
      },
    };
    const settleLease = vi.fn().mockResolvedValue(deductionResult({ idempotent: true }));
    const service = new CreditsService(
      {
        getLeasePricingContext: vi.fn().mockResolvedValue({
          catalogVersion: 1,
          rateCard: null,
          planKey: null,
        }),
        getBursarConfig: vi.fn().mockResolvedValue({ config }),
        settleLease,
        listQuotaEvents: vi.fn().mockResolvedValue([]),
      } as unknown as CreditStore,
      null,
      null,
      null,
    );

    await service.settle(
      "user-1",
      "lease-1",
      {
        operation: "completion",
        measures: { input_tokens: 2 },
        dimensions: { model: "gpt-fast", region: "us-east-1" },
      },
      {
        metadata: { referenceId: "42", dropMe: null },
        idempotencyKey: "settle:key",
      },
    );

    expect(settleLease).toHaveBeenCalledTimes(1);
    const args = settleLease.mock.calls[0] as [string, string, Decimal, Record<string, unknown>];
    expect(args[0]).toBe("user-1");
    expect(args[1]).toBe("lease-1");
    expect(args[2].eq(new Decimal(4))).toBe(true);
    expect(args[3]).toEqual(
      expect.objectContaining({
        idempotencyKey: "settle:key",
        region: "us-east-1",
        metadata: expect.objectContaining({
          operation: "completion",
          referenceId: "42",
          breakdownTotal: "4",
          idempotencyKey: "settle:key",
        }),
      }),
    );
  });

  it("emits lease-expired on settle of an expired lease", async () => {
    const service = makeService({
      getUserPlan: vi.fn().mockResolvedValue({
        userId: "user-1",
        planId: null,
        planKey: null,
        planLabel: null,
        allowance: null,
        entitlements: {},
        creditPolicy: null,
        admission: null,
      }),
      settleLease: vi.fn().mockResolvedValue({ error: "expired_lease" }),
      listQuotaEvents: vi.fn().mockResolvedValue([]),
    });

    await expect(service.settle("user-1", "lease-1", new Decimal(5))).rejects.toThrow(
      LeaseExpiredError,
    );
  });

  it("releases and renews leased reservations", async () => {
    const renewLease = vi.fn().mockResolvedValue(leaseResult());
    const releaseLease = vi.fn().mockResolvedValue({ released: true, reason: "aborted" });
    const service = makeService({
      getUserPlan: vi.fn().mockResolvedValue({
        userId: "user-1",
        planId: null,
        planKey: null,
        planLabel: null,
        allowance: null,
        entitlements: {},
        creditPolicy: null,
        admission: null,
      }),
      createLease: vi.fn().mockResolvedValue(leaseResult()),
      renewLease,
      releaseLease,
      listQuotaEvents: vi.fn().mockResolvedValue([]),
    });

    const operation = await service.beginBilledOperation("user-1", {
      estimate: new Decimal(10),
      operationKey: "job:43",
    });
    await operation.renew(45);
    await operation.release();

    expect(renewLease).toHaveBeenCalledWith("user-1", "lease-1", 45);
    expect(releaseLease).toHaveBeenCalledWith("user-1", "lease-1");
  });

  it("rejects empty operation keys", async () => {
    const service = makeService({
      getUserPlan: vi.fn().mockResolvedValue({}),
      createLease: vi.fn(),
    });
    await expect(
      service.beginBilledOperation("user-1", {
        estimate: new Decimal(1),
        operationKey: "  ",
      }),
    ).rejects.toThrow(ConfigError);
  });

  it("raises on renew of a finalized lease", async () => {
    const service = makeService({
      renewLease: vi.fn().mockResolvedValue({ error: "released_lease" }),
    });
    await expect(service.renew("user-1", "lease-1", 60)).rejects.toThrow(LeaseNotFoundError);
  });

  it("reports affordability against available balance", async () => {
    const service = makeService({
      getUserPlan: vi.fn().mockResolvedValue({
        userId: "user-1",
        planId: null,
        planKey: null,
        planLabel: null,
        allowance: null,
        entitlements: {},
        creditPolicy: null,
        admission: null,
      }),
      getAvailable: vi.fn().mockResolvedValue({
        userId: "user-1",
        balance: new Decimal(100),
        reserved: new Decimal(0),
        available: new Decimal(100),
      }),
      checkAllowance: vi.fn().mockResolvedValue({
        planId: "none",
        allowanceRemaining: new Decimal(0),
        periodStart: "",
        periodEnd: "",
      }),
    });

    await expect(service.canAfford("user-1", new Decimal(25))).resolves.toMatchObject({
      affordable: true,
      spendable: new Decimal(100),
    });
    await expect(service.canAfford("user-1", new Decimal(500))).resolves.toMatchObject({
      affordable: false,
      reason: "insufficient_credits",
    });
  });

  it("accounts for entitlement checks and allowance in affordability", async () => {
    const service = makeService({
      getUserPlan: vi.fn().mockResolvedValue({
        userId: "user-1",
        planId: null,
        planKey: null,
        planLabel: null,
        allowance: null,
        entitlements: {},
        creditPolicy: null,
        admission: null,
      }),
      getAvailable: vi.fn().mockResolvedValue({
        userId: "user-1",
        balance: new Decimal(100),
        reserved: new Decimal(0),
        available: new Decimal(100),
      }),
      checkAllowance: vi.fn().mockResolvedValue({
        planId: "none",
        allowanceRemaining: new Decimal(20),
        periodStart: "",
        periodEnd: "",
      }),
      checkFeature: vi.fn().mockResolvedValue({
        userId: "user-1",
        feature: "tutor_chat",
        value: false,
        hasFeature: false,
      }),
    });

    await expect(
      service.canAfford("user-1", new Decimal(25), { feature: "tutor_chat" }),
    ).resolves.toMatchObject({ affordable: false, reason: "feature_not_entitled" });
  });

  it("falls back to unavailable policy when the plan lookup fails", async () => {
    const service = makeService({
      getUserPlan: vi.fn().mockRejectedValue(new Error("plan service down")),
      getAvailable: vi.fn().mockResolvedValue({
        userId: "user-1",
        balance: new Decimal(100),
        reserved: new Decimal(0),
        available: new Decimal(100),
      }),
    });

    await expect(service.canAfford("user-1", new Decimal(25))).resolves.toMatchObject({
      affordable: false,
      reason: "policy_unavailable",
    });
  });

  it("continues when the allowance check fails", async () => {
    const service = makeService({
      getUserPlan: vi.fn().mockResolvedValue({
        userId: "user-1",
        planId: null,
        planKey: null,
        planLabel: null,
        allowance: null,
        entitlements: {},
        creditPolicy: null,
        admission: null,
      }),
      getAvailable: vi.fn().mockResolvedValue({
        userId: "user-1",
        balance: new Decimal(100),
        reserved: new Decimal(0),
        available: new Decimal(100),
      }),
      checkAllowance: vi.fn().mockRejectedValue(new Error("allowance query failed")),
    });

    await expect(service.canAfford("user-1", new Decimal(25))).resolves.toMatchObject({
      affordable: true,
    });
  });

  it("passes allowance checks through to the store", async () => {
    const checkAllowance = vi.fn().mockResolvedValue({
      planId: "pro",
      allowanceRemaining: new Decimal(10),
      periodStart: "2026-07-01",
      periodEnd: "2026-08-01",
    });
    const service = makeService({ checkAllowance });
    await expect(service.checkAllowance("user-1")).resolves.toMatchObject({
      allowanceRemaining: new Decimal(10),
    });
  });

  it("releases the lease and rethrows when work fails", async () => {
    const releaseLease = vi.fn().mockResolvedValue({ released: true, reason: "work_failed" });
    const service = makeService({
      getUserPlan: vi.fn().mockResolvedValue({
        userId: "user-1",
        planId: null,
        planKey: null,
        planLabel: null,
        allowance: null,
        entitlements: {},
        creditPolicy: null,
        admission: null,
      }),
      createLease: vi.fn().mockResolvedValue(leaseResult()),
      releaseLease,
      settleLease: vi.fn(),
      listQuotaEvents: vi.fn().mockResolvedValue([]),
    });

    await expect(
      service.runBilled("user-1", {
        estimate: new Decimal(10),
        operationKey: "job:44",
        doWork: vi.fn().mockRejectedValue(new Error("worker crashed")),
      }),
    ).rejects.toThrow("worker crashed");
    expect(releaseLease).toHaveBeenCalledWith("user-1", "lease-1");
  });

  it("settles after successful work", async () => {
    const settleLease = vi.fn().mockResolvedValue(deductionResult());
    const service = makeService({
      getUserPlan: vi.fn().mockResolvedValue({
        userId: "user-1",
        planId: null,
        planKey: null,
        planLabel: null,
        allowance: null,
        entitlements: {},
        creditPolicy: null,
        admission: null,
      }),
      createLease: vi.fn().mockResolvedValue(leaseResult()),
      settleLease,
      releaseLease: vi.fn(),
      listQuotaEvents: vi.fn().mockResolvedValue([]),
    });

    const outcome = await service.runBilled("user-1", {
      estimate: new Decimal(10),
      operationKey: "job:45",
      settlementAttempts: 2,
      doWork: vi.fn().mockResolvedValue({ result: "done", actual: new Decimal(8) }),
    });

    expect(outcome.result).toBe("done");
    expect(settleLease).toHaveBeenCalledWith(
      "user-1",
      "lease-1",
      new Decimal(8),
      expect.objectContaining({ idempotencyKey: "job:45:settle" }),
    );
  });
});
