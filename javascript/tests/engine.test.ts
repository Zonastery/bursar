import Decimal from "decimal.js";
import { describe, expect, it } from "vitest";

import type { BursarConfigData } from "../src/config.js";
import { PricingEngine } from "../src/engine.js";
import { ConfigError } from "../src/errors.js";
import { baseConfig } from "./config.test.js";

function mutableConfig(): BursarConfigData {
  return structuredClone(baseConfig()) as unknown as BursarConfigData;
}

function completionPricing(config: BursarConfigData) {
  return config.pricing!.rate_cards.standard!.operations!.completion!;
}

describe("typed pricing engine", () => {
  it("serializes its parsed configuration without re-validating camelCase fields", () => {
    const engine = PricingEngine.fromDict(baseConfig());
    const schema = engine.pricingSchema;
    expect(schema.version).toBe(1);
    expect(schema).toHaveProperty("pricing.rate_cards.standard");
    expect(schema).not.toHaveProperty("pricing.rateCards");
  });

  it("uses a matching typed rule", () => {
    const engine = PricingEngine.fromDict(baseConfig());
    const result = engine.calculate({
      operation: "completion",
      measures: { input_tokens: 2 },
      dimensions: { model: "gpt-fast" },
    });
    expect(result.total.eq(new Decimal("4.000000"))).toBe(true);
  });

  it("uses explicit unmatched charge", () => {
    const engine = PricingEngine.fromDict(baseConfig());
    const result = engine.calculate({
      operation: "completion",
      measures: { input_tokens: 2, output_tokens: 1 },
      dimensions: { model: "other" },
    });
    expect(result.total.eq(new Decimal("3.000000"))).toBe(true);
  });

  it("rejects unmatched dimensions when configured", () => {
    const config = mutableConfig();
    completionPricing(config).unmatched = {
      action: "reject",
    };
    const engine = PricingEngine.fromDict(config);
    expect(() =>
      engine.calculate({
        operation: "completion",
        measures: { input_tokens: 1 },
        dimensions: { model: "other" },
      }),
    ).toThrow(ConfigError);
  });

  it("calculates graduated pricing", () => {
    const config = mutableConfig();
    const completion = completionPricing(config);
    completion.rules = [];
    completion.unmatched = {
      action: "charge",
      charge: {
        type: "graduated",
        measure: "input_tokens",
        tiers: [
          { up_to: "10", rate: "1" },
          { up_to: null, rate: "2" },
        ],
      },
    };
    const engine = PricingEngine.fromDict(config);
    const result = engine.calculate({
      operation: "completion",
      measures: { input_tokens: 15 },
      dimensions: { model: "other" },
    });
    expect(result.total.toFixed(6)).toBe("20.000000");
  });

  it("rejects unknown operations, measures, and dimensions", () => {
    const pricing = PricingEngine.fromDict(baseConfig());
    expect(() => pricing.calculate({ operation: "missing", measures: {}, dimensions: {} })).toThrow(
      /unknown usage operation 'missing'/,
    );
    expect(() =>
      pricing.calculate({
        operation: "completion",
        measures: { surprise: 1 },
        dimensions: { model: "gpt-fast" },
      }),
    ).toThrow(/undeclared measures surprise/);
    expect(() =>
      pricing.calculate({
        operation: "completion",
        measures: { input_tokens: 1 },
        dimensions: { model: "gpt-fast", extra: "x" },
      }),
    ).toThrow(/undeclared dimensions extra/);
  });

  it("validates dimension types and required flags", () => {
    const config = baseConfig() as Record<string, unknown>;
    const operations = (config.pricing as Record<string, unknown>).operations as Record<
      string,
      Record<string, unknown>
    >;
    operations.completion = {
      measures: {
        input_tokens: { unit: "token" },
        output_tokens: { unit: "token" },
      },
      dimensions: {
        model: { type: "string" },
        premium: { type: "boolean" },
        weight: { type: "number", required: true },
      },
    };
    const pricing = PricingEngine.fromDict(config);

    expect(() =>
      pricing.calculate({
        operation: "completion",
        measures: { input_tokens: 1 },
        dimensions: { model: 5, weight: 1 },
      }),
    ).toThrow(/dimension 'model' must be string/);
    expect(() =>
      pricing.calculate({
        operation: "completion",
        measures: { input_tokens: 1 },
        dimensions: { model: "gpt-fast", premium: "yes", weight: 1 },
      }),
    ).toThrow(/dimension 'premium' must be boolean/);
    expect(() =>
      pricing.calculate({
        operation: "completion",
        measures: { input_tokens: 1 },
        dimensions: { model: "gpt-fast", premium: true, weight: "Infinity" },
      }),
    ).toThrow(/dimension 'weight' must be finite/);
    expect(() =>
      pricing.calculate({
        operation: "completion",
        measures: { input_tokens: 1 },
        dimensions: { model: "gpt-fast", premium: true },
      }),
    ).toThrow(/requires dimension 'weight'/);
  });

  it("rejects negative measures", () => {
    const pricing = PricingEngine.fromDict(baseConfig());
    expect(() =>
      pricing.calculate({
        operation: "completion",
        measures: { input_tokens: -1 },
        dimensions: { model: "gpt-fast" },
      }),
    ).toThrow(/must be finite and non-negative/);
  });

  it("requires a rate card when more than one is configured", () => {
    const config = mutableConfig();
    config.pricing!.rate_cards.enterprise = {
      extends: "standard",
      operations: {},
    };
    const pricing = PricingEngine.fromDict(config);
    expect(pricing.getRateCardForPlan(null)).toBeUndefined();
    expect(pricing.getRateCardForPlan("pro")).toBe("standard");
    expect(() =>
      pricing.calculate({
        operation: "completion",
        measures: { input_tokens: 1 },
        dimensions: { model: "gpt-fast" },
      }),
    ).toThrow(/rateCard is required/);
    expect(() =>
      pricing.calculate(
        {
          operation: "completion",
          measures: { input_tokens: 1 },
          dimensions: { model: "gpt-fast" },
        },
        { rateCard: "missing" },
      ),
    ).toThrow(/unknown rate card 'missing'/);
    const result = pricing.calculate(
      {
        operation: "completion",
        measures: { input_tokens: 1 },
        dimensions: { model: "gpt-fast" },
      },
      { rateCard: "enterprise" },
    );
    expect(result.total.eq(new Decimal("2.000000"))).toBe(true);
  });

  it("fails when a rate card lacks the operation and cannot extend", () => {
    const config = mutableConfig();
    config.pricing!.rate_cards.isolated = { operations: {} };
    const pricing = PricingEngine.fromDict(config);
    expect(() =>
      pricing.calculate(
        {
          operation: "completion",
          measures: { input_tokens: 1 },
          dimensions: { model: "gpt-fast" },
        },
        { rateCard: "isolated" },
      ),
    ).toThrow(/has no price for operation 'completion'/);
  });

  it("rejects a charge that evaluates negative", () => {
    const config = mutableConfig();
    const completion = completionPricing(config);
    completion.rules ??= [];
    completion.rules.push({
      when: { model: { op: "eq", value: "negative" } },
      charge: { type: "expression", formula: "input_tokens * -1" },
    });
    completion.unmatched = {
      action: "reject",
    };
    expect(() =>
      PricingEngine.fromDict(config).calculate({
        operation: "completion",
        measures: { input_tokens: 2 },
        dimensions: { model: "negative" },
      }),
    ).toThrow(/negative or non-finite credit cost/);
  });

  it("evaluates flat, package, volume, expression, and sum charges", () => {
    const config = mutableConfig();
    const completion = completionPricing(config);
    completion.rules = [
      {
        when: { model: { op: "eq", value: "flat" } },
        charge: { type: "flat", amount: "7.500000" },
      },
      {
        when: { model: { op: "eq", value: "package-ceil" } },
        charge: {
          type: "package",
          measure: "input_tokens",
          units: "10",
          amount: "2.000000",
          rounding: "ceil",
        },
      },
      {
        when: { model: { op: "eq", value: "package-floor" } },
        charge: {
          type: "package",
          measure: "input_tokens",
          units: "10",
          amount: "2.000000",
          rounding: "floor",
        },
      },
      {
        when: { model: { op: "eq", value: "package-nearest" } },
        charge: {
          type: "package",
          measure: "input_tokens",
          units: "10",
          amount: "2.000000",
          rounding: "nearest",
        },
      },
      {
        when: { model: { op: "eq", value: "volume" } },
        charge: {
          type: "volume",
          measure: "input_tokens",
          tiers: [
            { up_to: "10", rate: "1.000000" },
            { up_to: "50", rate: "2.000000" },
            { up_to: null, rate: "3.000000" },
          ],
        },
      },
      {
        when: { model: { op: "eq", value: "expression" } },
        charge: { type: "expression", formula: "input_tokens * 2 + output_tokens" },
      },
      {
        when: { model: { op: "eq", value: "sum" } },
        charge: {
          type: "sum",
          components: [
            { type: "flat", amount: "1.000000" },
            { type: "per_unit", measure: "input_tokens", rate: "0.500000" },
          ],
        },
      },
    ];
    completion.unmatched = { action: "reject" };
    const pricing = PricingEngine.fromDict(config);
    const calculate = (model: string, inputTokens: number, outputTokens = 0) =>
      pricing.calculate({
        operation: "completion",
        measures: { input_tokens: inputTokens, output_tokens: outputTokens },
        dimensions: { model },
      });

    expect(calculate("flat", 1).total.eq(new Decimal("7.500000"))).toBe(true);
    expect(calculate("package-ceil", 25).total.eq(new Decimal("6.000000"))).toBe(true);
    expect(calculate("package-floor", 25).total.eq(new Decimal("4.000000"))).toBe(true);
    expect(calculate("package-nearest", 25).total.eq(new Decimal("6.000000"))).toBe(true);
    expect(calculate("volume", 5).total.eq(new Decimal("5.000000"))).toBe(true);
    expect(calculate("volume", 15).total.eq(new Decimal("30.000000"))).toBe(true);
    expect(calculate("volume", 100).total.eq(new Decimal("300.000000"))).toBe(true);
    expect(calculate("expression", 3, 4).total.eq(new Decimal("10.000000"))).toBe(true);
    expect(calculate("sum", 2).total.eq(new Decimal("2.000000"))).toBe(true);
  });

  it("calculates batches and honors plan rate cards", () => {
    const pricing = PricingEngine.fromDict(baseConfig());
    const batch = pricing.calculateBatch(
      [
        {
          operation: "completion",
          measures: { input_tokens: 2 },
          dimensions: { model: "gpt-fast" },
        },
        {
          operation: "completion",
          measures: { input_tokens: 1 },
          dimensions: { model: "other" },
        },
      ],
      { rateCard: "standard" },
    );
    expect(batch).toHaveLength(2);
    expect(batch[0]!.total.eq(new Decimal("4.000000"))).toBe(true);
    expect(batch[1]!.total.eq(new Decimal("1.000000"))).toBe(true);
  });
});
