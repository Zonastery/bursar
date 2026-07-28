import Decimal from "decimal.js";
import { describe, expect, it } from "vitest";

import { PricingEngine } from "../src/engine.js";
import { ConfigError } from "../src/errors.js";
import { baseConfig } from "./config.test.js";

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
    const config = baseConfig();
    config.pricing.rate_cards.standard.operations.completion.unmatched = {
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
    const config = baseConfig();
    config.pricing.rate_cards.standard.operations.completion.rules = [];
    config.pricing.rate_cards.standard.operations.completion.unmatched = {
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
});
