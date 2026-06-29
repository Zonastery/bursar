import Decimal from "decimal.js";
import {
  CapReachedError,
  ConfigError,
  InsufficientCreditsError,
  PricingNotLoadedError,
  RefundError,
} from "./errors.js";
import type { PricingEngine } from "./engine.js";
import { PricingEngine as PricingEngineClass } from "./engine.js";
import type {
  AddCreditsResult,
  AggregateStats,
  BalanceResult,
  CheckFeatureResult,
  CreditMetadata,
  DailySpendRow,
  DeductionResult,
  DeductWithAllowanceOptions,
  GetUserPlanResult,
  PricingConfigData,
  RefundResult,
  ReserveResult,
  SetupResult,
  SpendByModelRow,
  SpendByUserRow,
  SweepResult,
  TeamDeductionResult,
  TopUserRow,
} from "./types.js";
import type {
  ListTransactionsOptions,
  ListUsageEventsOptions,
  PaginatedTransactions,
} from "./types.js";
import type { CreditStore } from "./stores/credit-store.js";
import type { CreditEventEmitter, CreditEventType } from "./stores/events.js";
import type { UsageMetrics } from "./metrics.js";

/**
 * Default `low_balance` threshold multiplier (contract §6 / M18). The event
 * fires when a deduction crosses ``minBalance * LOW_BALANCE_MULTIPLIER`` from
 * above. Override via the ``lowBalanceThreshold`` constructor option to set an
 * absolute threshold instead.
 */
const LOW_BALANCE_MULTIPLIER = 2;

/** Coerce a `Decimal | number` money input into a `Decimal`. */
function toDecimal(value: Decimal | number): Decimal {
  return value instanceof Decimal ? value : new Decimal(value);
}

/** Optional behavioural knobs for the manager. */
export interface CreditManagerOptions {
  /**
   * Absolute balance threshold for the ``credits.low_balance`` event. When
   * omitted, the manager uses ``engine.minBalance * 2`` (documented default,
   * M18). The event is **edge-triggered**: it fires only on the deduction that
   * crosses the threshold from above, never repeatedly while already below it.
   */
  lowBalanceThreshold?: Decimal | number | null;
}

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
export class CreditManager {
  private store: CreditStore;
  private engine: PricingEngine | null = null;
  private emitter: CreditEventEmitter | null = null;
  private lowBalanceThreshold: Decimal | null;

  constructor(
    store: CreditStore,
    engine?: PricingEngine | null,
    emitter?: CreditEventEmitter | null,
    options?: CreditManagerOptions | null,
  ) {
    this.store = store;
    if (engine) this.engine = engine;
    if (emitter) this.emitter = emitter;
    this.lowBalanceThreshold =
      options?.lowBalanceThreshold != null ? toDecimal(options.lowBalanceThreshold) : null;
  }

  /** Emit a credit lifecycle event. No-op if no emitter is configured. */
  private emit(type: CreditEventType, userId: string, data?: Record<string, unknown>): void {
    this.emitter?.emit({ type, timestamp: new Date(), userId, data });
  }

  /** The configured min-balance floor as a `Decimal` (defaults to 5). */
  private minBalanceDecimal(): Decimal {
    return new Decimal(this.engine?.minBalance ?? 5);
  }

  /**
   * Resolve the `low_balance` threshold: the explicit override when configured,
   * otherwise ``minBalance * LOW_BALANCE_MULTIPLIER`` (documented default).
   */
  private resolveLowBalanceThreshold(): Decimal {
    return this.lowBalanceThreshold ?? this.minBalanceDecimal().times(LOW_BALANCE_MULTIPLIER);
  }

  /** Run bundled SQL migrations through the store. */
  async setup(): Promise<SetupResult> {
    return await this.store.setup();
  }

  /** Load pricing from a PricingConfigData or raw dict and sync it. */
  async publishPricingFromDict(data: PricingConfigData | Record<string, unknown>): Promise<void> {
    const raw = data as Record<string, unknown>;
    this.engine = PricingEngineClass.fromDict(raw);
    await this.store.setActivePricing(data as PricingConfigData);
  }

  /** Load the active pricing config from the store. */
  async loadPricingFromStore(): Promise<void> {
    const active = await this.store.getActivePricing();
    if (!active) throw new PricingNotLoadedError("no active pricing config in store");

    const { models, tools, search, cache, fixed, minBalance, plans } = active.config;
    const engineDict: Record<string, unknown> = {
      models,
      tools: tools ?? { _default: "tool_calls * 0" },
      search: search ?? {},
      cache: cache ?? {},
      fixed: fixed ?? {},
      minBalance: minBalance ?? 5,
      ...(plans ? { plans } : {}),
    };

    this.engine = PricingEngineClass.fromDict(engineDict);
  }

  /**
   * Publish new pricing and update the engine in one call.
   *
   * H10: the store write is now **awaited** (was a fire-and-forget `void`), so
   * a persistence failure surfaces to the caller instead of becoming an
   * unhandled promise rejection.
   */
  async publishPricing(config: PricingConfigData, label?: string | null): Promise<void> {
    const { models, tools, search, cache, fixed, minBalance, plans } = config;
    const raw: Record<string, unknown> = {
      models,
      tools: tools ?? { _default: "tool_calls * 0" },
      search: search ?? {},
      cache: cache ?? {},
      fixed: fixed ?? {},
      minBalance: minBalance ?? 5,
      ...(plans ? { plans } : {}),
    };
    this.engine = PricingEngineClass.fromDict(raw);
    await this.store.setActivePricing(config, label);
  }

  /** The current PricingEngine, or null if not loaded. */
  get pricingEngine(): PricingEngine | null {
    return this.engine;
  }

  /** Fetch a user's current plan (including feature entitlements). */
  async getUserPlan(userId: string): Promise<GetUserPlanResult> {
    return this.store.getUserPlan(userId);
  }

  /**
   * Check whether a user's plan has a specific feature entitlement.
   *
   * Passthrough to the store, which distinguishes *presence* from *truthiness*
   * (numeric `0` / `""` count as present; only `null`/`undefined`/`false`/absent
   * read as missing — contract §5 / M6).
   */
  async checkFeature(userId: string, feature: string): Promise<CheckFeatureResult> {
    return this.store.checkFeature(userId, feature);
  }

  /** Get a user's current credit balance. */
  async getBalance(userId: string): Promise<BalanceResult> {
    return await this.store.getBalance(userId);
  }

  /** Add credits to a user's account. */
  async addCredits(
    userId: string,
    amount: Decimal | number,
    type = "adjustment",
    metadata?: CreditMetadata | null,
    expiresAt?: Date | null,
  ): Promise<AddCreditsResult> {
    const result = await this.store.addCredits(userId, toDecimal(amount), type, metadata, expiresAt);
    this.emit("credits.added", userId, {
      transactionId: result.transactionId,
      amount: result.amount,
      newBalance: result.newBalance,
      type,
    });
    return result;
  }

  /** Reserve credits for an upcoming operation. */
  async reserveCredits(
    userId: string,
    amount: Decimal | number,
    operationType = "usage",
    metadata?: CreditMetadata | null,
    minBalance?: Decimal | number | null,
  ): Promise<ReserveResult> {
    const actual = minBalance != null ? toDecimal(minBalance) : this.minBalanceDecimal();
    return await this.store.reserveCredits(
      userId,
      toDecimal(amount),
      operationType,
      metadata,
      actual,
    );
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
  ): Promise<DeductionResult> {
    if (!this.engine)
      throw new PricingNotLoadedError(
        "pricing not loaded: call loadPricingFromStore or publishPricing first",
      );

    // 1) Calculate cost — exact Decimal, never truncated (H1).
    const breakdown = this.engine.calculate(metrics);
    const cost = breakdown.total;

    // 2) Zero-amount short-circuit (no balance touch, no store round-trip).
    if (cost.lte(0)) {
      const balance = await this.store.getBalance(userId);
      const result: DeductionResult = {
        transactionId: "",
        userId,
        amount: new Decimal(0),
        allowanceConsumed: new Decimal(0),
        balanceAfter: balance.balance,
        idempotent: false,
        capWarning: null,
      };
      this.emit("credits.deducted", userId, {
        amount: new Decimal(0),
        balanceAfter: balance.balance,
        planCovered: false,
      });
      return result;
    }

    // Build transaction metadata: caller fields FIRST, system fields LAST so the
    // system fields win (contract §5 / M7).
    const meta: Record<string, unknown> = {};
    if (metadata) {
      for (const [k, v] of Object.entries(metadata)) {
        if (v != null) meta[k] = v;
      }
    }
    meta["inputTokens"] = metrics.inputTokens ?? 0;
    meta["outputTokens"] = metrics.outputTokens ?? 0;
    meta["model"] = metrics.model ?? "unknown";
    meta["breakdownTotal"] = breakdown.total.toString();
    if (metrics.fixedJob) meta["fixedJob"] = metrics.fixedJob;
    if (idempotencyKey) meta["idempotencyKey"] = idempotencyKey;

    const options: DeductWithAllowanceOptions = {
      idempotencyKey: idempotencyKey ?? null,
      minBalance: this.minBalanceDecimal(),
      model: metrics.model ?? null,
      metadata: meta as CreditMetadata,
    };

    // 3) Atomic charge.
    const result = await this.store.deductWithAllowance(userId, cost, options);

    if (result.error) {
      this.emit("credits.deduct_failed", userId, {
        error: result.error,
        amount: cost,
        model: metrics.model ?? null,
      });
      if (result.error === "cap_reached") {
        throw new CapReachedError(`Spend cap exceeded for user ${userId} (requested ${cost})`);
      }
      // insufficient_credits, invalid_amount, and any other business error.
      throw new InsufficientCreditsError(
        `Credit deduction failed: ${result.error}. user=${userId}, requested=${cost}`,
      );
    }

    // Success — emit deducted, then any cap warning, then edge-triggered low-balance.
    this.emit("credits.deducted", userId, {
      transactionId: result.transactionId,
      amount: result.amount,
      allowanceConsumed: result.allowanceConsumed,
      balanceAfter: result.balanceAfter,
      model: metrics.model ?? null,
      idempotent: result.idempotent,
    });

    if (result.capWarning) {
      this.emit("credits.cap_warning", userId, {
        action: result.capWarning,
        amount: result.amount,
        model: metrics.model ?? null,
      });
    }

    // low_balance is EDGE-triggered (M18): only fire when THIS deduction crossed
    // the threshold. A replayed (idempotent) result did not move the balance, so
    // it never crosses. balanceBefore = balanceAfter + amount charged.
    if (!result.idempotent) {
      const threshold = this.resolveLowBalanceThreshold();
      const balanceBefore = result.balanceAfter.plus(result.amount);
      if (result.balanceAfter.lte(threshold) && balanceBefore.gt(threshold)) {
        this.emit("credits.low_balance", userId, {
          balance: result.balanceAfter,
          threshold,
          minBalance: this.minBalanceDecimal(),
        });
      }
    }

    return result;
  }

  /**
   * Refund a previous credit deduction.
   *
   * H3: the store's ``error`` is checked **before** emitting. A successful refund
   * emits ``credits.refunded``; a failed/duplicate/over-refund emits
   * ``credits.refund_failed`` and throws a typed ``RefundError`` (no success
   * event is ever emitted for a failed refund).
   */
  async refundCredits(
    transactionId: string,
    amount?: Decimal | number,
    reason?: string,
    metadata?: CreditMetadata | null,
  ): Promise<RefundResult> {
    const refundAmount = amount != null ? toDecimal(amount) : undefined;
    const result = await this.store.refundCredits(transactionId, refundAmount, reason, metadata);

    if (result.error) {
      this.emit("credits.refund_failed", result.userId, {
        transactionId,
        error: result.error,
        reason: reason ?? null,
      });
      throw new RefundError(
        `Refund failed: ${result.error}. transaction=${transactionId}`,
      );
    }

    this.emit("credits.refunded", result.userId, {
      transactionId,
      refundTransactionId: result.refundTransactionId,
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
    if (!this.engine)
      throw new PricingNotLoadedError(
        "pricing not loaded: call loadPricingFromStore or publishPricing first",
      );

    const breakdown = this.engine.calculate(metrics);
    const cost = breakdown.total;

    if (cost.lte(0)) {
      const teamBal = await this.store.getTeamBalance(teamId);
      return {
        transactionId: "",
        teamId,
        userId,
        amount: new Decimal(0),
        teamBalanceAfter: teamBal.balance,
      };
    }

    const result = await this.store.deductTeam(teamId, userId, cost, metadata, idempotencyKey);
    if (!result.error) {
      this.emit("credits.deducted", userId, {
        transactionId: result.transactionId,
        amount: result.amount,
        teamBalanceAfter: result.teamBalanceAfter,
        teamId,
        deductType: "team",
      });
    }
    return result;
  }

  /**
   * Shortcut for fixed-cost batch jobs.
   *
   * L1: an unknown/typo'd ``jobName`` is rejected (throws) instead of silently
   * charging 0 credits — ``engine.getFixedCost(jobName) === null`` means the job
   * is not configured.
   */
  async deductFixed(
    userId: string,
    jobName: string,
    idempotencyKey?: string | null,
    metadata?: CreditMetadata | null,
  ): Promise<DeductionResult> {
    if (!this.engine)
      throw new PricingNotLoadedError(
        "pricing not loaded: call loadPricingFromStore or publishPricing first",
      );
    if (this.engine.getFixedCost(jobName) === null) {
      throw new ConfigError(
        `unknown fixed job '${jobName}': not configured in pricing 'fixed' section`,
      );
    }
    return await this.deduct(userId, { fixedJob: jobName }, idempotencyKey, metadata);
  }

  /**
   * Sweep expired credits from all users' balances.
   *
   * When ``dryRun`` is true, reports what would be expired without modifying
   * any balances.
   */
  async sweepExpiredCredits(dryRun?: boolean): Promise<SweepResult> {
    const result = await this.store.sweepExpiredCredits(dryRun);
    if (!dryRun && result.expiredCount > 0) {
      this.emit("credits.expired", "system", {
        expiredCount: result.expiredCount,
        expiredAmount: result.expiredAmount,
      });
    }
    return result;
  }

  // ── Usage analytics ──────────────────────────────────────────────────

  /** Aggregate statistics across all users in a time window. */
  async aggregateStats(start: Date, end: Date): Promise<AggregateStats> {
    return await this.store.aggregateStats(start, end);
  }

  /** Aggregate spend by user in a time window. */
  async spendByUser(start: Date, end: Date): Promise<SpendByUserRow[]> {
    return await this.store.spendByUser(start, end);
  }

  /** Aggregate spend by model in a time window. */
  async spendByModel(start: Date, end: Date): Promise<SpendByModelRow[]> {
    return await this.store.spendByModel(start, end);
  }

  /** List all user credit transactions with pagination. */
  async listUserTransactions(
    userId: string,
    options?: ListTransactionsOptions,
  ): Promise<PaginatedTransactions> {
    return await this.store.listUserTransactions(userId, options);
  }

  async listUsageEvents(
    userId: string,
    options?: ListUsageEventsOptions,
  ): Promise<PaginatedTransactions> {
    return await this.store.listUsageEvents(userId, options);
  }

  /** Top users by spend in a time window with limit. */
  async topUsers(limit: number, start: Date, end: Date): Promise<TopUserRow[]> {
    return await this.store.topUsers(limit, start, end);
  }

  /** Daily spend aggregation in a time window. */
  async dailySpend(start: Date, end: Date): Promise<DailySpendRow[]> {
    return await this.store.dailySpend(start, end);
  }
}
