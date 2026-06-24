import { evaluateExpression } from "./expr.js";
import type { PricingConfig } from "./config.js";
import { loadConfigFromDict } from "./config.js";
import type { CostBreakdown } from "./breakdown.js";
import { makeCostBreakdown } from "./breakdown.js";
import type { UsageMetrics } from "./metrics.js";
import type { PricingConfigData } from "./types.js";
import { ConfigError } from "./errors.js";

function safeTotal(value: number): number {
  return Math.max(0, Math.round(value * 100) / 100);
}

/**
 * Credit calculation engine.
 *
 * Evaluates pricing expressions against usage metrics to produce cost breakdowns.
 */
export class PricingEngine {
  private config: PricingConfig;

  constructor(config: PricingConfig) {
    this.config = config;
  }

  /** Load engine from a config dictionary. */
  static fromDict(data: Record<string, unknown>): PricingEngine {
    return new PricingEngine(loadConfigFromDict(data));
  }

  /** Calculate credit cost for a single usage event. */
  calculate(metrics: UsageMetrics): CostBreakdown {
    const variables = this.buildVariables(metrics);
    const modelCredits = this.calcModel(metrics.model ?? null, variables);
    const toolCredits = this.calcTools(metrics, variables);
    const searchCredits = this.calcSearch(variables);
    const cacheSavings = this.calcCache(variables);
    const fixedCredits = this.calcFixed(metrics);

    return makeCostBreakdown({
      modelCredits: safeTotal(modelCredits),
      toolCredits: safeTotal(toolCredits),
      searchCredits: safeTotal(searchCredits),
      cacheSavings: Math.round(cacheSavings * 100) / 100,
      fixedCredits: safeTotal(fixedCredits),
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

  /** Return the pricing config as a typed model. */
  pricingSchema(): PricingConfigData {
    return {
      models: { ...this.config.models },
      tools: Object.keys(this.config.tools).length > 0 ? { ...this.config.tools } : null,
      search: Object.keys(this.config.search).length > 0 ? { ...this.config.search } : null,
      cache: Object.keys(this.config.cache).length > 0 ? { ...this.config.cache } : null,
      fixed: Object.keys(this.config.fixed).length > 0 ? { ...this.config.fixed } : null,
      minBalance: this.config.minBalance,
      plans: this.config.plans ? { ...this.config.plans } : null,
    };
  }

  /** Minimum balance users must keep. */
  get minBalance(): number {
    return this.config.minBalance;
  }

  /** Check if a model name exists in the pricing config. */
  hasModel(modelName: string): boolean {
    return modelName in this.config.models;
  }

  /** Resolve a model version string to a pricing config key. */
  resolveModel(modelVersion: string): string | null {
    if (modelVersion in this.config.models) return modelVersion;
    for (const key of Object.keys(this.config.models)) {
      if (key !== "_default" && modelVersion.startsWith(key)) return key;
    }
    if ("_default" in this.config.models) return "_default";
    return null;
  }

  /** Get the fixed credit cost for a named batch job. */
  getFixedCost(jobName: string): number | null {
    if (jobName in this.config.fixed) return this.config.fixed[jobName];
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

  private calcModel(modelName: string | null, variables: Record<string, number>): number {
    const name = modelName === null || modelName === "none" ? "_default" : modelName;
    let expr: string | undefined;

    if (name in this.config.models) {
      expr = this.config.models[name];
    } else if ("_default" in this.config.models) {
      expr = this.config.models["_default"];
    }

    if (!expr) {
      throw new ConfigError(`model '${modelName}' not found and no _default configured`);
    }

    return evaluateExpression(expr, variables);
  }

  private calcTools(metrics: UsageMetrics, variables: Record<string, number>): number {
    const defaultExpr = this.config.tools["_default"] ?? "tool_calls * 0";
    let total = 0;
    const seenSpecific = new Set<string>();

    const callNames = (metrics.toolCalls ?? []).map((t) => t.name);
    const uniqueNames = [...new Set(callNames)];

    for (const toolName of uniqueNames) {
      if (toolName in this.config.tools) {
        total += evaluateExpression(this.config.tools[toolName], variables);
        seenSpecific.add(toolName);
      }
    }

    const unknownCount = uniqueNames.filter((n) => !seenSpecific.has(n)).length;
    if (unknownCount > 0) {
      const local = { ...variables, tool_calls: unknownCount };
      total += evaluateExpression(defaultExpr, local);
    }

    return total;
  }

  private calcSearch(variables: Record<string, number>): number {
    if (this.config.search && "costs" in this.config.search) {
      return evaluateExpression(this.config.search["costs"], variables);
    }
    return 0;
  }

  private calcCache(variables: Record<string, number>): number {
    if (this.config.cache && "discount" in this.config.cache) {
      return evaluateExpression(this.config.cache["discount"], variables);
    }
    return 0;
  }

  private calcFixed(metrics: UsageMetrics): number {
    if (metrics.fixedJob && metrics.fixedJob in this.config.fixed) {
      return this.config.fixed[metrics.fixedJob];
    }
    return 0;
  }
}
