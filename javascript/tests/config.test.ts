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
    expect(config.minBalance.toString()).toBe("10");
    expect(config.tools["_default"]).toBe("tool_calls * 5 / 1000");
    expect(config.fixed["batch_process"].toString()).toBe("50");
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
    expect(config.minBalance.toString()).toBe("0");
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

  // ── CF2: Plan freeAllowance negative is rejected (parity with Python's ge=0) ──
  it("rejects plan with negative freeAllowance", () => {
    expect(() =>
      loadConfigFromDict({
        models: { "gpt-4": "input_tokens * 0.001" },
        plans: {
          basic: { id: "b1", name: "Basic", freeAllowance: -10 },
        },
      }),
    ).toThrow(ConfigError);
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

  // ── CF4: minBalance as string "10" — coerced to Decimal (parity with Python) ──
  it("coerces minBalance string to Decimal", () => {
    const config = loadConfigFromDict({
      models: { "gpt-4": "input_tokens * 0.001" },
      minBalance: "10" as unknown as number,
    });
    expect(config.minBalance.toString()).toBe("10");
  });

  // ── C1: minBalance type coercion / boundary ──

  // C1a: minBalance: 0 is the valid boundary (check is `< 0`, so 0 must be accepted).
  it("accepts minBalance: 0 (zero is a valid balance floor)", () => {
    const config = loadConfigFromDict({
      models: { "gpt-4": "input_tokens * 0.001" },
      minBalance: 0,
    });
    expect(config.minBalance.toString()).toBe("0");
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
    expect(config.fixed["batch_job"].toString()).toBe("2.5");
  });

  // ── Cross-language parity fixes ──

  it("rejects an unknown top-level config key (typo)", () => {
    expect(() =>
      loadConfigFromDict({
        models: { "gpt-4": "input_tokens * 0.001" },
        min_balnce: 5,
      }),
    ).toThrow(ConfigError);
  });

  it("rejects an unknown key in a plan definition", () => {
    expect(() =>
      loadConfigFromDict({
        models: { "gpt-4": "input_tokens * 0.001" },
        plans: { pro: { id: "p1", name: "Pro", free_allownce: 5 } },
      }),
    ).toThrow(ConfigError);
  });

  it("rejects an unknown key in a tier definition", () => {
    expect(() =>
      loadConfigFromDict({
        models: { "gpt-4": "input_tokens * 0.001" },
        tiers: { gifted: { name: "Gifted", priority: 1, expres: true } },
      }),
    ).toThrow(ConfigError);
  });

  it("rejects a plan definition missing the required 'name' field", () => {
    expect(() =>
      loadConfigFromDict({
        models: { "gpt-4": "input_tokens * 0.001" },
        plans: { pro: { id: "p1" } },
      }),
    ).toThrow(ConfigError);
  });

  it("rejects version !== 1", () => {
    expect(() =>
      loadConfigFromDict({
        version: 2,
        models: { "gpt-4": "input_tokens * 0.001" },
      }),
    ).toThrow(ConfigError);
  });

  it("accepts version: 1", () => {
    expect(() =>
      loadConfigFromDict({
        version: 1,
        models: { "gpt-4": "input_tokens * 0.001" },
      }),
    ).not.toThrow();
  });

  it("rejects a negative signupBonus", () => {
    expect(() =>
      loadConfigFromDict({
        models: { "gpt-4": "input_tokens * 0.001" },
        signupBonus: -1,
      }),
    ).toThrow(ConfigError);
  });

  it("does not inject a default _default tool when tools is explicitly provided without one", () => {
    // Mirrors Python: `tools` only gets its default_factory value when the key
    // is entirely absent; a user-supplied map (even without `_default`) is used as-is.
    const config = loadConfigFromDict({
      models: { "gpt-4": "input_tokens * 0.001" },
      tools: { code_exec: "this_tool_calls * 10 / 1000" },
    });
    expect(config.tools).toEqual({ code_exec: "this_tool_calls * 10 / 1000" });
  });

  it("populates perOperation on a plan (was silently dropped)", () => {
    const config = loadConfigFromDict({
      models: { "gpt-4": "input_tokens * 0.001" },
      plans: {
        pro: {
          id: "p1",
          name: "Pro",
          perOperation: {
            agent: { billingMode: "overdraft", overdraftFloor: -30, maxConcurrent: 1 },
          },
        },
      },
    });
    const policy = config.plans!.pro.perOperation!.agent;
    expect(policy.billingMode).toBe("overdraft");
    expect(policy.overdraftFloor!.toString()).toBe("-30");
    expect(policy.maxConcurrent).toBe(1);
  });

  it("accepts snake_case per_operation with nested snake_case keys", () => {
    const config = loadConfigFromDict({
      models: { "gpt-4": "input_tokens * 0.001" },
      plans: {
        pro: {
          id: "p1",
          name: "Pro",
          per_operation: {
            agent: { billing_mode: "overdraft", overdraft_floor: -30 },
          },
        },
      },
    });
    expect(config.plans!.pro.perOperation!.agent.billingMode).toBe("overdraft");
  });

  it("rejects an invalid billingMode in perOperation", () => {
    expect(() =>
      loadConfigFromDict({
        models: { "gpt-4": "input_tokens * 0.001" },
        plans: {
          pro: { id: "p1", name: "Pro", perOperation: { agent: { billingMode: "bogus" } } },
        },
      }),
    ).toThrow(ConfigError);
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

  // ── Credit tiers: config-parsing-level validation and defaults ─────────
  // (store-level runtime resolution/expiry checks live in tests/tiers.test.ts)
  describe("tiers", () => {
    const models = { "gpt-4": "input_tokens * 0.001" };

    it("is absent by default (no tiers section)", () => {
      const config = loadConfigFromDict({ models });
      expect(config.tiers).toBeUndefined();
    });

    it("loads a single tier with all fields set", () => {
      const config = loadConfigFromDict({
        models,
        tiers: {
          gifted: {
            name: "Gifted Credits",
            priority: 10,
            expires: true,
            defaultTtlDays: 30,
          },
        },
      });
      expect(config.tiers!.gifted).toEqual({
        name: "Gifted Credits",
        priority: 10,
        expires: true,
        defaultTtlDays: 30,
        allowOverdraft: false,
        isDefault: false,
      });
    });

    it("defaults name to the config key, priority to 0, expires/allowOverdraft/isDefault to false", () => {
      const config = loadConfigFromDict({
        models,
        tiers: { basic: {} },
      });
      expect(config.tiers!.basic).toEqual({
        name: "basic",
        priority: 0,
        expires: false,
        defaultTtlDays: null,
        allowOverdraft: false,
        isDefault: false,
      });
    });

    it("accepts snake_case tier fields (default_ttl_days / allow_overdraft / is_default)", () => {
      const config = loadConfigFromDict({
        models,
        tiers: {
          gifted: {
            name: "Gifted",
            priority: 10,
            expires: true,
            default_ttl_days: 14,
          },
          purchased: {
            name: "Purchased",
            priority: 20,
            expires: false,
            is_default: true,
            allow_overdraft: true,
          },
        },
      });
      expect(config.tiers!.gifted.defaultTtlDays).toBe(14);
      expect(config.tiers!.purchased.isDefault).toBe(true);
      expect(config.tiers!.purchased.allowOverdraft).toBe(true);
    });

    it("rejects an explicit empty tiers object (ambiguous — omit the key instead)", () => {
      expect(() => loadConfigFromDict({ models, tiers: {} })).toThrow(ConfigError);
    });

    it("rejects tiers that is not a dict (e.g. an array)", () => {
      expect(() => loadConfigFromDict({ models, tiers: [] })).toThrow(ConfigError);
    });

    it("rejects more than one tier with allowOverdraft: true", () => {
      expect(() =>
        loadConfigFromDict({
          models,
          tiers: {
            a: { name: "A", priority: 10, allowOverdraft: true },
            b: { name: "B", priority: 20, allowOverdraft: true },
          },
        }),
      ).toThrow(ConfigError);
    });

    it("rejects more than one tier with isDefault: true", () => {
      expect(() =>
        loadConfigFromDict({
          models,
          tiers: {
            a: { name: "A", priority: 10, isDefault: true },
            b: { name: "B", priority: 20, isDefault: true },
          },
        }),
      ).toThrow(ConfigError);
    });

    it("accepts exactly one allowOverdraft tier and one isDefault tier (may be the same or different tiers)", () => {
      expect(() =>
        loadConfigFromDict({
          models,
          tiers: {
            a: { name: "A", priority: 10, allowOverdraft: true },
            b: { name: "B", priority: 20, isDefault: true },
          },
        }),
      ).not.toThrow();
    });

    it("rejects defaultTtlDays: 0", () => {
      expect(() =>
        loadConfigFromDict({
          models,
          tiers: { a: { name: "A", priority: 10, expires: true, defaultTtlDays: 0 } },
        }),
      ).toThrow(ConfigError);
    });

    it("rejects a negative defaultTtlDays", () => {
      expect(() =>
        loadConfigFromDict({
          models,
          tiers: { a: { name: "A", priority: 10, expires: true, defaultTtlDays: -1 } },
        }),
      ).toThrow(ConfigError);
    });

    it("accepts a positive defaultTtlDays", () => {
      const config = loadConfigFromDict({
        models,
        tiers: { a: { name: "A", priority: 10, expires: true, defaultTtlDays: 1 } },
      });
      expect(config.tiers!.a.defaultTtlDays).toBe(1);
    });

    it("does not require defaultTtlDays on a non-expiring tier", () => {
      expect(() =>
        loadConfigFromDict({
          models,
          tiers: { a: { name: "A", priority: 10, expires: false } },
        }),
      ).not.toThrow();
    });

    it("preserves insertion order of multiple tiers (sorting is a store-level concern)", () => {
      const config = loadConfigFromDict({
        models,
        tiers: {
          c: { name: "C", priority: 5 },
          a: { name: "A", priority: 1 },
          b: { name: "B", priority: 3 },
        },
      });
      expect(Object.keys(config.tiers!)).toEqual(["c", "a", "b"]);
    });
  });
});
