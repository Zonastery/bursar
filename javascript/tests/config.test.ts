import { describe, it, expect } from "vitest";
import { loadConfigFromDict } from "../src/config.js";
import { ConfigError } from "../src/errors.js";

const VALID_CONFIG = {
  version: 1,
  metering: {
    models: { "gpt-4": "input_tokens * (0.01 / 1000) + output_tokens * (0.03 / 1000)" },
    tools: { "*": "calls * 5 / 1000" },
    search: "search_queries * 0.5",
    cacheDiscount: "-cache_read_tokens * (0.001 / 1000)",
    flatJobs: { batch_process: 50 },
  },
  ledger: { minBalance: 10 },
};

describe("loadConfigFromDict", () => {
  it("loads a valid config", () => {
    const config = loadConfigFromDict(VALID_CONFIG);
    expect(config.metering.models["gpt-4"]).toBeTruthy();
    expect(config.ledger.minBalance.toString()).toBe("10");
    expect(config.metering.tools["*"]).toBe("calls * 5 / 1000");
    expect(config.metering.flatJobs["batch_process"].toString()).toBe("50");
  });

  it("rejects missing models", () => {
    expect(() => loadConfigFromDict({})).toThrow(ConfigError);
  });

  it("rejects empty models", () => {
    expect(() => loadConfigFromDict({ metering: { models: {} } })).toThrow(ConfigError);
  });

  it("rejects invalid expressions in models", () => {
    expect(() =>
      loadConfigFromDict({
        metering: { models: { "gpt-4": "invalid_expr @" } },
      }),
    ).toThrow(ConfigError);
  });

  it("rejects invalid expressions in tools", () => {
    expect(() =>
      loadConfigFromDict({
        metering: {
          models: { "gpt-4": "input_tokens * 0.01" },
          tools: { "*": "bad || expr" },
        },
      }),
    ).toThrow(ConfigError);
  });

  it("applies default for missing tools", () => {
    const config = loadConfigFromDict({ metering: { models: { a: "input_tokens * 1" } } });
    expect(config.metering.tools["*"]).toBe("calls * 0");
  });

  it("applies defaults for missing optional fields", () => {
    const config = loadConfigFromDict({ metering: { models: { a: "input_tokens * 1" } } });
    expect(config.ledger.minBalance.toString()).toBe("0");
    expect(config.metering.search).toBeNull();
    expect(config.metering.cacheDiscount).toBeNull();
    expect(config.metering.flatJobs).toEqual({});
  });

  it("rejects negative minBalance", () => {
    expect(() =>
      loadConfigFromDict({
        metering: { models: { a: "input_tokens * 1" } },
        ledger: { minBalance: -1 },
      }),
    ).toThrow(ConfigError);
  });

  // ── M5: variable-name validation against the known metric set ──
  it("rejects an unknown variable name at config load (typo)", () => {
    expect(() =>
      loadConfigFromDict({
        metering: { models: { "gpt-4": "inputtokens * 0.001" } },
      }),
    ).toThrow(ConfigError);
  });

  it("rejects an unknown variable in a tool expression", () => {
    expect(() =>
      loadConfigFromDict({
        metering: {
          models: { "gpt-4": "input_tokens * 0.001" },
          tools: { "*": "toolcalls * 5" },
        },
      }),
    ).toThrow(ConfigError);
  });

  it("rejects an unknown variable in a plan rate override", () => {
    expect(() =>
      loadConfigFromDict({
        metering: { models: { "gpt-4": "input_tokens * 0.001" } },
        plans: {
          pro: { label: "Pro", rateOverrides: { "gpt-4": "inputtokens * 0.002" } },
        },
      }),
    ).toThrow(ConfigError);
  });

  it("defaults signupGrant to null (no bonus)", () => {
    const config = loadConfigFromDict({ metering: { models: { a: "input_tokens * 1" } } });
    expect(config.ledger.signupGrant).toBeNull();
  });

  it("accepts a signupGrant object with bucket reference", () => {
    const config = loadConfigFromDict({
      metering: { models: { a: "input_tokens * 1" } },
      ledger: {
        signupGrant: { amount: 200, bucket: "gifted" },
        buckets: {
          gifted: { label: "Gifted", priority: 10 },
          purchased: { label: "Purchased", priority: 30, default: true },
        },
      },
    });
    expect(config.ledger.signupGrant).toEqual({ amount: 200, bucket: "gifted" });
  });

  it("rejects scalar signupGrant", () => {
    expect(() =>
      loadConfigFromDict({
        metering: { models: { a: "input_tokens * 1" } },
        ledger: { signupGrant: 200 },
      }),
    ).toThrow(ConfigError);
  });

  it("rejects signupGrant zero scalar", () => {
    expect(() =>
      loadConfigFromDict({
        metering: { models: { a: "input_tokens * 1" } },
        ledger: { signupGrant: 0 },
      }),
    ).toThrow(ConfigError);
  });

  it("accepts all canonical metric variables", () => {
    const expr =
      "input_tokens + output_tokens + cache_read_tokens + cache_write_tokens + " +
      "tool_calls + search_queries + search_results + web_search_calls + code_exec_calls";
    expect(() => loadConfigFromDict({ metering: { models: { "*": expr } } })).not.toThrow();
  });

  // ── WS2: `this_tool_calls` is scoped to `tools` expressions only ──
  it("rejects this_tool_calls in a models expression", () => {
    expect(() =>
      loadConfigFromDict({
        metering: { models: { "*": "this_tool_calls * 1" } },
      }),
    ).toThrow(ConfigError);
  });

  it("accepts calls in a tools expression", () => {
    expect(() =>
      loadConfigFromDict({
        metering: {
          models: { "*": "input_tokens * 1" },
          tools: { code_exec: "calls * 10 / 1000" },
        },
      }),
    ).not.toThrow();
  });

  // ── C5/C7: config-load rejects ** and div-by-zero-prone forms via validation ──
  it("rejects exponentiation in a model expression", () => {
    expect(() =>
      loadConfigFromDict({
        metering: { models: { "gpt-4": "input_tokens ** 2" } },
      }),
    ).toThrow(ConfigError);
  });

  // ── CF1: Plan with rate_overrides loads without error ──
  it("accepts a plan with valid rateOverrides", () => {
    expect(() =>
      loadConfigFromDict({
        metering: { models: { "gpt-4": "input_tokens * 0.001" } },
        plans: {
          pro: {
            label: "Pro",
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
        metering: { models: { "gpt-4": "input_tokens * 0.001" } },
        plans: {
          basic: { label: "Basic", allowance: { amount: -10 } },
        },
      }),
    ).toThrow(ConfigError);
  });

  // ── CF3: Empty sections are allowed ──
  it("accepts empty/absent tools, search, cache, fixed sections alongside valid models", () => {
    expect(() =>
      loadConfigFromDict({
        metering: {
          models: { "gpt-4": "input_tokens * 0.001" },
          tools: {},
          search: null,
          cacheDiscount: null,
          flatJobs: {},
        },
      }),
    ).not.toThrow();
  });

  // ── CF4: minBalance as string "10" — coerced to Decimal (parity with Python) ──
  it("coerces minBalance string to Decimal", () => {
    const config = loadConfigFromDict({
      metering: { models: { "gpt-4": "input_tokens * 0.001" } },
      ledger: { minBalance: "10" as unknown as number },
    });
    expect(config.ledger.minBalance.toString()).toBe("10");
  });

  // ── C1: minBalance type coercion / boundary ──

  // C1a: minBalance: 0 is the valid boundary (check is `< 0`, so 0 must be accepted).
  it("accepts minBalance: 0 (zero is a valid balance floor)", () => {
    const config = loadConfigFromDict({
      metering: { models: { "gpt-4": "input_tokens * 0.001" } },
      ledger: { minBalance: 0 },
    });
    expect(config.ledger.minBalance.toString()).toBe("0");
  });

  // C1b: minBalance: -1 is already covered by "rejects negative minBalance" above.
  // This test explicitly documents that -1 is always rejected regardless of type.
  it("rejects minBalance: -1 (negative balance floor makes no sense)", () => {
    expect(() =>
      loadConfigFromDict({
        metering: { models: { "gpt-4": "input_tokens * 0.001" } },
        ledger: { minBalance: -1 },
      }),
    ).toThrow(ConfigError);
  });

  // CF5: Duplicate plan names rejected ──
  it("rejects two plans with the same name field", () => {
    expect(() =>
      loadConfigFromDict({
        metering: { models: { "gpt-4": "input_tokens * 0.001" } },
        plans: {
          plan_a: { label: "SameName" },
          plan_b: { label: "SameName" },
        },
      }),
    ).toThrow(ConfigError);
  });

  // ── WS3: fractional (Decimal-compatible) flat-job costs ──
  it("rejects a negative flat job cost", () => {
    expect(() =>
      loadConfigFromDict({
        metering: { models: { "gpt-4": "input_tokens * 0.001" }, flatJobs: { batch_job: -1 } },
      }),
    ).toThrow(ConfigError);
  });

  it("accepts a fractional flat job cost (no longer integer-only)", () => {
    const config = loadConfigFromDict({
      metering: { models: { "gpt-4": "input_tokens * 0.001" }, flatJobs: { batch_job: 2.5 } },
    });
    expect(config.metering.flatJobs["batch_job"].toString()).toBe("2.5");
  });

  // ── Cross-language parity fixes ──

  it("rejects an unknown top-level config key (typo)", () => {
    expect(() =>
      loadConfigFromDict({
        metering: { models: { "gpt-4": "input_tokens * 0.001" } },
        min_balnce: 5,
      }),
    ).toThrow(ConfigError);
  });

  it("rejects an unknown key in a plan definition", () => {
    expect(() =>
      loadConfigFromDict({
        metering: { models: { "gpt-4": "input_tokens * 0.001" } },
        plans: { pro: { label: "Pro", free_allownce: 5 } },
      }),
    ).toThrow(ConfigError);
  });

  it("rejects an unknown key in a bucket definition", () => {
    expect(() =>
      loadConfigFromDict({
        metering: { models: { "gpt-4": "input_tokens * 0.001" } },
        ledger: { buckets: { gifted: { label: "Gifted", priority: 1, expres: true } } },
      }),
    ).toThrow(ConfigError);
  });

  it("rejects a plan definition missing the required 'name' field", () => {
    expect(() =>
      loadConfigFromDict({
        metering: { models: { "gpt-4": "input_tokens * 0.001" } },
        plans: { pro: {} },
      }),
    ).toThrow(ConfigError);
  });

  it("rejects version !== 1", () => {
    expect(() =>
      loadConfigFromDict({
        version: 2,
        metering: { models: { "gpt-4": "input_tokens * 0.001" } },
      }),
    ).toThrow(ConfigError);
  });

  it("accepts version: 1", () => {
    expect(() =>
      loadConfigFromDict({
        version: 1,
        metering: { models: { "gpt-4": "input_tokens * 0.001" } },
      }),
    ).not.toThrow();
  });

  it("rejects a negative signupGrant amount", () => {
    expect(() =>
      loadConfigFromDict({
        metering: { models: { "gpt-4": "input_tokens * 0.001" } },
        ledger: {
          signupGrant: { amount: -1, bucket: "gifted" },
          buckets: { gifted: { label: "Gifted", priority: 10 } },
        },
      }),
    ).toThrow(ConfigError);
  });

  it("rejects signupGrant without ledger buckets", () => {
    expect(() =>
      loadConfigFromDict({
        metering: { models: { a: "input_tokens * 1" } },
        ledger: { signupGrant: { amount: 50, bucket: "gifted" } },
      }),
    ).toThrow(ConfigError);
  });

  it("rejects signupGrant referencing unknown bucket", () => {
    expect(() =>
      loadConfigFromDict({
        metering: { models: { a: "input_tokens * 1" } },
        ledger: {
          signupGrant: { amount: 50, bucket: "missing" },
          buckets: { gifted: { label: "Gifted", priority: 10 } },
        },
      }),
    ).toThrow(ConfigError);
  });

  it("does not inject a default * tool when tools is explicitly provided without one", () => {
    // Mirrors Python: `tools` only gets its default_factory value when the key
    // is entirely absent; a user-supplied map (even without `*`) is used as-is.
    const config = loadConfigFromDict({
      metering: {
        models: { "gpt-4": "input_tokens * 0.001" },
        tools: { code_exec: "calls * 10 / 1000" },
      },
    });
    expect(config.metering.tools).toEqual({ code_exec: "calls * 10 / 1000" });
  });

  it("populates perOperation on a plan (was silently dropped)", () => {
    const config = loadConfigFromDict({
      metering: { models: { "gpt-4": "input_tokens * 0.001" } },
      plans: {
        pro: {
          label: "Pro",
          safety: {
            perOperation: {
              agent: { billingMode: "overdraft", overdraftFloor: -30, maxConcurrent: 1 },
            },
          },
        },
      },
    });
    const policy = config.plans!.pro.safety!.perOperation!.agent;
    expect(policy.billingMode).toBe("overdraft");
    expect(policy.overdraftFloor!.toString()).toBe("-30");
    expect(policy.maxConcurrent).toBe(1);
  });

  it("accepts snake_case per_operation with nested snake_case keys", () => {
    const config = loadConfigFromDict({
      metering: { models: { "gpt-4": "input_tokens * 0.001" } },
      plans: {
        pro: {
          label: "Pro",
          safety: {
            per_operation: {
              agent: { billing_mode: "overdraft", overdraft_floor: -30 },
            },
          },
        },
      },
    });
    expect(config.plans!.pro.safety!.perOperation!.agent.billingMode).toBe("overdraft");
  });

  it("rejects an invalid billingMode in perOperation", () => {
    expect(() =>
      loadConfigFromDict({
        metering: { models: { "gpt-4": "input_tokens * 0.001" } },
        plans: {
          pro: { label: "Pro", safety: { perOperation: { agent: { billingMode: "bogus" } } } },
        },
      }),
    ).toThrow(ConfigError);
  });

  // ── WS9b: allowancePeriod on plan definitions ──
  describe("allowancePeriod", () => {
    for (const period of ["calendar_month", "rolling_30d", "anniversary"] as const) {
      it(`loads successfully with allowancePeriod: "${period}"`, () => {
        const config = loadConfigFromDict({
          metering: { models: { "gpt-4": "input_tokens * 0.001" } },
          plans: {
            pro: { label: "Pro", allowance: { period } },
          },
        });
        expect(config.plans!.pro.allowance!.period).toBe(period);
      });
    }

    it("rejects an invalid allowancePeriod value", () => {
      expect(() =>
        loadConfigFromDict({
          metering: { models: { "gpt-4": "input_tokens * 0.001" } },
          plans: {
            pro: { label: "Pro", allowance: { period: "weekly" as any } }, // eslint-disable-line @typescript-eslint/no-explicit-any
          },
        }),
      ).toThrow(ConfigError);
    });

    it("defaults to calendar_month when omitted", () => {
      const config = loadConfigFromDict({
        metering: { models: { "gpt-4": "input_tokens * 0.001" } },
        plans: {
          pro: { label: "Pro" },
        },
      });
      expect(config.plans!.pro.allowance!.period).toBe("calendar_month");
    });
  });

  // ── Credit buckets: config-parsing-level validation and defaults ─────────
  // (store-level runtime resolution/expiry checks live in tests/tiers.test.ts)
  describe("buckets", () => {
    const metering = { models: { "gpt-4": "input_tokens * 0.001" } } as const;

    it("is absent by default (no buckets section)", () => {
      const config = loadConfigFromDict({ metering });
      expect(config.ledger.buckets).toBeUndefined();
    });

    it("loads a single bucket with all fields set", () => {
      const config = loadConfigFromDict({
        metering,
        ledger: {
          buckets: {
            gifted: {
              label: "Gifted Credits",
              priority: 10,
              expires: true,
              ttlDays: 30,
            },
          },
        },
      });
      expect(config.ledger.buckets!.gifted).toEqual({
        label: "Gifted Credits",
        priority: 10,
        expires: true,
        ttlDays: 30,
        allowOverdraft: false,
        default: false,
      });
    });

    it("defaults label to the config key, priority to 0, expires/allowOverdraft/default to false", () => {
      const config = loadConfigFromDict({
        metering,
        ledger: { buckets: { basic: {} } },
      });
      expect(config.ledger.buckets!.basic).toEqual({
        label: "basic",
        priority: 0,
        expires: false,
        ttlDays: null,
        allowOverdraft: false,
        default: false,
      });
    });

    it("accepts snake_case bucket fields (ttl_days / allow_overdraft / default)", () => {
      const config = loadConfigFromDict({
        metering,
        ledger: {
          buckets: {
            gifted: {
              label: "Gifted",
              priority: 10,
              expires: true,
              ttl_days: 14,
            },
            purchased: {
              label: "Purchased",
              priority: 20,
              expires: false,
              default: true,
              allow_overdraft: true,
            },
          },
        },
      });
      expect(config.ledger.buckets!.gifted.ttlDays).toBe(14);
      expect(config.ledger.buckets!.purchased["default"]).toBe(true);
      expect(config.ledger.buckets!.purchased.allowOverdraft).toBe(true);
    });

    it("rejects an explicit empty buckets object (ambiguous — omit the key instead)", () => {
      expect(() => loadConfigFromDict({ metering, ledger: { buckets: {} } })).toThrow(ConfigError);
    });

    it("rejects buckets that is not a dict (e.g. an array)", () => {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      expect(() => loadConfigFromDict({ metering, ledger: { buckets: [] as any } })).toThrow(
        ConfigError,
      );
    });

    it("rejects more than one bucket with allowOverdraft: true", () => {
      expect(() =>
        loadConfigFromDict({
          metering,
          ledger: {
            buckets: {
              a: { label: "A", priority: 10, allowOverdraft: true },
              b: { label: "B", priority: 20, allowOverdraft: true },
            },
          },
        }),
      ).toThrow(ConfigError);
    });

    it("rejects more than one bucket with default: true", () => {
      expect(() =>
        loadConfigFromDict({
          metering,
          ledger: {
            buckets: {
              a: { label: "A", priority: 10, default: true },
              b: { label: "B", priority: 20, default: true },
            },
          },
        }),
      ).toThrow(ConfigError);
    });

    it("accepts exactly one allowOverdraft bucket and one default (may be the same or different)", () => {
      expect(() =>
        loadConfigFromDict({
          metering,
          ledger: {
            buckets: {
              a: { label: "A", priority: 10, allowOverdraft: true },
              b: { label: "B", priority: 20, default: true },
            },
          },
        }),
      ).not.toThrow();
    });

    it("rejects ttlDays: 0", () => {
      expect(() =>
        loadConfigFromDict({
          metering,
          ledger: {
            buckets: { a: { label: "A", priority: 10, expires: true, ttlDays: 0 } },
          },
        }),
      ).toThrow(ConfigError);
    });

    it("rejects a negative ttlDays", () => {
      expect(() =>
        loadConfigFromDict({
          metering,
          ledger: {
            buckets: { a: { label: "A", priority: 10, expires: true, ttlDays: -1 } },
          },
        }),
      ).toThrow(ConfigError);
    });

    it("accepts a positive ttlDays", () => {
      const config = loadConfigFromDict({
        metering,
        ledger: { buckets: { a: { label: "A", priority: 10, expires: true, ttlDays: 1 } } },
      });
      expect(config.ledger.buckets!.a.ttlDays).toBe(1);
    });

    it("does not require ttlDays on a non-expiring bucket", () => {
      expect(() =>
        loadConfigFromDict({
          metering,
          ledger: {
            buckets: { a: { label: "A", priority: 10, expires: false } },
          },
        }),
      ).not.toThrow();
    });

    it("preserves insertion order of multiple buckets (sorting is a store-level concern)", () => {
      const config = loadConfigFromDict({
        metering,
        ledger: {
          buckets: {
            c: { label: "C", priority: 5 },
            a: { label: "A", priority: 1 },
            b: { label: "B", priority: 3 },
          },
        },
      });
      expect(Object.keys(config.ledger.buckets!)).toEqual(["c", "a", "b"]);
    });
  });

  describe("billing", () => {
    const metering = { models: { "*": "input_tokens * 1" } } as const;

    it("accepts a valid billing section with subscriptions and topups", () => {
      expect(() =>
        loadConfigFromDict({
          metering,
          plans: { pro: { label: "Pro" } },
          billing: {
            currency: "USD",
            subscriptions: {
              "pro-monthly": {
                plan: "pro",
                interval: "month",
                grant: { mode: "allowance" },
                providers: { stripe: { price_id: "price_pro" } },
              },
            },
            topups: {
              credits: {
                deposit_to: "purchased",
                credits_per_unit: 1000,
                providers: { stripe: { price_id: "price_topup" } },
              },
            },
          },
        }),
      ).not.toThrow();
    });

    it("rejects billing subscription referencing unknown plan", () => {
      expect(() =>
        loadConfigFromDict({
          metering,
          plans: { pro: { label: "Pro" } },
          billing: {
            subscriptions: { "pro-monthly": { plan: "nope", grant: { mode: "allowance" } } },
          },
        }),
      ).toThrow(ConfigError);
    });

    it("rejects billing subscription when plans section is absent", () => {
      expect(() =>
        loadConfigFromDict({
          metering,
          billing: {
            subscriptions: { "pro-monthly": { plan: "pro", grant: { mode: "allowance" } } },
          },
        }),
      ).toThrow(ConfigError);
    });

    it("rejects cycle_grant without credits", () => {
      expect(() =>
        loadConfigFromDict({
          metering,
          plans: { pro: { label: "Pro" } },
          billing: {
            subscriptions: {
              annual: { plan: "pro", grant: { mode: "cycle_grant" } },
            },
          },
        }),
      ).toThrow(ConfigError);
    });

    it("rejects cycle_grant without bucket", () => {
      expect(() =>
        loadConfigFromDict({
          metering,
          plans: { pro: { label: "Pro" } },
          billing: {
            subscriptions: {
              annual: { plan: "pro", grant: { mode: "cycle_grant", credits: 500 } },
            },
          },
        }),
      ).toThrow(ConfigError);
    });

    it("rejects topup without depositTo", () => {
      expect(() =>
        loadConfigFromDict({
          metering,
          billing: { topups: { x: { creditsPerUnit: 1000 } } },
        }),
      ).toThrow(ConfigError);
    });

    it("rejects topup depositTo referencing unknown bucket when buckets defined", () => {
      expect(() =>
        loadConfigFromDict({
          metering,
          ledger: {
            buckets: { gifted: { label: "Gifted", priority: 10 } },
          },
          billing: { topups: { x: { depositTo: "purchased" } } },
        }),
      ).toThrow(ConfigError);
    });

    it("rejects allowance grant with extra fields", () => {
      expect(() =>
        loadConfigFromDict({
          metering,
          plans: { pro: { label: "Pro" } },
          billing: {
            subscriptions: {
              monthly: { plan: "pro", grant: { mode: "allowance", credits: 500 } },
            },
          },
        }),
      ).toThrow(ConfigError);
    });

    it("rejects unknown key in billing topup", () => {
      expect(() =>
        loadConfigFromDict({
          metering,
          billing: { topups: { x: { tier: "purchased" } } },
        }),
      ).toThrow(ConfigError);
    });
  });

  describe("entitlements", () => {
    const metering = { models: { "*": "input_tokens * 1" } } as const;

    it("treats missing maxCalls as unlimited (null)", () => {
      const config = loadConfigFromDict({
        metering,
        plans: {
          pro: {
            label: "Pro",
            entitlements: { ai_chat: { value: true } },
          },
        },
      });
      expect(config.plans!.pro.entitlements!.ai_chat.maxCalls).toBeNull();
    });

    it("accepts explicit max_calls limit", () => {
      const config = loadConfigFromDict({
        metering,
        plans: {
          pro: {
            label: "Pro",
            entitlements: { export: { max_calls: 5, period: "monthly", on_exceed: "deny" } },
          },
        },
      });
      expect(config.plans!.pro.entitlements!.export.maxCalls).toBe(5);
    });

    it("rejects negative maxCalls", () => {
      expect(() =>
        loadConfigFromDict({
          metering,
          plans: {
            pro: {
              label: "Pro",
              entitlements: { export: { max_calls: -1 } },
            },
          },
        }),
      ).toThrow(ConfigError);
    });

    it("rejects plan rateOverrides referencing unknown model", () => {
      expect(() =>
        loadConfigFromDict({
          metering,
          plans: {
            pro: {
              label: "Pro",
              rateOverrides: { "unknown-model": "input_tokens * 0.003" },
            },
          },
        }),
      ).toThrow(ConfigError);
    });

    it("accepts wildcard rateOverrides key", () => {
      expect(() =>
        loadConfigFromDict({
          metering,
          plans: {
            pro: {
              label: "Pro",
              rateOverrides: { "*": "input_tokens * 0.003" },
            },
          },
        }),
      ).not.toThrow();
    });
  });
});
