import Decimal from "decimal.js";
import { randomUUID } from "node:crypto";
import {
  CapReachedError,
  ConfigError,
  InsufficientCreditsError,
  RefundError,
  StoreError,
} from "../errors.js";
import type { PricingEngine } from "../engine.js";
import type { CatalogRollout } from "../config.js";
import type {
  AddCreditsResult,
  AggregateStats,
  AllowanceResult,
  AvailableResult,
  BalanceResult,
  CatalogRevision,
  BucketBalancesResult,
  CanAffordResult,
  CheckFeatureResult,
  CreditMetadata,
  DailySpendRow,
  DeductionResult,
  DeductWithAllowanceOptions,
  ExecuteGrantProgramRequest,
  GetUserPlanResult,
  GrantProgramAwardResult,
  LedgerEntry,
  LedgerPage,
  LeaseSuccess,
  ListLedgerEntriesOptions,
  ListQuotaEventsOptions,
  ListUsageChargesOptions,
  ListUsageEntriesOptions,
  PlanMigrationBatchResult,
  PlanMigrationStartResult,
  QuotaEvent,
  QuotaState,
  RefundSuccess,
  RevokeCreditsResult,
  ReleaseResult,
  SetUserPlanResult,
  UnsetUserPlanResult,
  SpendByModelRow,
  SpendByUserRow,
  SweepResult,
  TeamDeductionResult,
  TopUserRow,
  UsageChargePage,
  UsageRecordResult,
} from "./types/index.js";
import type { CreditStore } from "./store.js";
import type { CreditEventEmitter, CreditEventType } from "./events.js";
import type { UsageMetrics } from "../metrics.js";
import { raiseDeductError } from "./service-errors.js";
import { LowBalanceMonitor } from "./low-balance-monitor.js";
import { CreditQueries } from "./queries.js";
import { CatalogRuntime, toDecimal } from "./catalog-runtime.js";
import { CreditLeaseWorkflow } from "./lease-workflow.js";
import type {
  AddCreditsOptions,
  BeginBilledOperationOptions,
  BilledOperation,
  CanAffordOptions,
  CreditsServiceOptions,
  DeductCreditsOptions,
  DeductFlatJobOptions,
  DeductOptions,
  DeductTeamOptions,
  GrantSubscriptionCycleOptions,
  MetricsOrAmount,
  PolicyPreset,
  PostDeductionContext,
  RecordUsageOptions,
  RefundCreditsOptions,
  ReserveOptions,
  RunBilledOptions,
  SettleOptions,
} from "./service-types.js";
export type {
  AddCreditsOptions,
  BeginBilledOperationOptions,
  BilledOperation,
  CanAffordOptions,
  CreditsServiceOptions,
  DeductCreditsOptions,
  DeductFlatJobOptions,
  DeductOptions,
  DeductTeamOptions,
  GrantSubscriptionCycleOptions,
  LowBalanceConfig,
  PolicyPreset,
  PostDeductionContext,
  RecordUsageOptions,
  RefundCreditsOptions,
  ReserveOptions,
  RunBilledOptions,
  SettleOptions,
} from "./service-types.js";
/**
 * Default lease TTL (seconds) for ``reserve`` and ``runBilled``.
 * Long batch/agentic jobs call the configured lease TTL before this elapses.
 */
const DEFAULT_LEASE_TTL_SECONDS = 600;

const POLICY_PRESETS = new Set<PolicyPreset>(["strict_prepaid", "overdraft"]);

function positiveAmount(value: Decimal | number, operation: string): Decimal {
  const amount = toDecimal(value);
  if (!amount.isFinite() || !amount.gt(0)) {
    throw new RangeError(`${operation} amount must be finite and greater than zero`);
  }
  return amount;
}

import { type NormalizedLogger, normalizeLogger } from "../shared/logger.js";

/**
 * Orchestrates credit operations.
 *
 * The deduction path is a single atomic, idempotency-keyed store call
 * (``deductWithAllowance``) that consumes free allowance, enforces plan policy,
 * and debits the net amount in one transaction. The service calculates the cost, maps
 * the store's typed ``error`` codes to exceptions, and emits lifecycle events
 * **only after** the operation has succeeded.
 *
 * Optionally accepts a ``CreditEventEmitter`` to emit lifecycle events
 * (deducted, deduct_failed, added, refunded, refund_failed, expired,
 * low_balance).
 */
export class CreditsService {
  private readonly store: CreditStore;
  private readonly queries: CreditQueries;
  private readonly catalogRuntime: CatalogRuntime;
  private readonly leases: CreditLeaseWorkflow;
  private emitter: CreditEventEmitter | null = null;
  private balanceMonitor: LowBalanceMonitor;
  // Lazy-on-read credit expiry (closes the gap with allowance windows/lease
  // TTLs, which are already lazy-on-read). Default `false` — unchanged
  // behaviour; explicit `sweepExpiredCredits`/cron remains required.
  private lazyExpiry: boolean;
  private logger: NormalizedLogger;
  private readonly postDeductionHooks = new Set<
    (context: PostDeductionContext) => void | Promise<void>
  >();
  constructor(
    store: CreditStore,
    engine?: PricingEngine | null,
    emitter?: CreditEventEmitter | null,
    options?: CreditsServiceOptions | null,
  ) {
    this.store = store;
    this.queries = new CreditQueries(
      store,
      options?.analytics ?? store,
      options?.usageStore ?? store,
    );
    const policy = options?.policy ?? "strict_prepaid";
    if (!POLICY_PRESETS.has(policy)) {
      throw new ConfigError(
        `unknown policy preset '${policy}'; expected one of ${[...POLICY_PRESETS].sort().join(", ")}`,
      );
    }
    if (emitter) this.emitter = emitter;
    this.logger = normalizeLogger(options?.logger);
    if (options?.postDeduction) this.postDeductionHooks.add(options.postDeduction);
    this.catalogRuntime = new CatalogRuntime(
      store,
      engine ?? null,
      this.logger,
      options?.catalogCacheTtlMs ?? 300_000,
    );
    this.balanceMonitor = new LowBalanceMonitor(
      options?.lowBalance,
      (type, userId, data) => this.emit(type, userId, data),
      this.logger,
    );
    this.lazyExpiry = options?.lazyExpiry ?? false;
    this.leases = new CreditLeaseWorkflow(
      store,
      this.catalogRuntime,
      this.logger,
      this.balanceMonitor,
      policy,
      options?.overdraftFloor != null ? toDecimal(options.overdraftFloor) : null,
      options?.maxConcurrent ?? null,
      options?.defaultTtlSeconds ?? DEFAULT_LEASE_TTL_SECONDS,
      (type, userId, data) => this.emit(type, userId, data),
      (userId, idempotencyKey) => this.emitQuotaEvents(userId, idempotencyKey),
      (userId) => this.maybeLazyExpire(userId),
      (userId, result) => this.afterDeduction(userId, "settle", result),
    );
  }

  /**
   * Register an awaited post-commit deduction hook. A failing hook is logged
   * and isolated so it can never roll back an already committed charge.
   */
  addPostDeductionHook(hook: (context: PostDeductionContext) => void | Promise<void>): () => void {
    this.postDeductionHooks.add(hook);
    return () => this.postDeductionHooks.delete(hook);
  }

  private async afterDeduction(
    userId: string,
    source: PostDeductionContext["source"],
    deduction: PostDeductionContext["deduction"],
  ): Promise<void> {
    for (const hook of this.postDeductionHooks) {
      try {
        await hook({ userId, source, deduction });
      } catch (error) {
        this.logger.warn("[CreditsService] post-deduction hook failed", {
          userId,
          source,
          error: error instanceof Error ? error.message : String(error),
        });
      }
    }
  }

  async getActiveCatalog(): Promise<CatalogRevision | null> {
    return this.queries.getActiveCatalog();
  }

  async getUserPlan(userId: string): Promise<GetUserPlanResult> {
    return this.queries.getUserPlan(userId);
  }

  async checkFeature(userId: string, feature: string): Promise<CheckFeatureResult> {
    return this.queries.checkFeature(userId, feature);
  }

  async getQuotaState(userId: string, quotaKey?: string | null): Promise<QuotaState[]> {
    return this.queries.getQuotaState(userId, quotaKey);
  }

  async listQuotaEvents(userId: string, options?: ListQuotaEventsOptions): Promise<QuotaEvent[]> {
    return this.queries.listQuotaEvents(userId, options);
  }

  async startPlanMigration(
    fromPlanId: string | null,
    toPlanId: string,
  ): Promise<PlanMigrationStartResult> {
    return this.queries.startPlanMigration(fromPlanId, toPlanId);
  }

  async migratePlanBatch(
    migrationId: string,
    batchSize?: number,
  ): Promise<PlanMigrationBatchResult> {
    return this.queries.migratePlanBatch(migrationId, batchSize);
  }

  async revokeCreditsByEntryType(userId: string, entryType: string): Promise<RevokeCreditsResult> {
    const result = await this.queries.revokeCreditsByEntryType(userId, entryType);
    if (result.revoked.gt(0)) {
      this.emit("credits.revoked", userId, {
        userId,
        entryType,
        amount: result.revoked,
        balanceAfter: result.balanceAfter,
      });
    }
    return result;
  }

  /** Execute an application-driven catalog grant program. */
  async executeGrantProgram(
    request: ExecuteGrantProgramRequest,
  ): Promise<GrantProgramAwardResult[]> {
    return this.store.executeGrantProgram(request);
  }

  async getLedgerEntry(userId: string, entryId: string): Promise<LedgerEntry | null> {
    return this.queries.getLedgerEntry(userId, entryId);
  }

  async getAvailable(userId: string): Promise<AvailableResult> {
    return this.queries.getAvailable(userId);
  }

  async aggregateStats(start: Date, end: Date): Promise<AggregateStats> {
    return this.queries.aggregateStats(start, end);
  }

  async spendByUser(start: Date, end: Date): Promise<SpendByUserRow[]> {
    return this.queries.spendByUser(start, end);
  }

  async spendByModel(start: Date, end: Date): Promise<SpendByModelRow[]> {
    return this.queries.spendByModel(start, end);
  }

  async listLedgerEntries(userId: string, options?: ListLedgerEntriesOptions): Promise<LedgerPage> {
    return this.queries.listLedgerEntries(userId, options);
  }

  async listUsageEntries(userId: string, options?: ListUsageEntriesOptions): Promise<LedgerPage> {
    return this.queries.listUsageEntries(userId, options);
  }

  async listUsageCharges(
    userId: string,
    options?: ListUsageChargesOptions,
  ): Promise<UsageChargePage> {
    return this.queries.listUsageCharges(userId, options);
  }

  async topUsers(limit: number, start: Date, end: Date): Promise<TopUserRow[]> {
    return this.queries.topUsers(limit, start, end);
  }

  async dailySpend(start: Date, end: Date): Promise<DailySpendRow[]> {
    return this.queries.dailySpend(start, end);
  }

  /** Emit a credit lifecycle event. No-op if no emitter is configured. */
  private emit(type: CreditEventType, userId: string, data?: Record<string, unknown>): void {
    this.emitter?.emit({ type, timestamp: new Date(), userId, data });
  }

  private async emitQuotaEvents(userId: string, idempotencyKey: string): Promise<void> {
    const events = await this.store.listQuotaEvents(userId, {
      idempotencyKey,
      limit: 100,
    });
    for (const event of events) {
      const data = {
        quotaKey: event.quotaKey,
        operation: event.operation,
        measure: event.measure,
        thresholdPercent: event.thresholdPercent,
        usageChargeId: event.usageChargeId,
        idempotencyKey: event.idempotencyKey,
      };
      if (event.eventType === "blocked") {
        this.emit("credits.quota_blocked", userId, data);
      } else {
        this.emit("credits.quota_threshold", userId, data);
      }
    }
  }

  /** Load the active catalog revision from the store. */
  async loadCatalogFromStore(): Promise<void> {
    await this.catalogRuntime.loadFromStore();
  }

  /**
   * If the cached PricingEngine is stale (TTL expired), reload it from the
   * store. Concurrent callers are deduplicated via the underlying
   * ``lru-cache.fetch()`` — only one ``loadCatalogFromStore`` runs and all
   * callers await the same result.
   *
   * When ``catalogCacheTtlMs`` is ``0``, this is a no-op (the consumer must call
   * ``loadCatalogFromStore`` manually).
   */
  async refreshCatalogIfStale(): Promise<void> {
    await this.catalogRuntime.refreshIfStale();
  }

  /**
   * Invalidate the catalog cache so the next ``refreshCatalogIfStale`` call
   * reloads from the store. Useful after a manual ``loadCatalogFromStore``
   * that bypasses the cache, or when the consumer knows the remote config
   * has changed.
   */
  invalidateCatalog(): void {
    this.catalogRuntime.invalidate();
  }

  /**
   * Publish and activate a catalog revision, then update the local engine.
   *
   * The store write is awaited, so
   * a persistence failure surfaces to the caller instead of becoming an
   * unhandled promise rejection.
   */
  async publishAndActivateCatalog(
    config: Record<string, unknown>,
    label?: string | null,
    rollout?: CatalogRollout | Record<string, unknown> | null,
  ): Promise<string> {
    return this.catalogRuntime.publishAndActivate(config, label, rollout);
  }

  /** Publish a validated, inactive catalog draft. */
  async publishCatalogDraft(
    config: Record<string, unknown>,
    label?: string | null,
  ): Promise<string> {
    return this.catalogRuntime.publishDraft(config, label);
  }

  /** Activate an existing catalog version and reload the local engine. */
  async activateCatalogRevision(
    version: number,
    rollout?: CatalogRollout | Record<string, unknown> | null,
  ): Promise<string> {
    return this.catalogRuntime.activateRevision(version, rollout);
  }

  /** The current PricingEngine, or null if not loaded. */
  get pricingEngine(): PricingEngine | null {
    return this.catalogRuntime.currentEngine;
  }

  /**
   * Set a user's subscription plan and emit ``credits.plan_changed``.
   *
   * The store call is awaited so a persistence failure surfaces to the caller.
   * The event is emitted only after the store write succeeds.
   */
  async setUserPlan(
    userId: string,
    planKey: string,
    planAssignedAt?: Date | null,
  ): Promise<SetUserPlanResult> {
    this.logger.info("[CreditsService] setUserPlan", { planKey, planAssignedAt });
    const result = await this.store.setUserPlan(userId, planKey, planAssignedAt);
    this.emit("credits.plan_changed", userId, {
      userId,
      planKey,
      planAssignedAt: result.planAssignedAt,
      assignmentState: result.assignmentState,
      timestamp: new Date().toISOString(),
    });
    return result;
  }

  /** Unset a user's subscription plan and emit the plan-change event. */
  async unsetUserPlan(userId: string): Promise<UnsetUserPlanResult> {
    const result = await this.store.unsetUserPlan(userId);
    this.emit("credits.plan_changed", userId, {
      userId,
      planKey: null,
      timestamp: new Date().toISOString(),
    });
    return result;
  }

  /** Pin or unpin the user's current assignment revision. */
  async setPlanRevisionPin(userId: string, pinned: boolean): Promise<boolean> {
    return this.store.setPlanRevisionPin(userId, pinned);
  }

  /** Apply one bounded batch of renewal-effective plan changes. */
  async applyDuePlanChanges(limit = 100): Promise<number> {
    return this.store.applyDuePlanChanges(limit);
  }

  /** Sweep this user's expired lots when lazy expiry is enabled. */
  private async maybeLazyExpire(userId: string): Promise<void> {
    if (!this.lazyExpiry) return;
    await this.runSweep(false, userId);
  }

  async getBalance(userId: string): Promise<BalanceResult> {
    await this.maybeLazyExpire(userId);
    return await this.store.getBalance(userId);
  }

  /** Add credits to a user's account. */
  async addCredits(
    userId: string,
    amount: Decimal | number,
    options?: AddCreditsOptions,
  ): Promise<AddCreditsResult> {
    const type = options?.type ?? "adjustment";
    this.logger.info("[CreditsService] addCredits", { amount, type, bucket: options?.bucket });
    const result = await this.store.addCredits(
      userId,
      positiveAmount(amount, "addCredits"),
      type,
      options?.metadata,
      options?.expiresAt,
      options?.bucket,
      options?.idempotencyKey,
    );
    this.emit("credits.added", userId, {
      entryId: result.entryId,
      amount: result.amount,
      newBalance: result.newBalance,
      type,
      idempotent: result.idempotent ?? false,
    });
    this.balanceMonitor.rearmAfterCredit(userId, result.newBalance);
    return result;
  }

  /**
   * Deduct a raw credit amount from a user's account.
   *
   * Uses the store's `addCredits` with `type='adjustment'` and a negated
   * amount — the existing validation path for negative/zero adjustments.
   * Use this for refund clawbacks and other administrative deductions that
   * bypass the usage-based `deduct()` flow.
   */
  async deductCredits(
    userId: string,
    amount: Decimal | number,
    options?: DeductCreditsOptions,
  ): Promise<AddCreditsResult> {
    const entryType = options?.entryType ?? "adjustment";
    this.logger.info("[CreditsService] deductCredits", {
      amount,
      entryType,
      bucket: options?.bucket,
    });
    const result = await this.store.addCredits(
      userId,
      positiveAmount(amount, "deductCredits").neg(),
      entryType,
      options?.metadata ?? null,
      null,
      options?.bucket ?? undefined,
      options?.idempotencyKey,
    );
    this.emit("credits.deducted", userId, {
      entryId: result.entryId,
      amount: result.amount,
      newBalance: result.newBalance,
      entryType,
      idempotent: result.idempotent ?? false,
    });
    if (!result.idempotent) {
      await this.afterDeduction(userId, "raw", {
        entryId: result.entryId,
        userId,
        amount: result.amount.abs(),
        allowanceConsumed: new Decimal(0),
        balanceAfter: result.newBalance,
        idempotent: false,
        usageChargeId: null,
        error: null,
        bucketBreakdown: null,
      });
    }
    return result;
  }

  /**
   * Grant one billing-cycle's worth of credits — idempotent-safe for a
   * payment-provider webhook handler (Stripe, etc. — bursar stays
   * provider-agnostic) to call even on webhook redelivery.
   *
   * 1. At most one of ``expiresAt``/``ttlDays`` may be given (throws
   *    ``ConfigError`` otherwise).
   * 2. When ``ttlDays`` is given, ``expiresAt = now + ttlDays`` days.
   * 3. The new cycle is granted via a direct ``store.addCredits`` call
   *    (bypassing {@link addCredits} so only ``credits.cycle_renewed`` fires,
   *    not a duplicate ``credits.added``), threading ``idempotencyKey`` so a
   *    redelivered webhook replays the prior grant instead of double-crediting.
   * 4. Existing bucket credits are never removed here. Subscription renewal
   *    policies that replace prior lots run atomically through BillingService.
   * 5. When ``planKey`` is given, assigns it via {@link setUserPlan}.
   * 6. Emits ``credits.cycle_renewed`` and returns the grant result.
   */
  async grantSubscriptionCycle(
    userId: string,
    amount: Decimal | number,
    options?: GrantSubscriptionCycleOptions,
  ): Promise<AddCreditsResult> {
    this.logger.info("[CreditsService] grantSubscriptionCycle", {
      amount,
      bucket: options?.bucket,
      planKey: options?.planKey,
    });
    if (options?.expiresAt != null && options?.ttlDays != null) {
      throw new ConfigError(
        "grantSubscriptionCycle: specify at most one of 'expiresAt' or 'ttlDays', not both",
      );
    }
    const bucket = options?.bucket ?? "subscription";
    const expiresAt: Date | undefined =
      options?.ttlDays != null
        ? new Date(Date.now() + options.ttlDays * 86_400_000)
        : options?.expiresAt;
    const amountDec = positiveAmount(amount, "grantSubscriptionCycle");

    const result = await this.store.addCredits(
      userId,
      amountDec,
      "purchase",
      options?.metadata,
      expiresAt,
      bucket,
      options?.idempotencyKey,
    );

    // The store contract exposes the mutation's replay result directly, avoiding
    // a racy lifetime-balance comparison and an extra database round trip.
    const isFreshGrant = !result.idempotent;
    if (options?.planKey) {
      // A fresh cycle intentionally re-anchors plan-assignment windows. On a
      // webhook replay, repair a previously interrupted grant->assignment saga
      // without re-anchoring an assignment that already succeeded.
      if (isFreshGrant || (await this.getUserPlan(userId)).planKey !== options.planKey) {
        await this.setUserPlan(userId, options.planKey);
      }
    }

    this.emit("credits.cycle_renewed", userId, {
      entryId: result.entryId,
      amount: amountDec,
      newBalance: result.newBalance,
      bucket,
      planKey: options?.planKey ?? null,
      idempotencyKey: options?.idempotencyKey ?? null,
      idempotent: result.idempotent,
    });

    return result;
  }

  // ── Lease lifecycle: atomic admission ───────────────────────────────

  async reserve(
    userId: string,
    metricsOrAmount: MetricsOrAmount,
    options?: ReserveOptions,
  ): Promise<LeaseSuccess> {
    return this.leases.reserve(userId, metricsOrAmount, options);
  }

  async settle(
    userId: string,
    leaseId: string,
    metricsOrAmount: MetricsOrAmount,
    options?: SettleOptions,
  ): Promise<DeductionResult> {
    return this.leases.settle(userId, leaseId, metricsOrAmount, options);
  }

  async release(userId: string, leaseId: string): Promise<ReleaseResult> {
    return this.leases.release(userId, leaseId);
  }

  async renew(userId: string, leaseId: string, ttl?: number | null): Promise<LeaseSuccess> {
    return this.leases.renew(userId, leaseId, ttl);
  }

  async canAfford(
    userId: string,
    metricsOrAmount: MetricsOrAmount,
    options?: CanAffordOptions,
  ): Promise<CanAffordResult> {
    return this.leases.canAfford(userId, metricsOrAmount, options);
  }

  async getBucketBalances(userId: string): Promise<BucketBalancesResult> {
    return this.leases.getBucketBalances(userId);
  }

  async checkAllowance(userId: string): Promise<AllowanceResult | null> {
    return this.leases.checkAllowance(userId);
  }

  async runBilled<T>(
    userId: string,
    options: RunBilledOptions<T>,
  ): Promise<{ result: T; deduction: DeductionResult }> {
    return this.leases.runBilled(userId, options);
  }

  async beginBilledOperation(
    userId: string,
    options: BeginBilledOperationOptions,
  ): Promise<BilledOperation> {
    return this.leases.beginBilledOperation(userId, options);
  }

  /**
   * Full deduction flow as one atomic store call.
   *
   * 1. ``breakdown = engine.calculate(metrics)``; ``cost = breakdown.total``
   *    (exact `Decimal`, **no truncation**).
   * 2. ``store.deductWithAllowance`` records the usage, enforces policy, consumes
   *    allowance, and debits any remainder — idempotency-keyed end-to-end.
   *
   * On a store ``error`` a ``credits.deduct_failed`` event is emitted and a
   * typed exception is thrown. No success event is emitted on error.
   */
  async deduct(
    userId: string,
    metrics: UsageMetrics,
    options?: DeductOptions,
  ): Promise<DeductionResult> {
    await this.maybeLazyExpire(userId);
    this.logger.debug("[CreditsService] deduct", {
      model: metrics.dimensions?.model,
      feature: options?.feature,
    });
    const engine = await this.catalogRuntime.engineForUser(userId);
    const plan = await this.store.getUserPlan(userId);
    const effectiveIdempotencyKey = options?.idempotencyKey ?? `usage:${randomUUID()}`;

    // 1) Calculate cost as an exact Decimal, never truncated.
    const breakdown = engine.calculate(metrics, { rateCard: plan.rateCard ?? undefined });
    const cost = breakdown.total;

    // Build ledger metadata: caller fields FIRST, system fields LAST so the
    // protected system fields win.
    const meta: Record<string, unknown> = {};
    if (options?.metadata) {
      for (const [k, v] of Object.entries(options.metadata)) {
        if (v != null) meta[k] = v;
      }
    }
    meta["operation"] = metrics.operation;
    meta["measures"] = { ...(metrics.measures ?? {}) };
    meta["dimensions"] = { ...(metrics.dimensions ?? {}) };
    meta["breakdownTotal"] = breakdown.total.toString();
    meta["idempotencyKey"] = effectiveIdempotencyKey;

    const deductionOptions: DeductWithAllowanceOptions = {
      idempotencyKey: effectiveIdempotencyKey,
      operation: metrics.operation,
      feature: options?.feature ?? null,
      model: typeof metrics.dimensions?.model === "string" ? metrics.dimensions.model : null,
      region: typeof metrics.dimensions?.region === "string" ? metrics.dimensions.region : null,
      measures: { ...(metrics.measures ?? {}) },
      dimensions: { ...(metrics.dimensions ?? {}) },
      metadata: meta as CreditMetadata,
    };

    // 2) Atomic charge. This records zero-cost usage too, so authorization,
    // quotas, and usage history cannot be bypassed by a free rate.
    const result = await this.store.deductWithAllowance(userId, cost, deductionOptions);

    if (result.error !== null) {
      if (result.error === "quota_exceeded") {
        await this.emitQuotaEvents(userId, effectiveIdempotencyKey);
      }
      this.logger.warn("[CreditsService] deduct failed", {
        error: result.error,
        amount: cost,
        model: metrics.dimensions?.model,
        feature: options?.feature,
      });
      this.emit("credits.deduct_failed", userId, {
        error: result.error,
        amount: cost,
        model: metrics.dimensions?.model ?? null,
      });
      raiseDeductError(result.error, userId, cost);
    }

    // Success — emit deducted, then any cap warning, then edge-triggered low-balance.
    this.emit("credits.deducted", userId, {
      entryId: result.entryId,
      amount: result.amount,
      allowanceConsumed: result.allowanceConsumed,
      balanceAfter: result.balanceAfter,
      model: metrics.dimensions?.model ?? null,
      idempotent: result.idempotent,
    });

    // low_balance is edge-triggered: only fire when this deduction crossed
    // the threshold. A replayed (idempotent) result did not move the balance, so
    // it never crosses. balanceBefore = balanceAfter + amount charged.
    if (!result.idempotent) {
      const balanceBefore = result.balanceAfter.plus(result.amount);
      await this.balanceMonitor.signalCrossing(userId, balanceBefore, result.balanceAfter);
      await this.emitQuotaEvents(userId, effectiveIdempotencyKey);
      await this.afterDeduction(userId, "deduct", result);
    }

    return result;
  }

  /**
   * Record priced usage for a workflow without debiting the account again.
   *
   * This supports priced usage receipts that should be retained without
   * affecting the account balance, such as externally billed usage.
   */
  async recordUsage(
    userId: string,
    metrics: UsageMetrics,
    options?: RecordUsageOptions,
  ): Promise<UsageRecordResult> {
    const engine = await this.catalogRuntime.engineForUser(userId);
    const plan = await this.store.getUserPlan(userId);
    const effectiveIdempotencyKey = options?.idempotencyKey ?? `usage-record:${randomUUID()}`;
    const breakdown = engine.calculate(metrics, { rateCard: plan.rateCard ?? undefined });
    const meta: Record<string, unknown> = {};
    if (options?.metadata) {
      for (const [key, value] of Object.entries(options.metadata)) {
        if (value != null) meta[key] = value;
      }
    }
    meta.operation = metrics.operation;
    meta.measures = { ...(metrics.measures ?? {}) };
    meta.dimensions = { ...(metrics.dimensions ?? {}) };
    meta.breakdownTotal = breakdown.total.toString();
    meta.idempotencyKey = effectiveIdempotencyKey;

    const result = await this.store.recordUsage(userId, metrics.operation, breakdown.total, {
      idempotencyKey: effectiveIdempotencyKey,
      operation: metrics.operation,
      model: typeof metrics.dimensions?.model === "string" ? metrics.dimensions.model : null,
      region: typeof metrics.dimensions?.region === "string" ? metrics.dimensions.region : null,
      measures: { ...(metrics.measures ?? {}) },
      dimensions: { ...(metrics.dimensions ?? {}) },
      metadata: meta as CreditMetadata,
    });
    if (result.error !== null) {
      throw new StoreError(`Usage record failed: ${result.error}`);
    }
    return result;
  }

  /**
   * Refund a previous credit deduction.
   *
   * Returns the RefundResult. A successful refund
   * emits ``credits.refunded``; a failed/duplicate/over-refund emits
   * ``credits.refund_failed`` (no success event is ever emitted for a failed
   * refund).
   */
  /** Deduct the configured fixed cost for one named job. */
  async deductFlatJob(
    userId: string,
    jobName: string,
    options?: DeductFlatJobOptions,
  ): Promise<DeductionResult> {
    return this.deduct(userId, { operation: jobName, measures: { jobs: 1 } }, options);
  }

  async refundCredits(entryId: string, options?: RefundCreditsOptions): Promise<RefundSuccess> {
    const refundAmount =
      options?.amount != null ? positiveAmount(options.amount, "refundCredits") : undefined;
    this.logger.info("[CreditsService] refundCredits", {
      entryId,
      refundAmount,
      reason: options?.reason,
    });
    const result = await this.store.refundCredits(
      entryId,
      refundAmount,
      options?.reason,
      options?.metadata,
      options?.idempotencyKey,
    );

    if (result.error !== null) {
      this.logger.warn("[CreditsService] refundCredits failed", {
        entryId,
        error: result.error,
      });
      if (result.userId !== null) {
        this.emit("credits.refund_failed", result.userId, {
          entryId,
          error: result.error,
          reason: options?.reason ?? null,
        });
      }
      throw new RefundError(`Refund rejected: ${result.error}`);
    }

    this.emit("credits.refunded", result.userId, {
      entryId,
      refundEntryId: result.refundEntryId,
      amount: result.amount,
      newBalance: result.newBalance,
      reason: options?.reason ?? null,
    });
    return result;
  }

  /**
   * Deduct from a team's shared balance pool.
   *
   * Calculates the cost via the pricing engine (exact `Decimal`, no truncation),
   * then debits the team balance. Threads an optional ``idempotencyKey`` through
   * to the store so retried team charges are not double-counted.
   */
  async deductTeam(
    teamId: string,
    userId: string,
    metrics: UsageMetrics,
    options?: DeductTeamOptions,
  ): Promise<TeamDeductionResult> {
    await this.maybeLazyExpire(userId);
    // Lazy expiry is scoped to the individual member's credits, not the team's
    // shared pool — there's no per-team expiry concept.
    const engine = await this.catalogRuntime.engineForUser(userId);
    const plan = await this.store.getUserPlan(userId);

    const breakdown = engine.calculate(metrics, { rateCard: plan.rateCard ?? undefined });
    const cost = breakdown.total;

    if (cost.lte(0)) {
      const teamBal = await this.store.getTeamBalance(teamId);
      if (teamBal === null) {
        throw new StoreError(`Team not found: ${teamId}`);
      }
      return {
        entryId: null,
        teamId,
        userId,
        amount: new Decimal(0),
        teamBalanceAfter: teamBal.balance,
        idempotent: false,
        error: null,
      };
    }

    const teamMetadata: CreditMetadata = {
      ...(options?.metadata ?? {}),
      operation: metrics.operation,
      measures: { ...(metrics.measures ?? {}) },
      dimensions: Object.fromEntries(
        Object.entries(metrics.dimensions ?? {}).map(([key, value]) => [key, String(value)]),
      ),
      breakdownTotal: breakdown.total.toString(),
    };
    const result = await this.store.deductTeam(
      teamId,
      userId,
      cost,
      teamMetadata,
      options?.idempotencyKey,
    );
    // Surface store errors: emit credits.deduct_failed and throw,
    // mirroring the Python credit service implementation. Previously returned a silent
    // success-shaped object with an .error field, so failed charges looked OK.
    if (result.error !== null) {
      this.emit("credits.deduct_failed", userId, {
        error: result.error,
        amount: cost,
        teamId,
        deductType: "team",
      });
      if (result.error === "member_spend_cap_exceeded") {
        throw new CapReachedError(
          `Team member spend cap exceeded. Team=${teamId}, user=${userId}, requested=${cost}`,
        );
      }
      throw new InsufficientCreditsError(
        `Team deduction failed: ${result.error}. Team=${teamId}, user=${userId}, requested=${cost}`,
      );
    }
    this.emit("credits.deducted", userId, {
      entryId: result.entryId,
      amount: result.amount,
      teamBalanceAfter: result.teamBalanceAfter,
      teamId,
      deductType: "team",
    });
    return result;
  }
  private async runSweep(dryRun: boolean, userId?: string): Promise<SweepResult> {
    const result = await this.store.sweepExpiredCredits(dryRun, userId);
    if (!dryRun && result.expiredCount > 0) {
      this.emit("credits.expired", userId ?? "system", {
        expiredCount: result.expiredCount,
        expiredAmount: result.expiredAmount,
        expiredByBucket: result.expiredByBucket ?? null,
      });
    }
    return result;
  }

  /**
   * Sweep expired credits from all users' balances.
   *
   * When ``dryRun`` is true, reports what would expire without mutating
   * balances. Automatic lazy sweeps remain scoped to one subject.
   */
  async sweepExpiredCredits(dryRun = false): Promise<SweepResult> {
    return await this.runSweep(dryRun);
  }
}
