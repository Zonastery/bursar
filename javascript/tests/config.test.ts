import { describe, it, expect } from "vitest";
import { loadConfigFromDict } from "../src/config.js";
import { ConfigError } from "../src/errors.js";

const VALID_CONFIG = {
  models: { "gpt-4": "input_tokens * (0.01 / 1000) + output_tokens * (0.03 / 1000)" },
  tools: { _default: "tool_calls * 5 / 1000" },
  search: "search_queries * 0.5",
  cache: "-cache_read_tokens * (0.001 / 1000)",
  fixed: { batch_process: 50 },
  minBalance: 10,
};

describe("loadConfigFromDict", () => {
  it("loads a valid config", () => {
    const config = loadConfigFromDict(VALID_CONFIG);
    expect(config.models["gpt-4"]).toBeTruthy();
    expect(config.minBalance).toBe(10);
    expect(config.tools["_default"]).toBe("tool_calls * 5 / 1000");
    expect(config.fixed["batch_process"]).toBe(50);
  });

  it("rejects missing models", () => {
    expect(() => loadConfigFromDict({})).toThrow(ConfigError);
  });

  it("rejects empty models", () => {
    expect(() => loadConfigFromDict({ models: {} })).toThrow(ConfigError);
  });

  it("rejects invalid expressions in models", () => {
    expect(() =>
      loadConfigFromDict({
        models: { "gpt-4": "invalid_expr @" },
      }),
    ).toThrow(ConfigError);
  });

  it("rejects invalid expressions in tools", () => {
    expect(() =>
      loadConfigFromDict({
        models: { "gpt-4": "input_tokens * 0.01" },
        tools: { _default: "bad || expr" },
      }),
    ).toThrow(ConfigError);
  });

  it("applies default for missing tools", () => {
    const config = loadConfigFromDict({ models: { a: "input_tokens * 1" } });
    expect(config.tools["_default"]).toBe("tool_calls * 0");
  });

  it("applies defaults for missing optional fields", () => {
    const config = loadConfigFromDict({ models: { a: "input_tokens * 1" } });
    // WS6: minBalance defaults to 0 (was 5).
    expect(config.minBalance).toBe(0);
    // WS1: search/cache are a single expression string (or null), not a Record.
    expect(config.search).toBeNull();
    expect(config.cache).toBeNull();
    expect(config.fixed).toEqual({});
  });

  it("rejects negative minBalance", () => {
    expect(() =>
      loadConfigFromDict({
        models: { a: "input_tokens * 1" },
        minBalance: -1,
      }),
    ).toThrow(ConfigError);
  });

  // ── M5: variable-name validation against the known metric set ──
  it("rejects an unknown variable name at config load (typo)", () => {
    expect(() =>
      loadConfigFromDict({
        models: { "gpt-4": "inputtokens * 0.001" },
      }),
    ).toThrow(ConfigError);
  });

  it("rejects an unknown variable in a tool expression", () => {
    expect(() =>
      loadConfigFromDict({
        models: { "gpt-4": "input_tokens * 0.001" },
        tools: { _default: "toolcalls * 5" },
      }),
    ).toThrow(ConfigError);
  });

  it("rejects an unknown variable in a plan rate override", () => {
    expect(() =>
      loadConfigFromDict({
        models: { "gpt-4": "input_tokens * 0.001" },
        plans: {
          pro: { id: "p1", name: "Pro", rateOverrides: { "gpt-4": "inputtokens * 0.002" } },
        },
      }),
    ).toThrow(ConfigError);
  });

  it("defaults signupBonus to undefined (not set)", () => {
    const config = loadConfigFromDict({ models: { a: "input_tokens * 1" } });
    expect(config.signupBonus).toBeUndefined();
  });

  it("accepts a custom signupBonus value", () => {
    const config = loadConfigFromDict({
      models: { a: "input_tokens * 1" },
      signupBonus: 200,
    });
    expect(config.signupBonus).toBe(200);
  });

  it("accepts signupBonus of 0 (no bonus)", () => {
    const config = loadConfigFromDict({
      models: { a: "input_tokens * 1" },
      signupBonus: 0,
    });
    expect(config.signupBonus).toBe(0);
  });

  it("accepts all canonical metric variables", () => {
    const expr =
      "input_tokens + output_tokens + cache_read_tokens + cache_write_tokens + " +
      "tool_calls + search_queries + search_results + web_search_calls + code_exec_calls";
    expect(() => loadConfigFromDict({ models: { _default: expr } })).not.toThrow();
  });

  // ── WS2: `this_tool_calls` is scoped to `tools` expressions only ──
  it("rejects this_tool_calls in a models expression", () => {
    expect(() =>
      loadConfigFromDict({
        models: { _default: "this_tool_calls * 1" },
      }),
    ).toThrow(ConfigError);
  });

  it("accepts this_tool_calls in a tools expression", () => {
    expect(() =>
      loadConfigFromDict({
        models: { _default: "input_tokens * 1" },
        tools: { code_exec: "this_tool_calls * 10 / 1000" },
      }),
    ).not.toThrow();
  });

  // ── C5/C7: config-load rejects ** and div-by-zero-prone forms via validation ──
  it("rejects exponentiation in a model expression", () => {
    expect(() =>
      loadConfigFromDict({
        models: { "gpt-4": "input_tokens ** 2" },
      }),
    ).toThrow(ConfigError);
  });

  // ── CF1: Plan with rate_overrides loads without error ──
  it("accepts a plan with valid rateOverrides", () => {
    expect(() =>
      loadConfigFromDict({
        models: { "gpt-4": "input_tokens * 0.001" },
        plans: {
          pro: {
            id: "p1",
            name: "Pro",
            rateOverrides: { "gpt-4": "input_tokens * 0.003" },
          },
        },
      }),
    ).not.toThrow();
  });

  // ── CF2: Plan freeAllowance negative — no validation, stored as-is ──
  it("accepts plan with negative freeAllowance (no config-level validation)", () => {
    // config.ts does not validate freeAllowance sign; it stores new Decimal(value).
    expect(() =>
      loadConfigFromDict({
        models: { "gpt-4": "input_tokens * 0.001" },
        plans: {
          basic: { id: "b1", name: "Basic", freeAllowance: -10 },
        },
      }),
    ).not.toThrow();
  });

  // ── CF3: Empty sections are allowed ──
  it("accepts empty/absent tools, search, cache, fixed sections alongside valid models", () => {
    // WS1: search/cache are a single expression string (or null/absent), not a Record.
    expect(() =>
      loadConfigFromDict({
        models: { "gpt-4": "input_tokens * 0.001" },
        tools: {},
        search: null,
        cache: null,
        fixed: {},
      }),
    ).not.toThrow();
  });

  // ── CF4: minBalance as string "10" — coerced to number via JS loose comparison ──
  it("accepts minBalance as string (coerced, not rejected)", () => {
    // config.ts: config.minBalance = data.minBalance ?? 5 (no type coercion/rejection).
    // "10" < 0 evaluates to false (JS coerces "10" → 10 for comparison), so no throw.
    const config = loadConfigFromDict({
      models: { "gpt-4": "input_tokens * 0.001" },
      minBalance: "10" as unknown as number,
    });
    // The value is accepted; minBalance is stored as-is ("10").
    expect(config.minBalance).toBe("10");
  });

  // ── C1: minBalance type coercion / boundary ──

  // C1a: minBalance: 0 is the valid boundary (check is `< 0`, so 0 must be accepted).
  it("accepts minBalance: 0 (zero is a valid balance floor)", () => {
    const config = loadConfigFromDict({
      models: { "gpt-4": "input_tokens * 0.001" },
      minBalance: 0,
    });
    expect(config.minBalance).toBe(0);
  });

  // C1b: minBalance: -1 is already covered by "rejects negative minBalance" above.
  // This test explicitly documents that -1 is always rejected regardless of type.
  it("rejects minBalance: -1 (negative balance floor makes no sense)", () => {
    expect(() =>
      loadConfigFromDict({
        models: { "gpt-4": "input_tokens * 0.001" },
        minBalance: -1,
      }),
    ).toThrow(ConfigError);
  });

  // CF5: Duplicate plan names rejected ──
  it("rejects two plans with the same name field", () => {
    expect(() =>
      loadConfigFromDict({
        models: { "gpt-4": "input_tokens * 0.001" },
        plans: {
          plan_a: { id: "a1", name: "SameName" },
          plan_b: { id: "b1", name: "SameName" },
        },
      }),
    ).toThrow(ConfigError);
  });

  // ── WS3: fractional (Decimal-compatible) fixed-job costs ──
  it("rejects a negative fixed job cost", () => {
    expect(() =>
      loadConfigFromDict({
        models: { "gpt-4": "input_tokens * 0.001" },
        fixed: { batch_job: -1 },
      }),
    ).toThrow(ConfigError);
  });

  it("accepts a fractional fixed job cost (no longer integer-only)", () => {
    const config = loadConfigFromDict({
      models: { "gpt-4": "input_tokens * 0.001" },
      fixed: { batch_job: 2.5 },
    });
    expect(config.fixed["batch_job"]).toBe(2.5);
  });

  // ── WS9b: allowancePeriod on plan definitions ──
  describe("allowancePeriod", () => {
    for (const period of ["calendar_month", "rolling_30d", "anniversary"] as const) {
      it(`loads successfully with allowancePeriod: "${period}"`, () => {
        const config = loadConfigFromDict({
          models: { "gpt-4": "input_tokens * 0.001" },
          plans: {
            pro: { id: "p1", name: "Pro", allowancePeriod: period },
          },
        });
        expect(config.plans!.pro.allowancePeriod).toBe(period);
      });
    }

    it("rejects an invalid allowancePeriod value", () => {
      expect(() =>
        loadConfigFromDict({
          models: { "gpt-4": "input_tokens * 0.001" },
          plans: {
            pro: { id: "p1", name: "Pro", allowancePeriod: "weekly" },
          },
        }),
      ).toThrow(ConfigError);
    });

    it("defaults to calendar_month when omitted", () => {
      const config = loadConfigFromDict({
        models: { "gpt-4": "input_tokens * 0.001" },
        plans: {
          pro: { id: "p1", name: "Pro" },
        },
      });
      expect(config.plans!.pro.allowancePeriod).toBe("calendar_month");
    });
  });
});
