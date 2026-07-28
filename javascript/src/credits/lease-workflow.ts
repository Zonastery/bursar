import Decimal from "decimal.js";
import { randomUUID } from "node:crypto";
import { ConfigError } from "../errors.js";
import type { NormalizedLogger } from "../shared/logger.js";
import type { CreditEventType } from "./events.js";
import { LowBalanceMonitor } from "./low-balance-monitor.js";
import { isAmount, PricingRuntime } from "./pricing-runtime.js";
import { raiseLeaseError } from "./service-errors.js";
import type {
  CanAffordOptions,
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
  LeaseResult,
  ReleaseResult,
} from "./types/index.js";

export class CreditLeaseWorkflow {
  constructor(
    private readonly store: CreditStore,
    private readonly pricing: PricingRuntime,
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
  ) {}

  private async expectedAdmissionPolicy(
    userId: string,
    billingModeOverride?: BillingMode | null,
  ): Promise<{
    billingMode: BillingMode;
    floor: Decimal;
    maxConcurrent: number | null;
  }> {
    const plan = await this.store.getUserPlan(userId);
    const defaultMode: BillingMode =
      plan.planId == null
        ? this.policy === "overdraft"
          ? "overdraft"
          : "strict"
        : plan.billingMode;
    const billingMode = billingModeOverride ?? defaultMode;
    const floor =
      billingMode === "overdraft"
        ? (plan.overdraftFloor ?? this.overdraftFloor ?? new Decimal(0))
        : new Decimal(0);
    return {
      billingMode,
      floor,
      maxConcurrent: plan.planId == null ? this.defaultMaxConcurrent : (plan.maxConcurrent ?? null),
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
  ): Promise<{ amount: Decimal; model: string | null }> {
    return this.pricing.costOf(metricsOrAmount, userId);
  }

  /**
   * Atomically acquire a lease — the only admission control (D4).
   *
   * Prices the estimate and delegates entitlement, quota, allowance, credit
   * policy, and concurrency enforcement to the database in one atomic call.
   */
  async reserve(
    userId: string,
    metricsOrAmount: MetricsOrAmount,
    options?: ReserveOptions,
  ): Promise<LeaseResult> {
    await this.maybeLazyExpire(userId);
    this.logger.debug("[CreditsService] reserve", {
      feature: options?.feature ?? options?.requiredFeature,
      operationType: options?.operationType,
    });
    if (
      options?.feature != null &&
      options.requiredFeature != null &&
      options.feature !== options.requiredFeature
    ) {
      throw new ConfigError("reserve feature and requiredFeature must match when both are set");
    }
    const operationType =
      options?.operationType ?? (isAmount(metricsOrAmount) ? "usage" : metricsOrAmount.operation);
    const expectedPolicy = await this.expectedAdmissionPolicy(userId, options?.billingMode);
    const { amount, model } = await this.costOf(metricsOrAmount, userId);
    const ttlSeconds = options?.ttl != null ? options.ttl : this.defaultTtl;
    const feature = options?.feature ?? options?.requiredFeature ?? null;
    const measures = isAmount(metricsOrAmount) ? {} : { ...(metricsOrAmount.measures ?? {}) };
    const dimensions = isAmount(metricsOrAmount) ? {} : { ...(metricsOrAmount.dimensions ?? {}) };
    const region = typeof dimensions.region === "string" ? dimensions.region : null;
    const leaseIdempotencyKey = options?.idempotencyKey ?? `lease:${randomUUID()}`;

    const result = await this.store.createLease(userId, amount, operationType, {
      idempotencyKey: leaseIdempotencyKey,
      billingMode: expectedPolicy.billingMode,
      floor: expectedPolicy.floor,
      maxConcurrent: expectedPolicy.maxConcurrent,
      ttlSeconds,
      model,
      metadata: options?.metadata,
      feature,
      region,
      measures,
      dimensions,
    });

    if (result.error) {
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
   * Charge the ACTUAL cost against a lease and finalize it (D5).
   *
   * De-clamped: bills the full actual cost even if it exceeds the lease hold
   * (overdraft). Never blocks on floor/cap at settle — a cap breach surfaces as a
   * non-blocking ``credits.cap_warning``/``credits.cap_reached`` signal. Emits
   * ``credits.deducted``, then multi-level ``credits.low_balance`` and a
   * ``credits.overdraft`` signal if the balance went negative.
   */
  async settle(
    userId: string,
    leaseId: string,
    metricsOrAmount: MetricsOrAmount,
    options?: SettleOptions,
  ): Promise<DeductionResult> {
    await this.maybeLazyExpire(userId);
    this.logger.debug("[CreditsService] settle", { leaseId });
    const idempotencyKey = options?.idempotencyKey ?? `lease:${leaseId}:settle`;
    const feature = options?.feature ?? null;
    const { amount, model } = await this.costOf(metricsOrAmount, userId);
    const measures = isAmount(metricsOrAmount) ? {} : { ...(metricsOrAmount.measures ?? {}) };
    const dimensions = isAmount(metricsOrAmount) ? {} : { ...(metricsOrAmount.dimensions ?? {}) };
    const region = typeof dimensions.region === "string" ? dimensions.region : null;

    // Build ledger metadata: caller fields first, system fields last (M7).
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

    if (result.error) {
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
    }
    return result;
  }

  /** Release a lease without charging (work failed/aborted) — idempotent (H1). */
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
  async renew(userId: string, leaseId: string, ttl?: number | null): Promise<LeaseResult> {
    const ttlSeconds = ttl ?? this.defaultTtl;
    this.logger.debug("[CreditsService] renew", { leaseId, ttlSeconds });
    const result = await this.store.renewLease(userId, leaseId, ttlSeconds);
    if (result.error) {
      if (result.error === "expired_lease") {
        this.emit("credits.lease_expired", userId, { leaseId });
      }
      raiseLeaseError(result.error, userId, new Decimal(0));
    }
    return result;
  }

  /**
   * Advisory affordability check — UI only, non-locking, may be stale (D4/H3).
   *
   * Never use this as an admission gate; only ``reserve`` is authoritative.
   */
  async canAfford(
    userId: string,
    metricsOrAmount: MetricsOrAmount,
    options?: CanAffordOptions,
  ): Promise<CanAffordResult> {
    await this.maybeLazyExpire(userId);
    const feature = options?.feature ?? options?.requiredFeature ?? null;
    const { amount: worstCase } = await this.costOf(metricsOrAmount, userId);
    const avail = await this.store.getAvailable(userId);
    const expectedPolicy = await this.expectedAdmissionPolicy(userId, options?.billingMode);
    const allowance = await this.store.checkAllowance(userId);
    const spendable = avail.available
      .plus(allowance.allowanceRemaining)
      .minus(expectedPolicy.floor);

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
   * to reach past it into the raw store. Returns a zero-allowance result for
   * planless users (no exception).
   *
   * Window calculation is database-owned and supports the full normalized
   * policy tuple, including timezone and arbitrary interval counts.
   */
  async checkAllowance(userId: string): Promise<AllowanceResult> {
    return await this.store.checkAllowance(userId);
  }

  /**
   * One-call shortcut wiring reserve → doWork → settle (interface plan §4).
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
    const lease = await this.reserve(userId, options.estimate, {
      operationType: options.operationType,
      billingMode: options.billingMode,
      requiredFeature: options.requiredFeature,
      ttl: options.ttl,
      feature: options.feature,
    });

    let workResult: T;
    let actual: MetricsOrAmount;
    try {
      ({ result: workResult, actual } = await options.doWork());
    } catch (err) {
      await this.release(userId, lease.leaseId);
      throw err;
    }

    const deduction = await this.settle(userId, lease.leaseId, actual, {
      idempotencyKey: options.idempotencyKey,
      feature: options.feature,
    });
    return { result: workResult, deduction };
  }
}
