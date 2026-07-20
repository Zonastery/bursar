import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import Decimal from "decimal.js";
import { PricingEngine as PricingEngineStrict } from "../src/engine.js";
import { ConfigError } from "../src/errors.js";
import type { UsageMetrics } from "../src/metrics.js";

const LEGACY_FIELD_NAMES: Record<string, string> = {
  minBalance: "min_balance",
  signupGrant: "signup_grant",
  rateOverrides: "rate_overrides",
  billingMode: "billing_mode",
  overdraftFloor: "overdraft_floor",
  perOperation: "per_operation",
  maxConcurrent: "max_concurrent",
  ttlDays: "ttl_days",
  allowOverdraft: "allow_overdraft",
  maxCalls: "max_calls",
  onExceed: "on_exceed",
  cacheDiscount: "cache_discount",
  flatJobs: "flat_jobs",
  intervalCount: "interval_count",
  depositTo: "deposit_to",
  creditsPerUnit: "credits_per_unit",
  minAmountMinor: "min_amount_minor",
  maxAmountMinor: "max_amount_minor",
  taxBehavior: "tax_behavior",
  productId: "product_id",
  priceId: "price_id",
  variantId: "variant_id",
  lookupKey: "lookup_key",
  replacePrior: "replace_prior",
  validFrom: "valid_from",
  validTo: "valid_to",
};

function legacyConfigFixture(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(legacyConfigFixture);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>).map(([key, item]) => [
        LEGACY_FIELD_NAMES[key] ?? key,
        legacyConfigFixture(item),
      ]),
    );
  }
  return value;
}

const PricingEngine = {
  fromDict(data: Record<string, unknown>) {
    return PricingEngineStrict.fromDict(legacyConfigFixture(data) as Record<string, unknown>);
  },
};

const TEST_CONFIG = {
  version: 1,
  metering: {
    models: {
      "gpt-4": "input_tokens * (0.01 / 1000) + output_tokens * (0.03 / 1000)",
      "gpt-3.5-turbo": "input_tokens * (0.001 / 1000) + output_tokens * (0.002 / 1000)",
      "*": "input_tokens * (0.05 / 1000)",
    },
    tools: {
      "*": "calls * 5 / 1000",
      code_exec: "calls * 10 / 1000",
    },
    search: "search_queries * 0.5 + search_results * 0.05",
    cacheDiscount: "cache_read_tokens * (0.001 / 1000)",
    flatJobs: { batch_train: 100 },
  },
  ledger: {
    minBalance: 5,
  },
};

describe("PricingEngine", () => {
  it("creates from dict", () => {
    const engine = PricingEngine.fromDict(TEST_CONFIG);
    expect(engine.minBalance.toString()).toBe("5");
  });

  it("rejects invalid config", () => {
    expect(() => PricingEngine.fromDict({ metering: { models: {} } })).toThrow(ConfigError);
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
        flatJob: "batch_train",
      };
      const result = engine.calculate(metrics);
      expect(result.fixedCredits.toFixed(4)).toBe("100.0000");
      expect(result.total.toFixed(4)).toBe("100.0000");
    });

    it("does not truncate a sub-1-credit total", () => {
      // CHANGED: a 0.4-credit op now yields total 0.4000 (was truncated to 0
      // downstream by Math.trunc in the manager).
      const engine = PricingEngine.fromDict({
        metering: { models: { "*": "input_tokens * 0.0004" } },
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

    it("uses * model when model not found", () => {
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

    it("throws for missing model with no *", () => {
      const cfg = {
        metering: { models: { "gpt-4": "input_tokens * 1" } },
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

    it("falls back to *", () => {
      const engine = PricingEngine.fromDict(TEST_CONFIG);
      expect(engine.resolveModel("claude-3")).toBe("*");
    });

    it("returns null if no match and no *", () => {
      const engine = PricingEngine.fromDict({
        metering: { models: { "gpt-4": "input_tokens * 1" } },
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

  describe("getFlatJobCost (Decimal | null)", () => {
    it("returns flat job cost for known job as Decimal", () => {
      const engine = PricingEngine.fromDict(TEST_CONFIG);
      const cost = engine.getFlatJobCost("batch_train");
      expect(cost).toBeInstanceOf(Decimal);
      expect(cost!.toFixed(4)).toBe("100.0000");
    });

    it("accepts fractional (Decimal-compatible) flat job cost (WS3)", () => {
      // WS3: flat job costs are Decimal-compatible, not integer-only. Only
      // negative values are rejected (see config.test.ts for that case).
      const engine = PricingEngine.fromDict({
        metering: { models: { "*": "input_tokens * 1" }, flatJobs: { tiny: 0.5 } },
      });
      expect(engine.getFlatJobCost("tiny")!.toFixed(4)).toBe("0.5000");
    });

    it("returns an integer flat job cost as exact Decimal", () => {
      const engine = PricingEngine.fromDict({
        metering: { models: { "*": "input_tokens * 1" }, flatJobs: { embed: 2 } },
      });
      expect(engine.getFlatJobCost("embed")!.toFixed(4)).toBe("2.0000");
    });

    it("returns null for unknown job", () => {
      const engine = PricingEngine.fromDict(TEST_CONFIG);
      expect(engine.getFlatJobCost("unknown")).toBeNull();
    });
  });

  describe("pricingSchema", () => {
    it("returns config schema", () => {
      const engine = PricingEngine.fromDict(TEST_CONFIG);
      const schema = engine.pricingSchema();
      expect(schema.metering.models["gpt-4"]).toBeTruthy();
      expect(schema.metering.tools).toBeTruthy();
      expect(schema.metering.search).toBeTruthy();
      expect(schema.metering.cache_discount).toBeTruthy();
      expect(schema.metering.flat_jobs).toBeTruthy();
    });
  });
});

// ── EN1: Tool calls with duplicate names ──
describe("tool calls with duplicate names", () => {
  it("duplicate tool names: specific tool charged once, unknown calls each charged via default", () => {
    // web_search is not in TEST_CONFIG.tools, so both calls go to unknownCount
    const engine = PricingEngine.fromDict(TEST_CONFIG);
    const metrics: UsageMetrics = {
      model: "gpt-4",
      inputTokens: 0,
      outputTokens: 0,
      toolCalls: [{ name: "web_search" }, { name: "web_search" }],
    };
    const result = engine.calculate(metrics);
    // unknownCount = 2, default = tool_calls * 5/1000 with tool_calls=2 → 0.01
    expect(result.toolCredits.toFixed(4)).toBe("0.0100");
  });

  it("duplicate known tool names: specific tool expression charged once (unique set)", () => {
    // code_exec is in TEST_CONFIG.tools. With two identical calls the specific expression
    // is only evaluated once (per unique name), with tool_calls=2 in context.
    const engine = PricingEngine.fromDict(TEST_CONFIG);
    const metrics: UsageMetrics = {
      model: "gpt-4",
      inputTokens: 0,
      outputTokens: 0,
      toolCalls: [{ name: "code_exec" }, { name: "code_exec" }],
    };
    const result = engine.calculate(metrics);
    // variables.tool_calls = 2 (total calls), expression = tool_calls * 10/1000
    // evaluated once for the unique name "code_exec": 2 * 10/1000 = 0.02
    expect(result.toolCredits.toFixed(4)).toBe("0.0200");
  });
});

// ── WS2: per-tool pricing gets its own `calls` variable ──
describe("WS2: calls is scoped per tool, tool_calls stays global", () => {
  it("code_exec is priced on its OWN call count, not the total across all tools", () => {
    const engine = PricingEngine.fromDict({
      metering: {
        models: { "*": "input_tokens * 0" },
        tools: {
          code_exec: "calls * 10 / 1000",
          "*": "calls * 5 / 1000",
        },
      },
    });
    const metrics: UsageMetrics = {
      model: "x",
      inputTokens: 0,
      toolCalls: [
        { name: "code_exec" },
        { name: "code_exec" },
        { name: "other_a" },
        { name: "other_b" },
        { name: "other_c" },
      ],
    };
    const result = engine.calculate(metrics);
    // code_exec: calls = 2 (its own count, NOT 5 total) → 2*10/1000 = 0.02
    // default (3 unconfigured calls): calls = 3 → 3*5/1000 = 0.015
    // total tool credits = 0.02 + 0.015 = 0.035
    expect(result.toolCredits.toFixed(4)).toBe("0.0350");
  });

  it("tool_calls (global) is still usable and reflects the TOTAL across all tools", () => {
    // A tool expression referencing the global `tool_calls` (not `calls`)
    // must see the total call count across every tool, unaffected by WS2.
    const engine = PricingEngine.fromDict({
      metering: {
        models: { "*": "input_tokens * 0" },
        tools: {
          code_exec: "tool_calls * 1 / 1000",
          "*": "tool_calls * 0",
        },
      },
    });
    const metrics: UsageMetrics = {
      model: "x",
      inputTokens: 0,
      toolCalls: [{ name: "code_exec" }, { name: "other_a" }, { name: "other_b" }],
    };
    const result = engine.calculate(metrics);
    // code_exec expression uses global tool_calls = 3 (total) → 3 * 1/1000 = 0.003
    expect(result.toolCredits.toFixed(4)).toBe("0.0030");
  });
});

// ── EN2: No tools section in config ──
describe("config with no tools section", () => {
  it("tool calls default to zero cost when no tools section provided", () => {
    const engine = PricingEngine.fromDict({
      metering: { models: { "*": "input_tokens * 0.001" } },
    });
    const result = engine.calculate({
      model: "x",
      inputTokens: 0,
      toolCalls: [{ name: "web_search" }],
    });
    // default tool expression is "calls * 0", so tool cost = 0
    expect(result.toolCredits.toFixed(4)).toBe("0.0000");
  });
});

// ── EN3: Cache section absent ──
describe("config with no cache section", () => {
  it("cache cost is zero when cache section is missing", () => {
    const engine = PricingEngine.fromDict({
      metering: { models: { "*": "input_tokens * 0.001" } },
    });
    const result = engine.calculate({
      model: "x",
      inputTokens: 1000,
      cacheReadTokens: 99999,
    });
    expect(result.cacheSavings.toFixed(4)).toBe("0.0000");
  });
});

// ── EN4: Flat job cost for unknown job ──
describe("getFlatJobCost for nonexistent job", () => {
  it("returns null for unknown job name", () => {
    const engine = PricingEngine.fromDict(TEST_CONFIG);
    expect(engine.getFlatJobCost("nonexistent_job")).toBeNull();
  });
});

// ── EN5: calculateBatch with empty array ──
describe("calculateBatch with empty array", () => {
  it("returns empty array without error", () => {
    const engine = PricingEngine.fromDict(TEST_CONFIG);
    expect(engine.calculateBatch([])).toEqual([]);
  });
});

// ── EN6: Total clamped at zero when cache discount > model cost ──
describe("total clamped at zero when discount exceeds cost", () => {
  it("large cache discount produces total = 0.0000 (not negative)", () => {
    const engine = PricingEngine.fromDict({
      metering: {
        models: { "*": "input_tokens * 0.000001" },
        cacheDiscount: "cache_read_tokens * 1",
      },
    });
    const result = engine.calculate({
      model: "x",
      inputTokens: 1, // model cost = 0.000001
      cacheReadTokens: 100, // cacheDiscount = 100 → negated → cacheSavings = -100
    });
    // rawTotal = 0.000001 + (-100) < 0 → clamped to 0
    expect(result.total.toFixed(4)).toBe("0.0000");
    expect(result.total.isNegative()).toBe(false);
  });
});

// ── EN7: Model resolution — exact match beats prefix ──
describe("model resolution: exact match over prefix", () => {
  it("gpt-4-turbo resolves to exact key, not gpt-4 prefix", () => {
    const engine = PricingEngine.fromDict({
      metering: {
        models: {
          "gpt-4": "input_tokens * 1",
          "gpt-4-turbo": "input_tokens * 2",
        },
      },
    });
    const result = engine.calculate({ model: "gpt-4-turbo", inputTokens: 1000 });
    // exact match "gpt-4-turbo" → 1000 * 2 = 2000
    expect(result.modelCredits.toFixed(4)).toBe("2000.0000");
  });
});

// ── Plan rate overrides ──
describe("plan rate overrides", () => {
  it("applies rateOverrides when passed to calculate()", () => {
    const engine = PricingEngine.fromDict({
      metering: {
        models: {
          "gpt-4": "input_tokens * 0.002",
          "*": "input_tokens * 0.001",
        },
      },
    });
    const result = engine.calculate(
      { model: "gpt-4", inputTokens: 1000 },
      { "gpt-4": "input_tokens * 0.005" },
    );
    expect(result.modelCredits.toFixed(4)).toBe("5.0000");
  });

  it("resolves prefix model keys for rateOverrides", () => {
    const engine = PricingEngine.fromDict({
      metering: {
        models: {
          "gpt-4": "input_tokens * 0.002",
          "*": "input_tokens * 0.001",
        },
      },
    });
    const result = engine.calculate(
      { model: "gpt-4-turbo", inputTokens: 1000 },
      { "gpt-4": "input_tokens * 0.004" },
    );
    expect(result.modelCredits.toFixed(4)).toBe("4.0000");
  });
});

// ── M5: calculateBatch error propagation for unknown model with no _default ──
describe("calculateBatch error propagation (M5)", () => {
  it("throws with a descriptive error when model is unknown and no * exists", () => {
    const engine = PricingEngine.fromDict({
      metering: { models: { "gpt-4": "input_tokens * 0.001" } },
    });
    // calculateBatch maps over calculate(); the first unknown model should throw,
    // not silently produce undefined or zero.
    expect(() =>
      engine.calculateBatch([
        { model: "gpt-4", inputTokens: 100 },
        { model: "unknown-model", inputTokens: 100 },
      ]),
    ).toThrow(ConfigError);
  });
});

// ── M6: Model prefix ambiguity — exact match beats prefix; prefix beats _default ──
describe("model prefix ambiguity (M6)", () => {
  const m6Engine = PricingEngine.fromDict({
    metering: {
      models: {
        "gpt-4": "input_tokens * 0.002",
        "gpt-4-turbo": "input_tokens * 0.003",
        "*": "input_tokens * 0.001",
      },
    },
  });

  it("gpt-4-turbo uses exact match (0.003 rate) → 3.0000", () => {
    // Exact key "gpt-4-turbo" exists; must NOT fall through to gpt-4 prefix.
    const result = m6Engine.calculate({ model: "gpt-4-turbo", inputTokens: 1000 });
    expect(result.modelCredits.toFixed(4)).toBe("3.0000");
  });

  it("gpt-4-0613 falls back to _default — calcModel does NOT do prefix matching", () => {
    // resolveModel() does prefix matching, but calcModel() (used by calculate()) does
    // only exact-key lookup then falls to _default. So "gpt-4-0613" has no exact match
    // and no _default prefix walk — it goes straight to _default (0.001 rate).
    // NOTE: if prefix matching were wired into calcModel(), the expectation would be
    // 2.0000 (gpt-4 prefix, 0.002 rate). This test documents that it is NOT applied.
    const result = m6Engine.calculate({ model: "gpt-4-0613", inputTokens: 1000 });
    // _default rate 0.001 → 1000 * 0.001 = 1.0000
    expect(result.modelCredits.toFixed(4)).toBe("1.0000");
  });

  it("claude-3 falls back to _default (0.001 rate) → 1.0000", () => {
    // No exact or prefix match → uses _default.
    const result = m6Engine.calculate({ model: "claude-3", inputTokens: 1000 });
    expect(result.modelCredits.toFixed(4)).toBe("1.0000");
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
      const rawToolCalls = c.metrics.tool_calls as Array<{ name: string }> | undefined;
      const metrics: UsageMetrics = {
        model: (c.metrics.model as string) ?? null,
        inputTokens: (c.metrics.input_tokens as number) ?? 0,
        outputTokens: (c.metrics.output_tokens as number) ?? 0,
        cacheReadTokens: (c.metrics.cache_read_tokens as number) ?? 0,
        cacheWriteTokens: (c.metrics.cache_write_tokens as number) ?? 0,
        searchQueries: (c.metrics.search_queries as number) ?? 0,
        searchResults: (c.metrics.search_results as number) ?? 0,
        webSearchCalls: (c.metrics.web_search_calls as number) ?? 0,
        codeExecCalls: (c.metrics.code_exec_calls as number) ?? 0,
        flatJob: (c.metrics.flat_job as string) ?? undefined,
        toolCalls: Array.isArray(rawToolCalls) ? rawToolCalls.map((t) => ({ name: t.name })) : [],
      };
      const result = engine.calculate(metrics);
      expect(result.total.toFixed(4)).toBe(c.expected_total);
    });
  }
});
