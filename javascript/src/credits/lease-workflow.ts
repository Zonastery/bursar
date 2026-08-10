import { Decimal } from "decimal.js";
import { retryBursarOperation } from "../retry.js";
import type { NormalizedLogger } from "../shared/logger.js";
import type { CreditEventType } from "./events.js";
import { LowBalanceMonitor } from "./low-balance-monitor.js";
import { CatalogRuntime } from "./catalog-runtime.js";
import { isAmount, rejectNativeCreditAmount } from "./amount.js";
import { raiseLeaseError } from "./service-errors.js";
import { requireStableKey, scopedStableKey } from "../shared/idempotency.js";
import type {
  CanAffordOptions,
  BeginBilledOperationOptions,
  BilledOperation,
  MetricsOrAmount,
  PolicyPreset,
  ReserveOptions,
  RunBilledOptions,
  SettleOptions,
} from "./service-types.js";
import type { CreditStore } from "./store.js";
import type {
  AllowanceResult,
  BillingMode,
  BucketBalancesResult,
  CanAffordResult,
  CreditMetadata,
  DeductionResult,
  DeductionSuccess,
  LeaseSuccess,
  ReleaseResult,
} from "./types/index.js";

export class CreditLeaseWorkflow {
  constructor(
    private readonly store: CreditStore,
    private readonly catalogRuntime: CatalogRuntime,
    private readonly logger: NormalizedLogger,
    private readonly balanceMonitor: LowBalanceMonitor,
    private readonly policy: PolicyPreset,
    private readonly overdraftFloor: Decimal | null,
    private readonly defaultMaxConcurrent: number | null,
    private readonly defaultTtl: number,
    private readonly emit: (
      type: CreditEventType,
      userId: string,
      data?: Record<string, unknown>,
    ) => void,
    private readonly emitQuotaEvents: (userId: string, idempotencyKey: string) => Promise<void>,
    private readonly maybeLazyExpire: (userId: string) => Promise<void>,
    private readonly afterDeduction: (userId: string, result: DeductionSuccess) => Promise<void>,
  ) {}

  private async expectedAdmissionPolicy(
    userId: string,
    operationType: string,
    billingModeOverride?: BillingMode | null,
  ): Promise<{
    billingMode: BillingMode;
    floor: Decimal;
    maxConcurrent: number | null;
  }> {
    const plan = await this.store.getUserPlan(userId);
    const creditPolicy = plan.planId == null ? null : plan.creditPolicy;
    const planMode: BillingMode =
      plan.planId == null
        ? this.policy === "overdraft"
          ? "overdraft"
          : "strict"
        : creditPolicy?.type === "credit_line"
          ? "overdraft"
          : "strict";
    const operationAdmission =
      plan.planId == null ? null : (plan.admission?.operations[operationType] ?? null);
    const billingMode = billingModeOverride ?? planMode;
    const floor =
      billingMode === "overdraft"
        ? (creditPolicy?.creditLimit?.negated() ?? this.overdraftFloor ?? new Decimal(0))
        : new Decimal(0);
    return {
      billingMode,
      floor,
      maxConcurrent:
        plan.planId == null
          ? this.defaultMaxConcurrent
          : (operationAdmission?.maxInFlight ?? plan.admission?.maxInFlight ?? null),
    };
  }

  /**
   * Compute the credit cost and model from metrics, or pass a raw amount through.
   *
   * For {@link UsageMetrics} the cost is ``engine.calculate(...).total`` (exact
   * `Decimal`, no truncation); a raw amount is used as-is with no model.
   */
  private async costOf(
    metricsOrAmount: MetricsOrAmount,
    userId?: string | null,
    leaseId?: string | null,
  ): Promise<{ amount: Decimal; model: string | null }> {
    return this.catalogRuntime.costOf(metricsOrAmount, userId, leaseId);
  }

  /**
   * Atomically acquire a lease — the only authoritative admission control.
   *
   * Prices the estimate and delegates entitlement, quota, allowance, credit
   * policy, and concurrency enforcement to the database in one atomic call.
   */
  async reserve(
    userId: string,
    metricsOrAmount: MetricsOrAmount,
    options: ReserveOptions,
  ): Promise<LeaseSuccess> {
    rejectNativeCreditAmount(metricsOrAmount);
    const leaseIdempotencyKey = requireStableKey(options?.idempotencyKey);
    await this.maybeLazyExpire(userId);
    this.logger.debug("[CreditsService] reserve", {
      feature: options.feature,
      operationType: options.operationType,
    });
    const operationType =
      options.operationType ?? (isAmount(metricsOrAmount) ? "usage" : metricsOrAmount.operation);
    const expectedPolicy = await this.expectedAdmissionPolicy(
      userId,
      operationType,
      options.billingMode,
    );
    const priced = await this.costOf(metricsOrAmount, userId);
    const amount = priced.amount;
    const model = priced.model ?? options.model ?? null;
    const ttlSeconds = options.ttl != null ? options.ttl : this.defaultTtl;
    const feature = options.feature ?? null;
    const measures = isAmount(metricsOrAmount) ? {} : { ...(metricsOrAmount.measures ?? {}) };
    const dimensions = isAmount(metricsOrAmount) ? {} : { ...(metricsOrAmount.dimensions ?? {}) };
    const region = typeof dimensions.region === "string" ? dimensions.region : null;

    const result = await this.store.createLease(userId, amount, operationType, {
      idempotencyKey: leaseIdempotencyKey,
      billingMode: expectedPolicy.billingMode,
      floor: expectedPolicy.floor,
      maxConcurrent: expectedPolicy.maxConcurrent,
      ttlSeconds,
      model,
      metadata: options.metadata,
      feature,
      region,
      measures,
      dimensions,
    });

    if (result.error !== null) {
      if (result.error === "quota_exceeded") {
        await this.emitQuotaEvents(userId, leaseIdempotencyKey);
      }
      this.emit("credits.deduct_failed", userId, {
        error: result.error,
        amount,
        stage: "reserve",
        operationType,
      });
      raiseLeaseError(result.error, userId, amount);
    }

    this.logger.info("[CreditsService] reservation acquired", {
      leaseId: result.leaseId,
      amount: result.amount,
      minimumBalance: result.minimumBalance,
    });
    this.emit("credits.reserved", userId, {
      leaseId: result.leaseId,
      amount: result.amount,
      available: result.available,
      minimumBalance: result.minimumBalance,
      operationType,
      expiresAt: result.expiresAt,
    });
    await this.emitQuotaEvents(userId, leaseIdempotencyKey);
    return result;
  }

  /**
   * Charge the actual cost against a lease and finalize it.
   *
   * De-clamped: bills the full actual cost even if it exceeds the lease hold
   * (overdraft). Emits ``credits.deducted``, then low-balance and overdraft
   * signals as applicable.
   */
  async settle(
    userId: string,
    leaseId: string,
    metricsOrAmount: MetricsOrAmount,
    options?: SettleOptions,
  ): Promise<DeductionResult> {
    rejectNativeCreditAmount(metricsOrAmount);
    await this.maybeLazyExpire(userId);
    this.logger.debug("[CreditsService] settle", { leaseId });
    const idempotencyKey =
      options?.idempotencyKey === undefined
        ? `lease:${leaseId}:settle`
        : requireStableKey(options.idempotencyKey);
    const feature = options?.feature ?? null;
    const { amount, model } = await this.costOf(metricsOrAmount, userId, leaseId);
    const measures = isAmount(metricsOrAmount) ? {} : { ...(metricsOrAmount.measures ?? {}) };
    const dimensions = isAmount(metricsOrAmount) ? {} : { ...(metricsOrAmount.dimensions ?? {}) };
    const region = typeof dimensions.region === "string" ? dimensions.region : null;

    // Build ledger metadata with caller fields first and protected system fields last.
    const txMeta: Record<string, unknown> = {};
    if (isAmount(metricsOrAmount)) {
      if (options?.metadata) {
        for (const [k, v] of Object.entries(options.metadata)) {
          if (v != null) txMeta[k] = v;
        }
      }
      if (idempotencyKey) txMeta["idempotencyKey"] = idempotencyKey;
    } else {
      if (options?.metadata) {
        for (const [k, v] of Object.entries(options.metadata)) {
          if (v != null) txMeta[k] = v;
        }
      }
      txMeta["operation"] = metricsOrAmount.operation;
      txMeta["measures"] = { ...(metricsOrAmount.measures ?? {}) };
      txMeta["dimensions"] = { ...(metricsOrAmount.dimensions ?? {}) };
      txMeta["breakdownTotal"] = amount.toString();
      if (idempotencyKey) txMeta["idempotencyKey"] = idempotencyKey;
    }

    const result = await this.store.settleLease(userId, leaseId, amount, {
      idempotencyKey,
      feature,
      model,
      region,
      measures,
      dimensions,
      metadata: txMeta as CreditMetadata,
    });

    if (result.error !== null) {
      this.emit("credits.deduct_failed", userId, {
        error: result.error,
        amount,
        stage: "settle",
        leaseId,
      });
      if (result.error === "expired_lease") {
        this.emit("credits.lease_expired", userId, { leaseId });
      }
      raiseLeaseError(result.error, userId, amount);
    }

    this.logger.info("[CreditsService] settled", {
      leaseId,
      amount: result.amount,
      balanceAfter: result.balanceAfter,
      idempotent: result.idempotent,
    });
    this.emit("credits.deducted", userId, {
      entryId: result.entryId,
      amount: result.amount,
      allowanceConsumed: result.allowanceConsumed,
      balanceAfter: result.balanceAfter,
      model,
      leaseId,
      idempotent: result.idempotent,
    });

    await this.balanceMonitor.afterCharge(userId, result);
    if (!result.idempotent) {
      await this.emitQuotaEvents(userId, idempotencyKey);
      await this.afterDeduction(userId, result);
    }
    return result;
  }

  /** Release a lease without charging; safe to repeat after failed or aborted work. */
  async release(userId: string, leaseId: string): Promise<ReleaseResult> {
    this.logger.debug("[CreditsService] release", { leaseId });
    const result = await this.store.releaseLease(userId, leaseId);
    if (result.released) {
      this.logger.info("[CreditsService] lease released", { leaseId, reason: result.reason });
      this.emit("credits.reservation_released", userId, {
        leaseId,
        reason: result.reason,
      });
    }
    return result;
  }

  /** Extend an active lease's expiry without changing its captured policy. */
  async renew(userId: string, leaseId: string, ttl?: number | null): Promise<LeaseSuccess> {
    const ttlSeconds = ttl ?? this.defaultTtl;
    this.logger.debug("[CreditsService] renew", { leaseId, ttlSeconds });
    const result = await this.store.renewLease(userId, leaseId, ttlSeconds);
    if (result.error !== null) {
      if (result.error === "expired_lease") {
        this.emit("credits.lease_expired", userId, { leaseId });
      }
      raiseLeaseError(result.error, userId, new Decimal(0));
    }
    return result;
  }

  /**
   * Advisory affordability check — UI only, non-locking, and potentially stale.
   *
   * Never use this as an admission gate; only ``reserve`` is authoritative.
   */
  async canAfford(
    userId: string,
    metricsOrAmount: MetricsOrAmount,
    options?: CanAffordOptions,
  ): Promise<CanAffordResult> {
    rejectNativeCreditAmount(metricsOrAmount);
    await this.maybeLazyExpire(userId);
    const feature = options?.feature ?? null;
    const { amount: worstCase } = await this.costOf(metricsOrAmount, userId);
    const avail = await this.store.getAvailable(userId);
    let expectedPolicy;
    try {
      expectedPolicy = await this.expectedAdmissionPolicy(
        userId,
        options?.operationType ?? "usage",
        options?.billingMode,
      );
    } catch {
      return {
        affordable: false,
        spendable: avail.available,
        worstCase,
        reason: "policy_unavailable",
      };
    }
    let allowanceRemaining = new Decimal(0);
    try {
      allowanceRemaining =
        (await this.store.checkAllowance(userId))?.allowanceRemaining ?? new Decimal(0);
    } catch (error) {
      this.logger.debug("[CreditsService] allowance fetch failed in canAfford", {
        error: error instanceof Error ? error.message : String(error),
      });
    }
    const spendable = avail.available.plus(allowanceRemaining).minus(expectedPolicy.floor);

    let affordable = true;
    let reason: string | null = null;
    if (feature != null) {
      const check = await this.store.checkFeature(userId, feature);
      if (!check.hasFeature) {
        affordable = false;
        reason = "feature_not_entitled";
      }
    }
    if (affordable && spendable.lt(worstCase)) {
      affordable = false;
      reason = "insufficient_credits";
    }

    return { affordable, spendable, worstCase, reason };
  }

  /** Get a user's per-bucket credit balances (credit buckets). Thin pass-through, no event emission. */
  async getBucketBalances(userId: string): Promise<BucketBalancesResult> {
    await this.maybeLazyExpire(userId);
    return await this.store.getBucketBalances(userId);
  }

  /**
   * Get remaining free allowance for the current billing period.
   *
   * Convenience wrapper that routes through the manager so callers never need
   * to reach past it into the raw store. Returns `null` when the subject has no
   * active allowance policy.
   *
   * Window calculation is database-owned and supports the full normalized
   * policy tuple, including timezone and arbitrary interval counts.
   */
  async checkAllowance(userId: string): Promise<AllowanceResult | null> {
    return await this.store.checkAllowance(userId);
  }

  /**
   * One-call shortcut wiring reserve → doWork → settle.
   *
   * ``doWork`` runs the operation and returns ``{ result, actual }`` where
   * ``actual`` is the real usage metrics (or amount) to settle. On any exception
   * from ``doWork`` the lease is released and the error re-raised. For long jobs
   * ``doWork`` may call the configured lease TTL. A crash between reserve and settle is
   * covered by the lease TTL (and the store's reaper).
   */
  async runBilled<T>(
    userId: string,
    options: RunBilledOptions<T>,
  ): Promise<{ result: T; deduction: DeductionResult }> {
    const operationKey = requireStableKey(options.operationKey, "operationKey");
    const operation = await this.beginBilledOperation(userId, {
      estimate: options.estimate,
      operationKey,
      operationType: options.operationType,
      billingMode: options.billingMode,
      ttl: options.ttl,
      feature: options.feature,
      metadata: options.metadata,
    });

    let workResult: T;
    let actual: MetricsOrAmount;
    try {
      ({ result: workResult, actual } = await options.doWork());
    } catch (err) {
      await operation.release();
      throw err;
    }

    // Never release after work succeeds. A settlement failure may be a
    // transient/unknown-commit outcome and is safe to replay with operationKey.
    const deduction = await retryBursarOperation(() => operation.settle(actual), {
      maxAttempts: options.settlementAttempts,
    });
    return { result: workResult, deduction };
  }

  /**
   * Begin a replay-safe billable operation that can span framework callbacks.
   *
   * The operation key is namespaced into distinct reserve and settle keys, so
   * replaying the whole lifecycle cannot acquire a second hold.
   */
  async beginBilledOperation(
    userId: string,
    options: BeginBilledOperationOptions,
  ): Promise<BilledOperation> {
    const operationKey = requireStableKey(options.operationKey, "operationKey");
    const lease = await this.reserve(userId, options.estimate, {
      operationType: options.operationType,
      billingMode: options.billingMode,
      ttl: options.ttl,
      feature: options.feature,
      metadata: options.metadata,
      idempotencyKey: scopedStableKey(operationKey, "reserve"),
    });
    return {
      leaseId: lease.leaseId,
      operationKey,
      settle: async (actual, metadata) =>
        this.settle(userId, lease.leaseId, actual, {
          idempotencyKey: scopedStableKey(operationKey, "settle"),
          feature: options.feature,
          metadata: metadata ?? options.metadata,
        }),
      renew: async (ttl) => {
        await this.renew(userId, lease.leaseId, ttl);
      },
      release: async () => {
        await this.release(userId, lease.leaseId);
      },
    };
  }
}
