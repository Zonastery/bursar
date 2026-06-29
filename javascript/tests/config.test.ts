import { describe, it, expect } from "vitest";
import { loadConfigFromDict } from "../src/config.js";
import { ConfigError } from "../src/errors.js";

const VALID_CONFIG = {
  models: { "gpt-4": "input_tokens * (0.01 / 1000) + output_tokens * (0.03 / 1000)" },
  tools: { _default: "tool_calls * 5 / 1000" },
  search: { costs: "search_queries * 0.5" },
  cache: { discount: "-cache_read_tokens * (0.001 / 1000)" },
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
    expect(config.minBalance).toBe(5);
    expect(config.search).toEqual({});
    expect(config.cache).toEqual({});
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

  it("accepts all canonical metric variables", () => {
    const expr =
      "input_tokens + output_tokens + cache_read_tokens + cache_write_tokens + " +
      "tool_calls + search_queries + search_results + web_search_calls + code_exec_calls";
    expect(() => loadConfigFromDict({ models: { _default: expr } })).not.toThrow();
  });

  // ── C5/C7: config-load rejects ** and div-by-zero-prone forms via validation ──
  it("rejects exponentiation in a model expression", () => {
    expect(() =>
      loadConfigFromDict({
        models: { "gpt-4": "input_tokens ** 2" },
      }),
    ).toThrow(ConfigError);
  });
});
