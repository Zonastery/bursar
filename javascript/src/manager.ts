import { InsufficientCreditsError, PricingNotLoadedError } from "./errors.js";
import type { PricingEngine } from "./engine.js";
import { PricingEngine as PricingEngineClass } from "./engine.js";
import type {
  AddCreditsResult,
  AggregateStats,
  BalanceResult,
  CreditMetadata,
  DailySpendRow,
  DeductionResult,
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
import type { CreditStore } from "./stores/credit-store.js";
import type { CreditEventEmitter, CreditEventType } from "./stores/events.js";
import type { UsageMetrics } from "./metrics.js";

/**
 * Orchestrates credit operations: pricing -> reserve -> deduct.
 *
 * Optionally accepts a ``CreditEventEmitter`` to emit lifecycle events
 * (deducted, added, refunded, expired, cap_reached, cap_warning, low_balance).
 */
export class CreditManager {
  private store: CreditStore;
  private engine: PricingEngine | null = null;
  private emitter: CreditEventEmitter | null = null;

  constructor(
    store: CreditStore,
    engine?: PricingEngine | null,
    emitter?: CreditEventEmitter | null,
  ) {
    this.store = store;
    if (engine) this.engine = engine;
    if (emitter) this.emitter = emitter;
  }

  /** Emit a credit lifecycle event. No-op if no emitter is configured. */
  private emit(type: CreditEventType, userId: string, data?: Record<string, unknown>): void {
    this.emitter?.emit({ type, timestamp: new Date(), userId, data });
  }

  /** Run bundled SQL migrations through the store. */
  async setup(): Promise<SetupResult> {
    return await this.store.setup();
  }

  /** Load pricing from a PricingConfigData or raw dict and sync it. */
  publishPricingFromDict(data: PricingConfigData | Record<string, unknown>): void {
    const raw = data as Record<string, unknown>;
    this.engine = PricingEngineClass.fromDict(raw);
    void this.store.setActivePricing(data as PricingConfigData);
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

  /** Publish new pricing and update the engine in one call. */
  publishPricing(config: PricingConfigData, label?: string | null): void {
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
    void this.store.setActivePricing(config, label);
  }

  /** The current PricingEngine, or null if not loaded. */
  get pricingEngine(): PricingEngine | null {
    return this.engine;
  }

  /** Get a user's current credit balance. */
  async getBalance(userId: string): Promise<BalanceResult> {
    return await this.store.getBalance(userId);
  }

  /** Add credits to a user's account. */
  async addCredits(
    userId: string,
    amount: number,
    type = "adjustment",
    metadata?: CreditMetadata | null,
    expiresAt?: Date | null,
  ): Promise<AddCreditsResult> {
    const result = await this.store.addCredits(userId, amount, type, metadata, expiresAt);
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
    amount: number,
    operationType = "usage",
    metadata?: CreditMetadata | null,
    minBalance?: number | null,
  ): Promise<ReserveResult> {
    const actual = minBalance ?? this.engine?.minBalance ?? 5;
    return await this.store.reserveCredits(userId, amount, operationType, metadata, actual);
  }

  /**
   * Full deduction flow: calculate -> reserve -> deduct.
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

    const breakdown = this.engine.calculate(metrics);
    let cost = breakdown.total > 0 ? Math.trunc(breakdown.total) : 0;

    // --- Plan allowance check ---
    // Consume free plan allowance before deducting from balance
    if (cost > 0) {
      const allowance = await this.store.checkAllowance(userId);
      if (allowance.allowanceRemaining > 0) {
        const consumeFromAllowance = Math.min(cost, allowance.allowanceRemaining);
        await this.store.incrementUsageWindow(userId, allowance.planId, consumeFromAllowance);
        cost -= consumeFromAllowance;
      }
    }

    if (cost <= 0) {
      // Fully covered by plan allowance — no balance deduction
      const balance = await this.store.getBalance(userId);
      const result: DeductionResult = {
        transactionId: "",
        userId,
        amount: 0,
        balanceAfter: balance.balance,
        idempotent: false,
      };
      this.emit("credits.deducted", userId, {
        amount: 0,
        balanceAfter: balance.balance,
        planCovered: true,
      });
      return result;
    }

    // ── Spend cap check ────────────────────────────────────────────────
    const capResult = await this.store.checkSpendCap(userId, metrics.model ?? null, cost);
    if (capResult.action === "deny") {
      this.emit("credits.cap_reached", userId, {
        currentSpend: capResult.currentSpend,
        limit: capResult.limit,
        model: capResult.model ?? undefined,
        amount: cost,
      });
      throw new InsufficientCreditsError(
        `Spend cap exceeded: ${capResult.currentSpend}/${capResult.limit}${capResult.model ? " (" + capResult.model + ")" : ""}`,
      );
    }
    if (capResult.action === "warn" || capResult.action === "notify") {
      this.emit("credits.cap_warning", userId, {
        currentSpend: capResult.currentSpend,
        limit: capResult.limit,
        model: capResult.model ?? undefined,
        amount: cost,
        action: capResult.action,
      });
    }

    const metaBase: Record<string, unknown> = {
      inputTokens: metrics.inputTokens ?? 0,
      outputTokens: metrics.outputTokens ?? 0,
      model: metrics.model ?? "unknown",
      breakdownTotal: breakdown.total,
    };
    if (metrics.fixedJob) metaBase["fixedJob"] = metrics.fixedJob;
    if (idempotencyKey) metaBase["idempotencyKey"] = idempotencyKey;
    if (metadata) {
      for (const [k, v] of Object.entries(metadata)) {
        if (v != null) metaBase[k] = v;
      }
    }

    const reserveResult = await this.store.reserveCredits(
      userId,
      cost,
      metrics.fixedJob ?? "usage",
      metaBase as CreditMetadata,
      this.engine.minBalance,
    );

    if (reserveResult.error) throw new InsufficientCreditsError(reserveResult.error);

    const deduction = await this.store.deductCredits(
      userId,
      reserveResult.reservationId,
      cost,
      idempotencyKey,
      metaBase as CreditMetadata,
    );

    if (deduction.error) throw new InsufficientCreditsError(deduction.error);

    this.emit("credits.deducted", userId, {
      transactionId: deduction.transactionId,
      amount: deduction.amount,
      balanceAfter: deduction.balanceAfter,
      model: metrics.model ?? null,
    });

    // Emit low_balance when balance after deduct is at or below minBalance * 2
    const minBal = this.engine?.minBalance ?? 5;
    if (deduction.balanceAfter <= minBal * 2) {
      this.emit("credits.low_balance", userId, {
        balance: deduction.balanceAfter,
        minBalance: minBal,
      });
    }

    return deduction;
  }

  /**
   * Refund a previous credit deduction.
   */
  async refundCredits(
    transactionId: string,
    amount?: number,
    reason?: string,
    metadata?: CreditMetadata | null,
  ): Promise<RefundResult> {
    const result = await this.store.refundCredits(transactionId, amount, reason, metadata);
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
   * Calculates cost via the pricing engine, then debits the team balance.
   */
  async deductTeam(
    teamId: string,
    userId: string,
    metrics: UsageMetrics,
    metadata?: CreditMetadata | null,
  ): Promise<TeamDeductionResult> {
    if (!this.engine)
      throw new PricingNotLoadedError(
        "pricing not loaded: call loadPricingFromStore or publishPricing first",
      );

    const breakdown = this.engine.calculate(metrics);
    const cost = breakdown.total > 0 ? Math.trunc(breakdown.total) : 0;

    if (cost <= 0) {
      const teamBal = await this.store.getTeamBalance(teamId);
      return {
        transactionId: "",
        teamId,
        userId,
        amount: 0,
        teamBalanceAfter: teamBal.balance,
      };
    }

    const result = await this.store.deductTeam(teamId, userId, cost, metadata);
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
   */
  async deductFixed(
    userId: string,
    jobName: string,
    idempotencyKey?: string | null,
    metadata?: CreditMetadata | null,
  ): Promise<DeductionResult> {
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

  /** Top users by spend in a time window with limit. */
  async topUsers(limit: number, start: Date, end: Date): Promise<TopUserRow[]> {
    return await this.store.topUsers(limit, start, end);
  }

  /** Daily spend aggregation in a time window. */
  async dailySpend(start: Date, end: Date): Promise<DailySpendRow[]> {
    return await this.store.dailySpend(start, end);
  }
}
