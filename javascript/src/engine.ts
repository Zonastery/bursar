import Decimal from "decimal.js";
import { evaluateExpression } from "./expr.js";
import type { BursarConfig } from "./config.js";
import { loadConfigFromDict } from "./config.js";
import type { CostBreakdown } from "./breakdown.js";
import { makeCostBreakdown } from "./breakdown.js";
import type { UsageMetrics } from "./metrics.js";
import { ConfigError } from "./errors.js";

/**
 * Credit calculation engine.
 *
 * Evaluates pricing expressions against usage metrics to produce cost
 * breakdowns. All money values are exact `Decimal`s — never binary `number`.
 */
export class PricingEngine {
  private config: BursarConfig;

  constructor(config: BursarConfig) {
    this.config = config;
  }

  /** Load engine from a config dictionary. */
  static fromDict(data: Record<string, unknown>): PricingEngine {
    return new PricingEngine(loadConfigFromDict(data));
  }

  /** Calculate credit cost for a single usage event. */
  calculate(metrics: UsageMetrics, rateOverrides?: Record<string, string> | null): CostBreakdown {
    const variables = this.buildVariables(metrics);
    const modelCredits = this.calcModel(metrics.model ?? null, variables, rateOverrides);
    const toolCredits = this.calcTools(metrics, variables);
    const searchCredits = this.calcSearch(variables);
    const cacheSavings = this.calcCache(variables);
    const fixedCredits = this.calcFlatJobs(metrics);

    // makeCostBreakdown quantizes every component to 4dp HALF_UP and computes
    // the single-source-of-truth total (clamped at 0). No truncation.
    return makeCostBreakdown({
      modelCredits,
      toolCredits,
      searchCredits,
      cacheSavings,
      fixedCredits,
      breakdown: {
        model: metrics.model ?? "unknown",
        inputTokens: metrics.inputTokens ?? 0,
        outputTokens: metrics.outputTokens ?? 0,
        toolCount: (metrics.toolCalls ?? []).length,
      },
    });
  }

  /** Calculate credit costs for multiple usage events. */
  calculateBatch(metricsList: UsageMetrics[]): CostBreakdown[] {
    return metricsList.map((m) => this.calculate(m));
  }

  /** Return the pricing config as a dict. */
  pricingSchema(): Record<string, unknown> {
    return {
      version: this.config.version,
      metering: {
        models: { ...this.config.metering.models },
        tools:
          Object.keys(this.config.metering.tools).length > 0
            ? { ...this.config.metering.tools }
            : null,
        search: this.config.metering.search ?? null,
        cacheDiscount: this.config.metering.cacheDiscount ?? null,
        flatJobs:
          Object.keys(this.config.metering.flatJobs).length > 0
            ? { ...this.config.metering.flatJobs }
            : null,
      },
      ledger: {
        minBalance: this.config.ledger.minBalance,
        signupGrant: this.config.ledger.signupGrant ?? null,
        buckets: this.config.ledger.buckets ? { ...this.config.ledger.buckets } : null,
      },
      plans: this.config.plans ? { ...this.config.plans } : null,
      billing: this.config.billing ? { ...this.config.billing } : null,
    };
  }

  /** Minimum balance users must keep. */
  get minBalance(): Decimal {
    return this.config.ledger.minBalance;
  }

  /** The canonical set of metric variable names usable in expressions. */
  get knownVariables(): Set<string> {
    return new Set(Object.keys(this.buildVariables({})));
  }

  /** Check if a model name exists in the pricing config. */
  hasModel(modelName: string): boolean {
    return Object.prototype.hasOwnProperty.call(this.config.metering.models, modelName);
  }

  /** Resolve a model version string to a pricing config key. */
  resolveModel(modelVersion: string): string | null {
    const models = this.config.metering.models;
    if (Object.prototype.hasOwnProperty.call(models, modelVersion)) return modelVersion;
    for (const key of Object.keys(models)) {
      if (key !== "*" && modelVersion.startsWith(key)) return key;
    }
    if (Object.prototype.hasOwnProperty.call(models, "*")) return "*";
    return null;
  }

  /**
   * Get the flat job credit cost for a named batch job, as a `Decimal`.
   * Returns `null` for an unknown job (L3 parity with Python). The amount is
   * NOT truncated to an integer.
   */
  getFlatJobCost(jobName: string): Decimal | null {
    if (Object.prototype.hasOwnProperty.call(this.config.metering.flatJobs, jobName)) {
      return this.config.metering.flatJobs[jobName];
    }
    return null;
  }

  // ── Internal ──

  private buildVariables(metrics: UsageMetrics): Record<string, number> {
    return {
      input_tokens: metrics.inputTokens ?? 0,
      output_tokens: metrics.outputTokens ?? 0,
      cache_read_tokens: metrics.cacheReadTokens ?? 0,
      cache_write_tokens: metrics.cacheWriteTokens ?? 0,
      tool_calls: (metrics.toolCalls ?? []).length,
      search_queries: metrics.searchQueries ?? 0,
      search_results: metrics.searchResults ?? 0,
      web_search_calls: metrics.webSearchCalls ?? 0,
      code_exec_calls: metrics.codeExecCalls ?? 0,
    };
  }

  private calcModel(
    modelName: string | null,
    variables: Record<string, number>,
    rateOverrides?: Record<string, string> | null,
  ): Decimal {
    const name = modelName === null || modelName === "none" ? "*" : modelName;

    if (rateOverrides) {
      const resolved = this.resolveModel(name);
      if (resolved && Object.prototype.hasOwnProperty.call(rateOverrides, resolved)) {
        return evaluateExpression(rateOverrides[resolved], variables);
      }
      if (Object.prototype.hasOwnProperty.call(rateOverrides, "*")) {
        return evaluateExpression(rateOverrides["*"], variables);
      }
    }

    const models = this.config.metering.models;
    let expr: string | undefined;

    if (Object.prototype.hasOwnProperty.call(models, name)) {
      expr = models[name];
    } else if (Object.prototype.hasOwnProperty.call(models, "*")) {
      expr = models["*"];
    } else if (Object.prototype.hasOwnProperty.call(models, "_default")) {
      expr = models["_default"];
    }

    if (!expr) {
      throw new ConfigError(`model '${modelName}' not found and no "*" configured`);
    }

    return evaluateExpression(expr, variables);
  }

  private calcTools(metrics: UsageMetrics, variables: Record<string, number>): Decimal {
    const tools = this.config.metering.tools;
    const defaultExpr = tools["*"] ?? "tool_calls * 0";
    let total = new Decimal(0);
    const seenSpecific = new Set<string>();

    const calls = metrics.toolCalls ?? [];
    const uniqueNames = [...new Set(calls.map((t) => t.name))];

    // WS2: `tool_calls` always means the GLOBAL total across all tools — it is
    // never overridden. Each branch instead gets its own `calls`,
    // scoped to just that branch's call count, alongside the unchanged globals.
    for (const toolName of uniqueNames) {
      if (Object.prototype.hasOwnProperty.call(tools, toolName)) {
        const thisToolCalls = calls.filter((t) => t.name === toolName).length;
        const local = { ...variables, calls: thisToolCalls };
        total = total.plus(evaluateExpression(tools[toolName], local));
        seenSpecific.add(toolName);
      }
    }

    // Count unknown *calls* (not unique names) for the default expression, to
    // match the Python engine's `sum(1 for t in tool_calls if t.name not in
    // seen_specific)`. Previously this counted unique names, diverging from
    // Python and (masked by 2dp rounding) under-charging on repeated unknowns.
    const unknownCount = calls.filter((t) => !seenSpecific.has(t.name)).length;
    if (unknownCount > 0) {
      const local = { ...variables, calls: unknownCount };
      total = total.plus(evaluateExpression(defaultExpr, local));
    }

    return total;
  }

  private calcSearch(variables: Record<string, number>): Decimal {
    if (this.config.metering.search) {
      return evaluateExpression(this.config.metering.search, variables);
    }
    return new Decimal(0);
  }

  private calcCache(variables: Record<string, number>): Decimal {
    if (this.config.metering.cacheDiscount) {
      // cacheDiscount is the savings expression; negate so it reduces cost.
      return evaluateExpression(this.config.metering.cacheDiscount, variables).negated();
    }
    return new Decimal(0);
  }

  private calcFlatJobs(metrics: UsageMetrics): Decimal {
    if (
      metrics.flatJob &&
      Object.prototype.hasOwnProperty.call(this.config.metering.flatJobs, metrics.flatJob)
    ) {
      return this.config.metering.flatJobs[metrics.flatJob];
    }
    return new Decimal(0);
  }
}
