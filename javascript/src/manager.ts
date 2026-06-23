import { InsufficientCreditsError, PricingNotLoadedError } from "./errors.js";
import type { PricingEngine } from "./engine.js";
import { PricingEngine as PricingEngineClass } from "./engine.js";
import type {
  AddCreditsResult,
  BalanceResult,
  CreditMetadata,
  DeductionResult,
  PricingConfigData,
  ReserveResult,
  SetupResult,
} from "./types.js";
import type { CreditStore } from "./stores/credit-store.js";
import { loadConfigFromDict } from "./config.js";
import type { UsageMetrics } from "./metrics.js";

/**
 * Orchestrates credit operations: pricing -> reserve -> deduct.
 */
export class CreditManager {
  private store: CreditStore;
  private engine: PricingEngine | null = null;

  constructor(store: CreditStore, engine?: PricingEngine | null) {
    this.store = store;
    if (engine) this.engine = engine;
  }

  /** Run bundled SQL migrations through the store. */
  async setup(): Promise<SetupResult> {
    return await this.store.setup();
  }

  /** Load pricing from a PricingConfigData or raw dict and sync it. */
  publishPricingFromDict(data: PricingConfigData | Record<string, unknown>): void {
    const raw =
      "models" in data && data.models != null
        ? (data as Record<string, unknown>)
        : (data as Record<string, unknown>);

    this.engine = PricingEngineClass.fromDict(raw);
    void this.store.setActivePricing(
      "version" in data ? (data as PricingConfigData) : loadConfigFromDict(raw),
    );
  }

  /** Load the active pricing config from the store. */
  async loadPricingFromStore(): Promise<void> {
    const active = await this.store.getActivePricing();
    if (!active) throw new PricingNotLoadedError("no active pricing config in store");

    const { version, models, tools, search, cache, fixed, minBalance } = active.config;
    const engineDict: Record<string, unknown> = {
      version,
      models,
      tools: tools ?? { _default: "tool_calls * 0" },
      search: search ?? {},
      cache: cache ?? {},
      fixed: fixed ?? {},
      minBalance: minBalance ?? 5,
    };

    this.engine = PricingEngineClass.fromDict(engineDict);
  }

  /** Publish new pricing and update the engine in one call. */
  publishPricing(config: PricingConfigData, label?: string | null): void {
    const { version, models, tools, search, cache, fixed, minBalance } = config;
    const raw: Record<string, unknown> = {
      version,
      models,
      tools: tools ?? { _default: "tool_calls * 0" },
      search: search ?? {},
      cache: cache ?? {},
      fixed: fixed ?? {},
      minBalance: minBalance ?? 5,
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
  ): Promise<AddCreditsResult> {
    return await this.store.addCredits(userId, amount, type, metadata);
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
      return {
        transactionId: "",
        userId,
        amount: 0,
        balanceAfter: balance.balance,
        idempotent: false,
      };
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
    return deduction;
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
}
