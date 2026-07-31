import { describe, expect, it } from "vitest";

import { canonicalBursarConfigDict, loadConfigFromDict } from "../src/config.js";
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
  it("accepts and canonicalizes the typed catalog", () => {
    const parsed = loadConfigFromDict(baseConfig());
    expect(parsed.plans.pro.creditAllowance?.amount.toString()).toBe("10");
    expect(parsed.plans.pro.revisionPolicy).toBe("immediate");
    const canonical = canonicalBursarConfigDict(baseConfig());
    expect(
      ((canonical.credits as Record<string, unknown>).policies as Record<string, unknown>).invoice,
    ).toEqual({ type: "credit_line", limit: "500.000000" });
  });

  it("projects public plans and prices without provider identifiers", () => {
    const config = baseConfig() as ReturnType<typeof baseConfig> & Record<string, unknown>;
    config.catalog = {
      default_plan: "pro",
      activation: { mode: "on_publish" },
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
    expect(projected.plans[0]?.offers[0]).toMatchObject({
      key: "pro_monthly",
      price: { amountMinor: 1900, currency: "USD" },
    });
    expect(JSON.stringify(projected)).not.toContain("price_secret");
  });

  it("allows partial rate cards for unused operations", () => {
    const parsed = loadConfigFromDict(baseConfig());
    expect(parsed.pricing?.rateCards.standard.operations.image).toBeUndefined();
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

  it("requires version explicitly", () => {
    const { version: _, ...config } = baseConfig();
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
