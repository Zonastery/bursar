import Decimal from "decimal.js";
import { randomUUID } from "node:crypto";
import { CapReachedError, ConfigError, InsufficientCreditsError } from "../errors.js";
import type { PricingEngine } from "../engine.js";
import type {
  AddCreditsResult,
  AggregateStats,
  AllowanceResult,
  AvailableResult,
  BalanceResult,
  BursarConfigResult,
  BucketBalancesResult,
  CanAffordResult,
  CheckFeatureResult,
  CreditMetadata,
  DailySpendRow,
  DeductionResult,
  DeductWithAllowanceOptions,
  ExecuteGrantProgramRequest,
  FeatureLimitResult,
  GetUserPlanResult,
  GrantProgramAwardResult,
  LedgerEntry,
  LedgerPage,
  LeaseResult,
  ListLedgerEntriesOptions,
  ListQuotaEventsOptions,
  ListUsageEntriesOptions,
  MigratePlanUsersResult,
  PlanMigrationBatchResult,
  PlanMigrationStartResult,
  QuotaEvent,
  QuotaState,
  RefundResult,
  ReleaseResult,
  SpendByModelRow,
  SpendByUserRow,
  SweepResult,
  TeamDeductionResult,
  TopUserRow,
} from "./types/index.js";
import type { CreditStore } from "./store.js";
import type { CreditEventEmitter, CreditEventType } from "./events.js";
import type { UsageMetrics } from "../metrics.js";
import { raiseDeductError } from "./service-errors.js";
import { LowBalanceMonitor } from "./low-balance-monitor.js";
import { CreditQueries } from "./queries.js";
import { PricingRuntime, toDecimal } from "./pricing-runtime.js";
import { CreditLeaseWorkflow } from "./lease-workflow.js";
import type {
  CanAffordOptions,
  CreditsServiceOptions,
  GrantSubscriptionCycleOptions,
  MetricsOrAmount,
  PolicyPreset,
  PostDeductionContext,
  ReserveOptions,
  RunBilledOptions,
  SettleOptions,
} from "./service-types.js";
export type {
  CanAffordOptions,
  CreditsServiceOptions,
  GrantSubscriptionCycleOptions,
  LowBalanceConfig,
  PolicyPreset,
  ReserveOptions,
  RunBilledOptions,
  SettleOptions,
  PostDeductionContext,
} from "./service-types.js";
/**
 * Default lease TTL (seconds) for ``reserve``/``runBilled`` (interface plan §3).
 * Long batch/agentic jobs call the configured lease TTL before this elapses.
 */
const DEFAULT_LEASE_TTL_SECONDS = 600;

const POLICY_PRESETS = new Set<PolicyPreset>(["strict_prepaid", "overdraft"]);

import { type NormalizedLogger, normalizeLogger } from "../shared/logger.js";

/**
 * Orchestrates credit operations.
 *
 * The deduction path is a single atomic, idempotency-keyed store call
 * (``deductWithAllowance``) that consumes free allowance, enforces spend caps,
 * applies the balance floor and debits the net amount in one transaction
 * (contract §2). The manager is a thin layer that calculates the cost, maps
 * the store's typed ``error`` codes to exceptions, and emits lifecycle events
 * **only after** the operation has succeeded (contract §6).
 *
 * Optionally accepts a ``CreditEventEmitter`` to emit lifecycle events
 * (deducted, deduct_failed, added, refunded, refund_failed, expired,
 * cap_reached, cap_warning, low_balance).
 */
export class CreditsService {
  private readonly store: CreditStore;
  private readonly queries: CreditQueries;
  private readonly pricing: PricingRuntime;
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
    this.queries = new CreditQueries(store, options?.analytics ?? store);
    const policy = options?.policy ?? "strict_prepaid";
    if (!POLICY_PRESETS.has(policy)) {
      throw new ConfigError(
        `unknown policy preset '${policy}'; expected one of ${[...POLICY_PRESETS].sort().join(", ")}`,
      );
    }
    if (emitter) this.emitter = emitter;
    this.logger = normalizeLogger(options?.logger);
    if (options?.postDeduction) this.postDeductionHooks.add(options.postDeduction);
    this.pricing = new PricingRuntime(
      store,
      engine ?? null,
      this.logger,
      options?.pricingTtl ?? 300_000,
    );
    this.balanceMonitor = new LowBalanceMonitor(
      options?.lowBalance,
      (type, userId, data) => this.emit(type, userId, data),
      () => new Decimal(this.pricing.currentEngine?.minBalance ?? 0),
      this.logger,
    );
    this.lazyExpiry = options?.lazyExpiry ?? false;
    this.leases = new CreditLeaseWorkflow(
      store,
      this.pricing,
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

  async getActivePricing(): Promise<BursarConfigResult | null> {
    return this.queries.getActivePricing();
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

  /** @deprecated Use `getQuotaState`; `feature` is interpreted as a quota key. */
  async checkFeatureLimit(userId: string, feature: string): Promise<FeatureLimitResult> {
    return this.queries.checkFeatureLimit(userId, feature);
  }

  /** @deprecated Prefer resumable migrations for large populations. */
  async migratePlanUsers(
    planKey: string,
    targetConfigVersion?: number | null,
  ): Promise<MigratePlanUsersResult> {
    return this.queries.migratePlanUsers(planKey, targetConfigVersion);
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

  async revokeCreditsByEntryType(
    userId: string,
    entryType: string,
  ): Promise<Record<string, unknown>> {
    return this.queries.revokeCreditsByEntryType(userId, entryType);
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
        // Compatibility notification for consumers of the former feature-limit API.
        this.emit("credits.feature_limit_reached", userId, {
          ...data,
          feature: event.quotaKey,
        });
      } else {
        this.emit("credits.quota_threshold", userId, data);
        this.emit("credits.feature_limit_warning", userId, {
          ...data,
          feature: event.quotaKey,
          action: "notify",
        });
      }
    }
  }

  /** Load pricing from a raw dict and sync it. */
  async publishPricingFromDict(data: Record<string, unknown>): Promise<void> {
    await this.pricing.publishFromDict(data);
  }

  /** Load the active pricing config from the store. */
  async loadPricingFromStore(): Promise<void> {
    await this.pricing.loadFromStore();
  }

  /**
   * If the cached PricingEngine is stale (TTL expired), reload it from the
   * store. Concurrent callers are deduplicated via the underlying
   * ``lru-cache.fetch()`` — only one ``loadPricingFromStore`` runs and all
   * callers await the same result.
   *
   * When ``pricingTtl`` is ``0``, this is a no-op (the consumer must call
   * ``loadPricingFromStore`` manually).
   */
  async refreshIfStale(): Promise<void> {
    await this.pricing.refreshIfStale();
  }

  /**
   * Invalidate the pricing cache so the next ``refreshIfStale`` call
   * reloads from the store. Useful after a manual ``loadPricingFromStore``
   * that bypasses the cache, or when the consumer knows the remote config
   * has changed.
   */
  invalidatePricing(): void {
    this.pricing.invalidate();
  }

  /**
   * Publish new pricing and update the engine in one call.
   *
   * H10: the store write is now **awaited** (was a fire-and-forget `void`), so
   * a persistence failure surfaces to the caller instead of becoming an
   * unhandled promise rejection.
   */
  async publishPricing(config: Record<string, unknown>, label?: string | null): Promise<void> {
    await this.pricing.publish(config, label);
  }

  /** Publish a validated, inactive catalog draft. */
  async publishPricingDraft(
    config: Record<string, unknown>,
    label?: string | null,
  ): Promise<string> {
    return this.pricing.publishDraft(config, label);
  }

  /** Activate an existing catalog version and reload the local engine. */
  async activatePricing(version: number): Promise<string> {
    return this.pricing.activate(version);
  }

  /** The current PricingEngine, or null if not loaded. */
  get pricingEngine(): PricingEngine | null {
    return this.pricing.currentEngine;
  }

  /**
   * Set a user's subscription plan and emit ``credits.plan_changed``.
   *
   * The store call is awaited so a persistence failure surfaces to the caller.
   * The event is emitted only after the store write succeeds (contract §6).
   */
  async setUserPlan(userId: string, planKey: string, planAssignedAt?: Date | null): Promise<void> {
    this.logger.info("[CreditsService] setUserPlan", { planKey, planAssignedAt });
    const result = await this.store.setUserPlan(userId, planKey, planAssignedAt);
    this.emit("credits.plan_changed", userId, {
      userId,
      planKey,
      planAssignedAt: result.planAssignedAt ?? null,
      timestamp: new Date().toISOString(),
    });
  }

  /** Unset a user's subscription plan and emit the plan-change event. */
  async unsetUserPlan(userId: string): Promise<void> {
    await this.store.unsetUserPlan(userId);
    this.emit("credits.plan_changed", userId, {
      userId,
      planKey: null,
      timestamp: new Date().toISOString(),
    });
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
    options?: {
      type?: string;
      metadata?: CreditMetadata | null;
      expiresAt?: Date | null;
      /** Target credit bucket; omitted resolves to the config's default bucket. */
      bucket?: string | null;
      /** Replay-safe idempotency key (parity with `deduct`/`settle`/`refund`). */
      idempotencyKey?: string | null;
    },
  ): Promise<AddCreditsResult> {
    const type = options?.type ?? "adjustment";
    this.logger.info("[CreditsService] addCredits", { amount, type, bucket: options?.bucket });
    const result = await this.store.addCredits(
      userId,
      toDecimal(amount),
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
    options?: {
      entryType?: string;
      bucket?: string | null;
      metadata?: CreditMetadata | null;
    },
  ): Promise<AddCreditsResult> {
    const entryType = options?.entryType ?? "adjustment";
    this.logger.info("[CreditsService] deductCredits", {
      amount,
      entryType,
      bucket: options?.bucket,
    });
    const result = await this.store.addCredits(
      userId,
      toDecimal(amount).neg(),
      entryType,
      options?.metadata ?? null,
      null,
      options?.bucket ?? undefined,
      undefined,
    );
    this.emit("credits.deducted", userId, {
      entryId: result.entryId,
      amount: result.amount,
      newBalance: result.newBalance,
      entryType,
    });
    await this.afterDeduction(userId, "raw", {
      entryId: result.entryId,
      userId,
      amount: result.amount.abs(),
      balanceAfter: result.newBalance,
      idempotent: result.idempotent ?? false,
    });
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
   * 3. When ``replacePrior`` (default ``true``), any remaining balance in
   *    ``bucket`` is expired immediately via a direct ``store.addCredits``
   *    negative adjustment — naturally idempotent (a replay finds the bucket
   *    already at zero and skips the call).
   * 4. The new cycle is granted via a direct ``store.addCredits`` call
   *    (bypassing {@link addCredits} so only ``credits.cycle_renewed`` fires,
   *    not a duplicate ``credits.added``), threading ``idempotencyKey`` so a
   *    redelivered webhook replays the prior grant instead of double-crediting.
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
    const replacePrior = options?.replacePrior ?? true;
    const expiresAt: Date | undefined =
      options?.ttlDays != null
        ? new Date(Date.now() + options.ttlDays * 86_400_000)
        : options?.expiresAt;
    const amountDec = toDecimal(amount);

    // Snapshot the bucket's leftover balance and lifetimePurchased BEFORE granting.
    // A redelivered webhook must be a full no-op -- including skipping the
    // replace-prior wipe below -- not just avoiding a double-grant. AddCreditsResult
    // carries no reliable cross-store "was this a replay" flag (Postgres/Supabase
    // never populate `idempotent`), so a genuine new grant is detected after the
    // fact by checking whether lifetimePurchased actually moved by `amountDec`; an
    // idempotent replay leaves it unchanged.
    let priorLeftover = new Decimal(0);
    let preLifetimePurchased = new Decimal(0);
    if (replacePrior) {
      const bucketsBefore = await this.getBucketBalances(userId);
      const current = bucketsBefore.buckets.find((t) => t.bucketKey === bucket);
      if (current) priorLeftover = current.balance;
      preLifetimePurchased = (await this.getBalance(userId)).lifetimePurchased;
    }

    let result = await this.store.addCredits(
      userId,
      amountDec,
      "purchase",
      options?.metadata,
      expiresAt,
      bucket,
      options?.idempotencyKey,
    );

    const isFreshGrant = result.lifetimePurchased.minus(preLifetimePurchased).eq(amountDec);
    // TODO: once PostgresAddCreditsResult reliably populates idempotent, use result.idempotent instead
    if (replacePrior && isFreshGrant && priorLeftover.gt(0)) {
      await this.store.addCredits(
        userId,
        priorLeftover.negated(),
        "adjustment",
        { reason: "cycle_replaced" },
        undefined,
        bucket,
      );
      // Reflect the post-replace balance so the returned result is accurate
      // (the grant call above only knows the pre-replace balance).
      result = { ...result, newBalance: (await this.getBalance(userId)).balance };
    }

    if (options?.planKey) {
      await this.setUserPlan(userId, options.planKey);
    }

    this.emit("credits.cycle_renewed", userId, {
      entryId: result.entryId,
      amount: amountDec,
      newBalance: result.newBalance,
      bucket,
      planKey: options?.planKey ?? null,
      idempotencyKey: options?.idempotencyKey ?? null,
    });

    return result;
  }

  // ── Lease lifecycle: atomic admission (interface plan §3/§4) ────────

  async reserve(
    userId: string,
    metricsOrAmount: MetricsOrAmount,
    options?: ReserveOptions,
  ): Promise<LeaseResult> {
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

  async renew(userId: string, leaseId: string, ttl?: number | null): Promise<LeaseResult> {
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

  async checkAllowance(userId: string): Promise<AllowanceResult> {
    return this.leases.checkAllowance(userId);
  }

  async runBilled<T>(
    userId: string,
    options: RunBilledOptions<T>,
  ): Promise<{ result: T; deduction: DeductionResult }> {
    return this.leases.runBilled(userId, options);
  }

  /**
   * Full deduction flow as one atomic store call (contract §2).
   *
   * 1. ``breakdown = engine.calculate(metrics)``; ``cost = breakdown.total``
   *    (exact `Decimal`, **no truncation**).
   * 2. If ``cost <= 0`` short-circuit with a zero-amount result.
   * 3. Otherwise ``store.deductWithAllowance`` consumes allowance, enforces caps,
   *    applies the balance floor and debits — idempotency-keyed end-to-end.
   *
   * On a store ``error`` a ``credits.deduct_failed`` event is emitted and a
   * typed exception is thrown (``insufficient_credits`` → InsufficientCreditsError,
   * ``cap_reached`` → CapReachedError). No success event is emitted on error.
   */
  async deduct(
    userId: string,
    metrics: UsageMetrics,
    idempotencyKey?: string | null,
    metadata?: CreditMetadata | null,
    /** Named feature to enforce/tag a per-feature invocation-count limit for. */
    feature?: string | null,
  ): Promise<DeductionResult> {
    await this.maybeLazyExpire(userId);
    this.logger.debug("[CreditsService] deduct", { model: metrics.dimensions?.model, feature });
    const engine = await this.pricing.engineForUser(userId);
    const plan = await this.store.getUserPlan(userId);
    const effectiveIdempotencyKey = idempotencyKey ?? `usage:${randomUUID()}`;

    // 1) Calculate cost — exact Decimal, never truncated (H1).
    const breakdown = engine.calculate(metrics, { rateCard: plan.rateCard ?? undefined });
    const cost = breakdown.total;

    // Build ledger metadata: caller fields FIRST, system fields LAST so the
    // system fields win (contract §5 / M7).
    const meta: Record<string, unknown> = {};
    if (metadata) {
      for (const [k, v] of Object.entries(metadata)) {
        if (v != null) meta[k] = v;
      }
    }
    meta["operation"] = metrics.operation;
    meta["measures"] = { ...(metrics.measures ?? {}) };
    meta["dimensions"] = { ...(metrics.dimensions ?? {}) };
    meta["breakdownTotal"] = breakdown.total.toString();
    meta["idempotencyKey"] = effectiveIdempotencyKey;

    const options: DeductWithAllowanceOptions = {
      idempotencyKey: effectiveIdempotencyKey,
      operation: metrics.operation,
      feature: feature ?? null,
      model: typeof metrics.dimensions?.model === "string" ? metrics.dimensions.model : null,
      region: typeof metrics.dimensions?.region === "string" ? metrics.dimensions.region : null,
      measures: { ...(metrics.measures ?? {}) },
      dimensions: { ...(metrics.dimensions ?? {}) },
      metadata: meta as CreditMetadata,
    };

    // 3) Atomic charge.
    const result = await this.store.deductWithAllowance(userId, cost, options);

    if (result.error) {
      if (result.error === "quota_exceeded") {
        await this.emitQuotaEvents(userId, effectiveIdempotencyKey);
      }
      this.logger.warn("[CreditsService] deduct failed", {
        error: result.error,
        amount: cost,
        model: metrics.dimensions?.model,
        feature,
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

    // low_balance is EDGE-triggered (M18): only fire when THIS deduction crossed
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
   * Refund a previous credit deduction.
   *
   * Returns the RefundResult (with .error set on failure). A successful refund
   * emits ``credits.refunded``; a failed/duplicate/over-refund emits
   * ``credits.refund_failed`` (no success event is ever emitted for a failed
   * refund).
   */
  /** Deduct the configured fixed cost for one named job. */
  async deductFlatJob(
    userId: string,
    jobName: string,
    idempotencyKey?: string | null,
    metadata?: CreditMetadata | null,
    feature?: string | null,
  ): Promise<DeductionResult> {
    return this.deduct(
      userId,
      { operation: jobName, measures: { jobs: 1 } },
      idempotencyKey,
      metadata,
      feature,
    );
  }

  async refundCredits(
    entryId: string,
    amount?: Decimal | number,
    reason?: string,
    metadata?: CreditMetadata | null,
    idempotencyKey?: string | null,
  ): Promise<RefundResult> {
    const refundAmount = amount != null ? toDecimal(amount) : undefined;
    this.logger.info("[CreditsService] refundCredits", { entryId, refundAmount, reason });
    const result = await this.store.refundCredits(
      entryId,
      refundAmount,
      reason,
      metadata,
      idempotencyKey,
    );

    if (result.error) {
      this.logger.warn("[CreditsService] refundCredits failed", {
        entryId,
        error: result.error,
      });
      this.emit("credits.refund_failed", result.userId, {
        entryId,
        error: result.error,
        reason: reason ?? null,
      });
      return result;
    }

    this.emit("credits.refunded", result.userId, {
      entryId,
      refundEntryId: result.refundEntryId,
      amount: result.amount,
      newBalance: result.newBalance,
      reason: reason ?? null,
    });
    return result;
  }

  /**
   * Deduct from a team's shared balance pool.
   *
   * Calculates the cost via the pricing engine (exact `Decimal`, no truncation),
   * then debits the team balance. Threads an optional ``idempotencyKey`` through
   * to the store so retried team charges are not double-counted (H12).
   */
  async deductTeam(
    teamId: string,
    userId: string,
    metrics: UsageMetrics,
    idempotencyKey?: string | null,
    metadata?: CreditMetadata | null,
  ): Promise<TeamDeductionResult> {
    await this.maybeLazyExpire(userId);
    // Lazy expiry is scoped to the individual member's credits, not the team's
    // shared pool — there's no per-team expiry concept.
    const engine = await this.pricing.engineForUser(userId);

    const breakdown = engine.calculate(metrics);
    const cost = breakdown.total;

    if (cost.lte(0)) {
      const teamBal = await this.store.getTeamBalance(teamId);
      return {
        entryId: "",
        teamId,
        userId,
        amount: new Decimal(0),
        teamBalanceAfter: teamBal.balance,
      };
    }

    const teamMetadata: CreditMetadata = {
      ...(metadata ?? {}),
      operation: metrics.operation,
      measures: { ...(metrics.measures ?? {}) },
      dimensions: Object.fromEntries(
        Object.entries(metrics.dimensions ?? {}).map(([key, value]) => [key, String(value)]),
      ),
      breakdownTotal: breakdown.total.toString(),
    };
    const result = await this.store.deductTeam(teamId, userId, cost, teamMetadata, idempotencyKey);
    // H2 fix: surface store errors — emit credits.deduct_failed and throw,
    // mirroring the Python credit service implementation. Previously returned a silent
    // success-shaped object with an .error field, so failed charges looked OK.
    if (result.error) {
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
