import Decimal from "decimal.js";

import { ConfigError } from "./errors.js";
import { evaluateExpression } from "./expr.js";
import type { ParsedBursarConfig, PriceRule } from "./config.js";
import { canonicalBursarConfigDict, loadConfigFromDict } from "./config.js";
import type { CostBreakdown } from "./breakdown.js";
import { makeCostBreakdown } from "./breakdown.js";
import type { UsageMetrics } from "./metrics.js";

const quantize = (value: Decimal): Decimal => value.toDecimalPlaces(4, Decimal.ROUND_HALF_UP);

export class PricingEngine {
  constructor(private readonly config: ParsedBursarConfig) {}

  static fromDict(data: Record<string, unknown>): PricingEngine {
    return new PricingEngine(loadConfigFromDict(data));
  }

  get pricingSchema(): Record<string, unknown> {
    return canonicalBursarConfigDict(this.config as unknown as Record<string, unknown>);
  }

  get minBalance(): Decimal {
    return new Decimal(0);
  }

  calculate(
    metrics: UsageMetrics,
    options: { rateCard?: string } | Record<string, string> | null = {},
  ): CostBreakdown {
    const usage = this.config.usage;
    if (!usage) throw new ConfigError("usage pricing is not configured");
    const operationName = metrics.operation;
    const operation = usage.operations[operationName];
    if (!operation) throw new ConfigError(`unknown usage operation '${operationName}'`);
    const measures: Record<string, Decimal.Value> = { ...(metrics.measures ?? {}) };
    const dimensions: Record<string, string> = { ...(metrics.dimensions ?? {}) };

    const unknownMeasures = Object.keys(measures).filter(
      (key) => !operation.measures.includes(key),
    );
    const unknownDimensions = Object.keys(dimensions).filter(
      (key) => !operation.dimensions.includes(key),
    );
    if (unknownMeasures.length)
      throw new ConfigError(
        `operation '${metrics.operation}' received undeclared measures ${unknownMeasures.join(", ")}`,
      );
    if (unknownDimensions.length)
      throw new ConfigError(
        `operation '${metrics.operation}' received undeclared dimensions ${unknownDimensions.join(", ")}`,
      );
    const requestedRateCard =
      options != null && "rateCard" in options ? options.rateCard : undefined;
    const card = this.resolveRateCard(requestedRateCard);
    const rules = this.rulesFor(card, operationName);
    const rule = rules.find((candidate) => this.matches(candidate, dimensions));
    if (!rule)
      throw new ConfigError(
        `no price rule matched operation '${operationName}' in rate card '${card}'`,
      );
    const variables: Record<string, Decimal> = {};
    for (const name of operation.measures) {
      const value = new Decimal(measures[name] ?? 0);
      if (!value.isFinite() || value.isNegative())
        throw new ConfigError(`usage measure '${name}' must be finite and non-negative`);
      variables[name] = value;
    }
    const value = evaluateExpression(rule.formula, variables);
    if (!value.isFinite() || value.isNegative())
      throw new ConfigError(
        `price formula for '${operationName}' produced a negative or non-finite credit cost`,
      );
    const total = quantize(value);
    return makeCostBreakdown({
      operationCredits: total,
      breakdown: { operation: operationName, rateCard: card, measures, dimensions },
    });
  }

  calculateBatch(metrics: UsageMetrics[], options: { rateCard?: string } = {}): CostBreakdown[] {
    return metrics.map((metric) => this.calculate(metric, options));
  }

  getRateCardForPlan(planId: string | null | undefined): string | undefined {
    if (!planId) return undefined;
    return this.config.plans[planId]?.rateCard;
  }

  private resolveRateCard(requested?: string): string {
    const cards = this.config.usage!.rateCards;
    if (requested) {
      if (!cards[requested]) throw new ConfigError(`unknown rate card '${requested}'`);
      return requested;
    }
    const keys = Object.keys(cards);
    if (keys.length === 1) return keys[0];
    throw new ConfigError("rateCard is required when more than one rate card is configured");
  }

  private rulesFor(cardKey: string, operation: string): PriceRule[] {
    const card = this.config.usage!.rateCards[cardKey];
    if (card.prices[operation]) return card.prices[operation];
    if (!card.extends)
      throw new ConfigError(`rate card '${cardKey}' has no price for operation '${operation}'`);
    return this.rulesFor(card.extends, operation);
  }

  private matches(rule: PriceRule, dimensions: Record<string, string>): boolean {
    if (rule.default) return true;
    for (const [key, matcher] of Object.entries(rule.match ?? {})) {
      const value = dimensions[key];
      if (value == null) return false;
      if (matcher.exact != null && value !== matcher.exact) return false;
      if (matcher.prefix != null && !value.startsWith(matcher.prefix)) return false;
    }
    return true;
  }
}
