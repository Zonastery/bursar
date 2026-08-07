import Decimal from "decimal.js";

import { makeCostBreakdown } from "./breakdown.js";
import type { CostBreakdown } from "./breakdown.js";
import {
  canonicalParsedBursarConfigDict,
  loadConfigFromDict,
  type Charge,
  type BursarConfigData,
  type DimensionMatcher,
  type OperationPricing,
  type ParsedBursarConfig,
  type PriceRule,
} from "./config.js";
import { ConfigError } from "./errors.js";
import { evaluateExpression } from "./expr.js";
import type { UsageMetrics } from "./metrics.js";

const quantize = (value: Decimal): Decimal => value.toDecimalPlaces(6, Decimal.ROUND_HALF_UP);

type DimensionValue = string | Decimal | boolean;

function requiredMeasure(measures: Record<string, Decimal>, name: string): Decimal {
  const measure = measures[name];
  if (measure === undefined) {
    throw new ConfigError(`price charge references missing usage measure '${name}'`);
  }
  return measure;
}

export class PricingEngine {
  constructor(private readonly config: ParsedBursarConfig) {}

  static fromDict(data: BursarConfigData | Record<string, unknown>): PricingEngine {
    return new PricingEngine(loadConfigFromDict(data));
  }

  get pricingSchema(): Record<string, unknown> {
    return canonicalParsedBursarConfigDict(this.config);
  }

  calculate(
    metrics: UsageMetrics,
    options: { rateCard?: string } | Record<string, string> | null = {},
  ): CostBreakdown {
    const pricing = this.config.pricing;
    if (!pricing) throw new ConfigError("usage pricing not configured");
    const operation = pricing.operations[metrics.operation];
    if (!operation) throw new ConfigError(`unknown usage operation '${metrics.operation}'`);
    const measuresInput = metrics.measures ?? {};
    const dimensionsInput = metrics.dimensions ?? {};
    const unknownMeasures = Object.keys(measuresInput).filter((key) => !operation.measures[key]);
    const unknownDimensions = Object.keys(dimensionsInput).filter(
      (key) => !operation.dimensions[key],
    );
    if (unknownMeasures.length)
      throw new ConfigError(
        `operation '${metrics.operation}' received undeclared measures ${unknownMeasures.join(", ")}`,
      );
    if (unknownDimensions.length)
      throw new ConfigError(
        `operation '${metrics.operation}' received undeclared dimensions ${unknownDimensions.join(", ")}`,
      );

    const dimensions: Record<string, DimensionValue> = {};
    for (const [name, definition] of Object.entries(operation.dimensions)) {
      const input = dimensionsInput[name];
      if (input == null) {
        if (definition.required)
          throw new ConfigError(`operation '${metrics.operation}' requires dimension '${name}'`);
        continue;
      }
      if (definition.type === "string") {
        if (typeof input !== "string") throw new ConfigError(`dimension '${name}' must be string`);
        dimensions[name] = input;
      } else if (definition.type === "boolean") {
        if (typeof input !== "boolean")
          throw new ConfigError(`dimension '${name}' must be boolean`);
        dimensions[name] = input;
      } else {
        const numeric = new Decimal(input as Decimal.Value);
        if (!numeric.isFinite()) throw new ConfigError(`dimension '${name}' must be finite`);
        dimensions[name] = numeric;
      }
    }

    const measures: Record<string, Decimal> = {};
    for (const name of Object.keys(operation.measures)) {
      const value = new Decimal(measuresInput[name] ?? 0);
      if (!value.isFinite() || value.isNegative())
        throw new ConfigError(`usage measure '${name}' must be finite and non-negative`);
      measures[name] = value;
    }
    const requestedRateCard =
      options != null && "rateCard" in options ? options.rateCard : undefined;
    const cardKey = this.resolveRateCard(requestedRateCard);
    const operationPrice = this.operationPricing(cardKey, metrics.operation);
    const matched = operationPrice.rules.find((rule) => this.matches(rule, dimensions));
    const selected =
      matched?.charge ??
      (operationPrice.unmatched.action === "charge" ? operationPrice.unmatched.charge : undefined);
    if (!selected)
      throw new ConfigError(
        `no price rule matched operation '${metrics.operation}' in rate card '${cardKey}'`,
      );
    const value = this.evaluateCharge(selected, measures);
    if (!value.isFinite() || value.isNegative())
      throw new ConfigError(
        `price charge for '${metrics.operation}' produced a negative or non-finite credit cost`,
      );
    const total = quantize(value);
    return makeCostBreakdown({
      operationCredits: total,
      breakdown: {
        operation: metrics.operation,
        rateCard: cardKey,
        chargeType: selected.type,
        measures: Object.fromEntries(
          Object.entries(measures).map(([key, amount]) => [key, amount.toString()]),
        ),
        dimensions: dimensionsInput,
      },
    });
  }

  calculateBatch(metrics: UsageMetrics[], options: { rateCard?: string } = {}): CostBreakdown[] {
    return metrics.map((item) => this.calculate(item, options));
  }

  getRateCardForPlan(planId: string | null | undefined): string | undefined {
    if (!planId) return undefined;
    return this.config.plans[planId]?.rateCard;
  }

  private resolveRateCard(requested?: string): string {
    const cards = this.config.pricing!.rateCards;
    if (requested) {
      if (!cards[requested]) throw new ConfigError(`unknown rate card '${requested}'`);
      return requested;
    }
    const keys = Object.keys(cards);
    const onlyCard = keys[0];
    if (keys.length === 1 && onlyCard !== undefined) return onlyCard;
    throw new ConfigError("rateCard is required when more than one rate card is configured");
  }

  private operationPricing(cardKey: string, operation: string): OperationPricing {
    const card = this.config.pricing!.rateCards[cardKey];
    if (!card) throw new ConfigError(`unknown rate card '${cardKey}'`);
    const operationPrice = card.operations[operation];
    if (operationPrice) return operationPrice;
    if (!card.extends)
      throw new ConfigError(`rate card '${cardKey}' has no price for operation '${operation}'`);
    return this.operationPricing(card.extends, operation);
  }

  private matches(rule: PriceRule, dimensions: Record<string, DimensionValue>): boolean {
    return Object.entries(rule.when).every(([name, matcher]) =>
      this.matchesOne(matcher, dimensions[name]),
    );
  }

  private matchesOne(matcher: DimensionMatcher, value: DimensionValue | undefined): boolean {
    if (value == null) return false;
    if (matcher.op === "eq") return this.scalarEquals(value, matcher.value);
    if (matcher.op === "in")
      return matcher.values.some((candidate) => this.scalarEquals(value, candidate));
    if (matcher.op === "not_in")
      return !matcher.values.some((candidate) => this.scalarEquals(value, candidate));
    if (matcher.op === "prefix")
      return typeof value === "string" && value.startsWith(matcher.value);
    if (matcher.op !== "range" || !(value instanceof Decimal)) return false;
    if (matcher.gt && !value.gt(matcher.gt)) return false;
    if (matcher.gte && !value.gte(matcher.gte)) return false;
    if (matcher.lt && !value.lt(matcher.lt)) return false;
    if (matcher.lte && !value.lte(matcher.lte)) return false;
    return true;
  }

  private scalarEquals(left: DimensionValue, right: string | Decimal | boolean): boolean {
    if (left instanceof Decimal || right instanceof Decimal) {
      try {
        return new Decimal(left as Decimal.Value).eq(new Decimal(right as Decimal.Value));
      } catch {
        return false;
      }
    }
    return left === right;
  }

  private evaluateCharge(charge: Charge, measures: Record<string, Decimal>): Decimal {
    if (charge.type === "flat") return charge.amount;
    if (charge.type === "per_unit")
      return requiredMeasure(measures, charge.measure).div(charge.unitSize).mul(charge.rate);
    if (charge.type === "package") {
      const packages = requiredMeasure(measures, charge.measure).div(charge.units);
      const rounded =
        charge.rounding === "ceil"
          ? packages.ceil()
          : charge.rounding === "floor"
            ? packages.floor()
            : packages.toDecimalPlaces(0, Decimal.ROUND_HALF_UP);
      return rounded.mul(charge.amount);
    }
    if (charge.type === "graduated") {
      let remaining = requiredMeasure(measures, charge.measure);
      let previous = new Decimal(0);
      let total = new Decimal(0);
      for (const tier of charge.tiers) {
        const units =
          tier.upTo == null ? remaining : Decimal.min(remaining, tier.upTo.sub(previous));
        if (units.isPositive()) {
          total = total.add(units.mul(tier.rate));
          remaining = remaining.sub(units);
        }
        if (!remaining.isPositive()) break;
        if (tier.upTo != null) previous = tier.upTo;
      }
      return total;
    }
    if (charge.type === "volume") {
      const value = requiredMeasure(measures, charge.measure);
      const tier =
        charge.tiers.find((candidate) => candidate.upTo == null || value.lte(candidate.upTo)) ??
        charge.tiers.at(-1);
      if (!tier) throw new ConfigError("volume charge requires at least one tier");
      return value.mul(tier.rate);
    }
    if (charge.type === "expression") return evaluateExpression(charge.formula, measures);
    if (charge.type === "sum")
      return Decimal.sum(
        ...charge.components.map((component) => this.evaluateCharge(component, measures)),
      );
    throw new ConfigError(`unsupported charge type '${String((charge as Charge).type)}'`);
  }
}
