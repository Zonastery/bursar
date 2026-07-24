import { describe, expect, it } from "vitest";

import { PricingEngine } from "../src/engine.js";
import { ConfigError } from "../src/errors.js";

const pricing = () => ({
  version: 1,
  usage: {
    operations: {
      completion: { measures: ["input_tokens", "output_tokens"], dimensions: ["model"] },
      image: { measures: ["images"], dimensions: [] },
    },
    rate_cards: {
      standard: {
        prices: {
          completion: [
            { match: { model: { exact: "fast" } }, formula: "input_tokens * 2" },
            {
              match: { model: { prefix: "premium-" } },
              formula: "input_tokens * 3 + output_tokens * 4",
            },
            { default: true, formula: "input_tokens + output_tokens" },
          ],
          image: [{ default: true, formula: "images * 5" }],
        },
      },
      discount: {
        extends: "standard",
        prices: { completion: [{ default: true, formula: "input_tokens * 0.5 + output_tokens" }] },
      },
    },
  },
  plans: { pro: { display_name: "Pro", rate_card: "discount" } },
});

describe("generic operation pricing", () => {
  it("uses exact and prefix matchers deterministically", () => {
    const engine = PricingEngine.fromDict(pricing());
    expect(
      engine
        .calculate(
          { operation: "completion", measures: { input_tokens: 2 }, dimensions: { model: "fast" } },
          { rateCard: "standard" },
        )
        .total.toString(),
    ).toBe("4");
    expect(
      engine
        .calculate(
          {
            operation: "completion",
            measures: { input_tokens: 2, output_tokens: 1 },
            dimensions: { model: "premium-x" },
          },
          { rateCard: "standard" },
        )
        .total.toString(),
    ).toBe("10");
  });

  it("inherits untouched operations and replaces overridden rules", () => {
    const engine = PricingEngine.fromDict(pricing());
    expect(
      engine
        .calculate(
          {
            operation: "completion",
            measures: { input_tokens: 2, output_tokens: 1 },
            dimensions: { model: "fast" },
          },
          { rateCard: "discount" },
        )
        .total.toString(),
    ).toBe("2");
    expect(
      engine
        .calculate({ operation: "image", measures: { images: 2 } }, { rateCard: "discount" })
        .total.toString(),
    ).toBe("10");
  });

  it("fails closed for unknown input and ambiguous rate-card choice", () => {
    const engine = PricingEngine.fromDict(pricing());
    expect(() => engine.calculate({ operation: "audio" }, { rateCard: "standard" })).toThrow(
      ConfigError,
    );
    expect(() =>
      engine.calculate({ operation: "image", measures: { seconds: 2 } }, { rateCard: "standard" }),
    ).toThrow(ConfigError);
    expect(() => engine.calculate({ operation: "image", measures: { images: 1 } })).toThrow(
      /rateCard is required/,
    );
  });
});
