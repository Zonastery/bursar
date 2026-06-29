import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import Decimal from "decimal.js";
import { PricingEngine } from "../src/engine.js";
import { ConfigError } from "../src/errors.js";
import type { UsageMetrics } from "../src/metrics.js";

const TEST_CONFIG = {
  models: {
    "gpt-4": "input_tokens * (0.01 / 1000) + output_tokens * (0.03 / 1000)",
    "gpt-3.5-turbo": "input_tokens * (0.001 / 1000) + output_tokens * (0.002 / 1000)",
    _default: "input_tokens * (0.05 / 1000)",
  },
  tools: {
    _default: "tool_calls * 5 / 1000",
    code_exec: "tool_calls * 10 / 1000",
  },
  search: { costs: "search_queries * 0.5 + search_results * 0.05" },
  cache: { discount: "-cache_read_tokens * (0.001 / 1000)" },
  fixed: { batch_train: 100 },
  minBalance: 5,
};

describe("PricingEngine", () => {
  it("creates from dict", () => {
    const engine = PricingEngine.fromDict(TEST_CONFIG);
    expect(engine.minBalance).toBe(5);
  });

  it("rejects invalid config", () => {
    expect(() => PricingEngine.fromDict({ models: {} })).toThrow(ConfigError);
  });

  describe("calculate (Decimal money)", () => {
    it("returns breakdown for model usage (exact Decimal)", () => {
      const engine = PricingEngine.fromDict(TEST_CONFIG);
      const metrics: UsageMetrics = {
        model: "gpt-4",
        inputTokens: 1000,
        outputTokens: 500,
      };
      const result = engine.calculate(metrics);
      // input: 1000 * (0.01 / 1000) = 0.01
      // output: 500 * (0.03 / 1000) = 0.015
      // modelCredits = 0.025 -> quantized 0.0250 (CHANGED: was round-to-2dp 0.03)
      expect(result.modelCredits).toBeInstanceOf(Decimal);
      expect(result.modelCredits.toFixed(4)).toBe("0.0250");
      expect(result.total.toFixed(4)).toBe("0.0250");
      expect(result.breakdown["model"]).toBe("gpt-4");
    });

    it("includes tool costs", () => {
      const engine = PricingEngine.fromDict(TEST_CONFIG);
      const metrics: UsageMetrics = {
        model: "gpt-4",
        inputTokens: 0,
        outputTokens: 0,
        toolCalls: [{ name: "code_exec" }, { name: "code_exec" }],
      };
      const result = engine.calculate(metrics);
      // code_exec: 2 * 10/1000 = 0.02
      expect(result.toolCredits.toFixed(4)).toBe("0.0200");
    });

    it("uses default tool cost for unknown tools", () => {
      const engine = PricingEngine.fromDict(TEST_CONFIG);
      const metrics: UsageMetrics = {
        model: "gpt-3.5-turbo",
        inputTokens: 100,
        outputTokens: 100,
        toolCalls: [{ name: "unknown_tool" }, { name: "unknown_tool" }],
      };
      const result = engine.calculate(metrics);
      // 2 * 5/1000 = 0.01
      expect(result.toolCredits.toFixed(4)).toBe("0.0100");
    });

    it("includes search costs", () => {
      const engine = PricingEngine.fromDict(TEST_CONFIG);
      const metrics: UsageMetrics = {
        model: "gpt-4",
        inputTokens: 100,
        outputTokens: 100,
        searchQueries: 2,
        searchResults: 10,
      };
      const result = engine.calculate(metrics);
      // 2 * 0.5 + 10 * 0.05 = 1 + 0.5 = 1.5
      expect(result.searchCredits.toFixed(4)).toBe("1.5000");
    });

    it("includes cache savings (negative)", () => {
      const engine = PricingEngine.fromDict(TEST_CONFIG);
      const metrics: UsageMetrics = {
        model: "gpt-4",
        inputTokens: 100,
        outputTokens: 100,
        cacheReadTokens: 50000,
      };
      const result = engine.calculate(metrics);
      // -50000 * 0.000001 = -0.05
      expect(result.cacheSavings.toFixed(4)).toBe("-0.0500");
      expect(result.cacheSavings.isNegative()).toBe(true);
    });

    it("includes fixed job cost (not truncated)", () => {
      const engine = PricingEngine.fromDict(TEST_CONFIG);
      const metrics: UsageMetrics = {
        fixedJob: "batch_train",
      };
      const result = engine.calculate(metrics);
      expect(result.fixedCredits.toFixed(4)).toBe("100.0000");
      expect(result.total.toFixed(4)).toBe("100.0000");
    });

    it("does not truncate a sub-1-credit total", () => {
      // CHANGED: a 0.4-credit op now yields total 0.4000 (was truncated to 0
      // downstream by Math.trunc in the manager).
      const engine = PricingEngine.fromDict({
        models: { _default: "input_tokens * 0.0004" },
      });
      const result = engine.calculate({ model: "x", inputTokens: 1000 });
      expect(result.total.toFixed(4)).toBe("0.4000");
    });

    it("total is never negative (clamped to zero)", () => {
      const engine = PricingEngine.fromDict(TEST_CONFIG);
      const metrics: UsageMetrics = {
        model: "gpt-4",
        inputTokens: 0,
        outputTokens: 0,
        cacheReadTokens: 100_000, // big discount but no positive costs
      };
      const result = engine.calculate(metrics);
      expect(result.total.toFixed(4)).toBe("0.0000");
      expect(result.total.isNegative()).toBe(false);
    });

    it("uses _default model when model not found", () => {
      const engine = PricingEngine.fromDict(TEST_CONFIG);
      const metrics: UsageMetrics = {
        model: "unknown-model",
        inputTokens: 1000,
        outputTokens: 0,
      };
      const result = engine.calculate(metrics);
      // _default: 1000 * 0.00005 = 0.05
      expect(result.modelCredits.toFixed(4)).toBe("0.0500");
    });

    it("throws for missing model with no _default", () => {
      const cfg = {
        models: { "gpt-4": "input_tokens * 1" },
      };
      const engine = PricingEngine.fromDict(cfg);
      expect(() => engine.calculate({ model: "unknown", inputTokens: 100 })).toThrow(ConfigError);
    });
  });

  describe("calculateBatch", () => {
    it("calculates multiple metrics", () => {
      const engine = PricingEngine.fromDict(TEST_CONFIG);
      const results = engine.calculateBatch([
        { model: "gpt-4", inputTokens: 3000, outputTokens: 2000 },
        { model: "gpt-3.5-turbo", inputTokens: 5000, outputTokens: 3000 },
      ]);
      expect(results).toHaveLength(2);
      expect(results[0].modelCredits.greaterThan(0)).toBe(true);
      expect(results[1].modelCredits.greaterThan(0)).toBe(true);
    });
  });

  describe("resolveModel", () => {
    it("finds exact match", () => {
      const engine = PricingEngine.fromDict(TEST_CONFIG);
      expect(engine.resolveModel("gpt-4")).toBe("gpt-4");
    });

    it("finds prefix match", () => {
      const engine = PricingEngine.fromDict(TEST_CONFIG);
      expect(engine.resolveModel("gpt-4-turbo")).toBe("gpt-4");
    });

    it("falls back to _default", () => {
      const engine = PricingEngine.fromDict(TEST_CONFIG);
      expect(engine.resolveModel("claude-3")).toBe("_default");
    });

    it("returns null if no match and no _default", () => {
      const engine = PricingEngine.fromDict({
        models: { "gpt-4": "input_tokens * 1" },
      });
      expect(engine.resolveModel("claude-3")).toBeNull();
    });
  });

  describe("hasModel", () => {
    it("returns true for known model", () => {
      const engine = PricingEngine.fromDict(TEST_CONFIG);
      expect(engine.hasModel("gpt-4")).toBe(true);
    });

    it("returns false for unknown model", () => {
      const engine = PricingEngine.fromDict(TEST_CONFIG);
      expect(engine.hasModel("claude-3")).toBe(false);
    });

    it("returns false for prototype-chain names", () => {
      const engine = PricingEngine.fromDict(TEST_CONFIG);
      expect(engine.hasModel("constructor")).toBe(false);
      expect(engine.hasModel("__proto__")).toBe(false);
    });
  });

  describe("getFixedCost (Decimal | null)", () => {
    it("returns fixed cost for known job as Decimal", () => {
      const engine = PricingEngine.fromDict(TEST_CONFIG);
      const cost = engine.getFixedCost("batch_train");
      expect(cost).toBeInstanceOf(Decimal);
      expect(cost!.toFixed(4)).toBe("100.0000");
    });

    it("returns a fractional fixed cost without truncation", () => {
      // CHANGED: was coerced to int in Python / float in JS; now exact Decimal.
      const engine = PricingEngine.fromDict({
        models: { _default: "input_tokens * 1" },
        fixed: { tiny: 0.5 },
      });
      expect(engine.getFixedCost("tiny")!.toFixed(4)).toBe("0.5000");
    });

    it("returns null for unknown job", () => {
      const engine = PricingEngine.fromDict(TEST_CONFIG);
      expect(engine.getFixedCost("unknown")).toBeNull();
    });
  });

  describe("pricingSchema", () => {
    it("returns config as PricingConfigData", () => {
      const engine = PricingEngine.fromDict(TEST_CONFIG);
      const schema = engine.pricingSchema();
      expect(schema.models["gpt-4"]).toBeTruthy();
      expect(schema.tools).toBeTruthy();
      expect(schema.search).toBeTruthy();
      expect(schema.cache).toBeTruthy();
      expect(schema.fixed).toBeTruthy();
    });
  });
});

// ── Cross-SDK parity fixture (contract §7) — pricing_cases ──
const __dirname = dirname(fileURLToPath(import.meta.url));
const fixturePath = resolve(__dirname, "../../tests/parity/expression_cases.json");
interface PricingCase {
  name: string;
  config: Record<string, unknown>;
  metrics: {
    model?: string;
    input_tokens?: number;
    output_tokens?: number;
    [k: string]: unknown;
  };
  expected_total: string;
}
const fixture = JSON.parse(readFileSync(fixturePath, "utf8")) as {
  pricing_cases: PricingCase[];
};

describe("parity fixture — pricing_cases (totals)", () => {
  for (const c of fixture.pricing_cases) {
    it(c.name, () => {
      const engine = PricingEngine.fromDict(c.config);
      const metrics: UsageMetrics = {
        model: c.metrics.model ?? null,
        inputTokens: c.metrics.input_tokens ?? 0,
        outputTokens: c.metrics.output_tokens ?? 0,
      };
      const result = engine.calculate(metrics);
      expect(result.total.toFixed(4)).toBe(c.expected_total);
    });
  }
});
