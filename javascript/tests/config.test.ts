import { describe, expect, it } from "vitest";

import {
  canonicalBursarConfigDict,
  loadCatalogRollout,
  loadConfigFromDict,
  validateCatalogRollout,
} from "../src/config.js";
import { projectPublicCatalog } from "../src/catalog.js";
import { ConfigError } from "../src/errors.js";

export const baseConfig = () => ({
  version: 1,
  pricing: {
    operations: {
      completion: {
        measures: {
          input_tokens: { unit: "token" },
          output_tokens: { unit: "token" },
        },
        dimensions: { model: { type: "string" } },
      },
      image: {
        measures: { images: { unit: "image" } },
        dimensions: {},
      },
    },
    rate_cards: {
      standard: {
        operations: {
          completion: {
            rules: [
              {
                when: { model: { op: "prefix", value: "gpt-" } },
                charge: {
                  type: "per_unit",
                  measure: "input_tokens",
                  rate: "2.000000",
                },
              },
            ],
            unmatched: {
              action: "charge",
              charge: {
                type: "sum",
                components: [
                  {
                    type: "per_unit",
                    measure: "input_tokens",
                    rate: "1.000000",
                  },
                  {
                    type: "per_unit",
                    measure: "output_tokens",
                    rate: "1.000000",
                  },
                ],
              },
            },
          },
        },
      },
    },
  },
  credits: {
    accounting: { unit: "credit", scale: 6, rounding: "half_up" },
    buckets: {
      gifted: {
        priority: 10,
        expiry: {
          type: "after_grant",
          interval: { unit: "day", count: 7 },
          timezone: "UTC",
        },
      },
      purchased: { priority: 20 },
    },
    default_bucket: "purchased",
    policies: {
      prepaid: { type: "prepaid" },
      invoice: { type: "credit_line", limit: "500.000000" },
    },
  },
  entitlements: {
    features: {
      tutor_chat: { type: "boolean", default: false },
      access_level: {
        type: "enum",
        values: ["basic", "full"],
        default: "basic",
      },
    },
  },
  admission: {
    policies: {
      interactive: {
        max_in_flight: 5,
        operations: { completion: { max_in_flight: 2 } },
      },
    },
  },
  plans: {
    pro: {
      display_name: "Pro",
      rank: 1,
      rate_card: "standard",
      allowed_operations: ["completion"],
      features: { tutor_chat: true, access_level: "full" },
      credit_allowance: {
        amount: "10.000000",
        priority: 15,
        window: { type: "calendar", unit: "month", timezone: "UTC" },
      },
      quotas: {
        token_budget: {
          operation: "completion",
          measure: "input_tokens",
          limit: "1000.000000",
          window: {
            type: "rolling",
            duration: { unit: "day", count: 30 },
          },
          enforcement: "block",
          emit_at_percent: [80, 100],
        },
      },
      credit_policy: "invoice",
      admission_policy: "interactive",
    },
  },
});

describe("typed v1 config", () => {
  it.each([Number.NaN, Number.POSITIVE_INFINITY])(
    "rejects non-finite programmatic numbers (%s)",
    (value) => {
      const config = baseConfig();
      config.admission.policies.interactive.max_in_flight = value;

      expect(() => loadConfigFromDict(config)).toThrow(ConfigError);
    },
  );

  it("rejects the removed catalog activation field", () => {
    const config = baseConfig() as ReturnType<typeof baseConfig> & Record<string, unknown>;
    config.catalog = { activation: { mode: "on_publish" } };

    expect(() => loadConfigFromDict(config)).toThrow(/additional properties/);
  });

  it("accepts and canonicalizes the typed catalog", () => {
    const parsed = loadConfigFromDict(baseConfig());
    expect(parsed.plans.pro!.creditAllowance?.amount.toString()).toBe("10");
    expect(parsed.plans.pro!.creditAllowance?.priority).toBe(15);
    expect(parsed.plans.pro!.evolution.defaultRollout).toBe("immediate");
    const canonical = canonicalBursarConfigDict(baseConfig());
    expect(
      ((canonical.credits as Record<string, unknown>).policies as Record<string, unknown>).invoice,
    ).toEqual({ type: "credit_line", limit: "500.000000" });
  });

  it("supports new-assignments-only defaults and strict release overrides", () => {
    const config = baseConfig();
    Object.assign(config.plans.pro, {
      evolution: { default_rollout: "new_assignments_only" },
    });
    const parsed = loadConfigFromDict(config);
    expect(parsed.plans.pro!.evolution.defaultRollout).toBe("new_assignments_only");

    const rollout = loadCatalogRollout({
      plans: { pro: { effective: "immediate", include_pinned: true } },
    });
    expect(validateCatalogRollout(parsed, rollout)).toEqual({
      plans: { pro: { effective: "immediate", includePinned: true } },
    });
    expect(() =>
      validateCatalogRollout(
        parsed,
        loadCatalogRollout({ plans: { enterprise: { effective: "immediate" } } }),
      ),
    ).toThrow(/unknown plan/);
  });

  it("rejects next-renewal evolution for a plan without a subscription offer", () => {
    const config = baseConfig();
    Object.assign(config.plans.pro, {
      evolution: { default_rollout: "next_renewal" },
    });
    expect(() => loadConfigFromDict(config)).toThrow(/requires a subscription offer/);
  });

  it("defaults fixed accounting and plan rank for config authors", () => {
    const config = baseConfig();
    delete (config.credits as Partial<typeof config.credits>).accounting;
    delete (config.plans.pro as Partial<typeof config.plans.pro>).rank;

    const parsed = loadConfigFromDict(config);

    expect(parsed.credits.accounting).toEqual({
      unit: "credit",
      scale: 6,
      rounding: "half_up",
    });
    expect(parsed.plans.pro!.rank).toBe(0);
  });

  it("projects public plans and prices without provider identifiers", () => {
    const config = baseConfig() as ReturnType<typeof baseConfig> & Record<string, unknown>;
    config.catalog = {
      default_plan: "pro",
    };
    config.credits = {
      ...config.credits,
      display: { currency: "USD", units_per_major: "1000" },
    } as typeof config.credits;
    config.commerce = {
      providers: { stripe: { type: "stripe" } },
      offers: {
        pro_monthly: {
          type: "subscription",
          display_name: "Pro monthly",
          plan: "pro",
          billing_interval: { unit: "month", count: 1 },
          price: { amount_minor: 1900, currency: "USD" },
          providers: {
            stripe: { type: "stripe_price", price_id: "price_secret" },
          },
        },
      },
    };

    const projected = projectPublicCatalog(loadConfigFromDict(config));

    expect(projected.defaultPlan).toBe("pro");
    expect(projected.creditDisplay).toEqual({ currency: "USD", unitsPerMajor: "1000" });
    expect(projected.plans[0]?.allowance?.priority).toBe(15);
    expect(projected.plans[0]?.offers[0]).toMatchObject({
      key: "pro_monthly",
      price: { amountMinor: 1900, currency: "USD" },
    });
    expect(JSON.stringify(projected)).not.toContain("price_secret");
  });

  it("allows partial rate cards for unused operations", () => {
    const parsed = loadConfigFromDict(baseConfig());
    expect(parsed.pricing?.rateCards.standard!.operations.image).toBeUndefined();
  });

  it("requires pricing for every operation enabled by a plan", () => {
    const config = baseConfig();
    config.plans.pro.allowed_operations.push("image");
    expect(() => loadConfigFromDict(config)).toThrow(/without pricing/);
  });

  it("validates feature references and values", () => {
    const config = baseConfig();
    config.plans.pro.features.access_level = "enterprise";
    expect(() => loadConfigFromDict(config)).toThrow(ConfigError);
  });

  it("enforces feature types, integer bounds, and string patterns", () => {
    const config = baseConfig();
    const featureDefinitions = config.entitlements.features as Record<string, unknown>;
    const planFeatures = config.plans.pro.features as Record<string, boolean | number | string>;
    featureDefinitions.agent_limit = {
      type: "integer",
      default: 1,
      minimum: 1,
      maximum: 10,
    };
    featureDefinitions.support_tier = {
      type: "string",
      default: "standard",
      pattern: "^(standard|priority)$",
    };
    planFeatures.tutor_chat = "yes";
    planFeatures.agent_limit = 99;
    planFeatures.support_tier = "unknown";

    expect(() => loadConfigFromDict(config)).toThrow(/tutor_chat.*boolean/);
    planFeatures.tutor_chat = true;
    expect(() => loadConfigFromDict(config)).toThrow(/agent_limit.*maximum/);
    planFeatures.agent_limit = 5;
    expect(() => loadConfigFromDict(config)).toThrow(/support_tier.*pattern/);
  });

  it("validates default-bucket, policy, and allowance references", () => {
    const config = baseConfig();
    config.credits.default_bucket = "typo";
    expect(() => loadConfigFromDict(config)).toThrow(/default_bucket/);

    const creditPolicy = baseConfig();
    creditPolicy.plans.pro.credit_policy = "typo";
    expect(() => loadConfigFromDict(creditPolicy)).toThrow(/credit_policy/);

    const admissionPolicy = baseConfig();
    admissionPolicy.plans.pro.admission_policy = "typo";
    expect(() => loadConfigFromDict(admissionPolicy)).toThrow(/admission_policy/);

    const allowance = baseConfig();
    delete (allowance.credits as Partial<typeof allowance.credits>).default_bucket;
    expect(() => loadConfigFromDict(allowance)).toThrow(
      /credit_allowance requires credits.default_bucket/,
    );

    const priorityConflict = baseConfig();
    priorityConflict.plans.pro.credit_allowance.priority = 10;
    expect(() => loadConfigFromDict(priorityConflict)).toThrow(
      /credit_allowance.priority conflicts/,
    );

    const missingPriority = baseConfig();
    delete (missingPriority.plans.pro.credit_allowance as Partial<{ priority: number }>).priority;
    expect(() => loadConfigFromDict(missingPriority)).toThrow(/priority/);
  });

  it("requires matcher operators to match their dimension type", () => {
    const config = baseConfig();
    const when = config.pricing.rate_cards.standard.operations.completion.rules[0]!.when as Record<
      string,
      unknown
    >;
    when.model = {
      op: "range",
      gte: "1",
    };
    expect(() => loadConfigFromDict(config)).toThrow(/range matcher requires a number dimension/);
  });

  it("normalizes exact decimal strings for number-dimension matchers", () => {
    const config = baseConfig();
    config.pricing.operations.completion.dimensions.model.type = "number";
    const when = config.pricing.rate_cards.standard.operations.completion.rules[0]!.when as Record<
      string,
      unknown
    >;
    when.model = { op: "eq", value: "1.5" };

    const parsed = loadConfigFromDict(config);
    const matcher = parsed.pricing?.rateCards.standard!.operations.completion!.rules[0]!.when.model;

    expect(matcher?.op).toBe("eq");
    if (matcher?.op === "eq") expect(matcher.value.toString()).toBe("1.5");
  });

  it("requires compatible provider references", () => {
    const config = {
      ...baseConfig(),
      commerce: {
        providers: { stripe: { type: "stripe" } },
        offers: {
          pro_monthly: {
            type: "subscription",
            display_name: "Pro monthly",
            price: { amount_minor: 1200, currency: "USD" },
            providers: {
              stripe: {
                type: "dodo_product",
                product_id: "product_wrong_provider",
              },
            },
            plan: "pro",
            billing_interval: { unit: "month" },
          },
        },
      },
    };
    expect(() => loadConfigFromDict(config)).toThrow(/incompatible provider reference/);
  });

  it("requires version explicitly", () => {
    const config: Record<string, unknown> = { ...baseConfig() };
    delete config.version;
    expect(() => loadConfigFromDict(config)).toThrow(ConfigError);
  });

  it("validates RFC 3339 timestamps declared by the JSON Schema", () => {
    const config = baseConfig();
    const gifted = config.credits.buckets.gifted as unknown as {
      expiry: { type: string; at: string };
    };
    gifted.expiry = { type: "fixed_at", at: "2026-02-30T25:61:00Z" };
    expect(() => loadConfigFromDict(config)).toThrow(ConfigError);

    gifted.expiry = { type: "fixed_at", at: "2028-02-29T23:59:59Z" };
    expect(() => loadConfigFromDict(config)).not.toThrow();
  });
});
