import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { afterAll, describe, expect, it } from "vitest";

import { projectPublicCatalog } from "../src/catalog.js";
import { type BursarConfigData, loadConfigFromDict } from "../src/config.js";
import { loadConfigFile } from "../src/load-config-file.js";
import { PricingEngine } from "../src/engine.js";

const tempDir = mkdtempSync(join(tmpdir(), "bursar-config-engine-"));
afterAll(() => rmSync(tempDir, { recursive: true, force: true }));

function productionConfig(): BursarConfigData {
  return {
    version: 1,
    catalog: { default_plan: "pro" },
    pricing: {
      operations: {
        completion: {
          measures: {
            input_tokens: { unit: "token" },
            output_tokens: { unit: "token" },
          },
          dimensions: {
            model: { type: "string", required: true },
            region: { type: "string", required: false },
            premium: { type: "boolean", required: false },
            weight: { type: "number", required: false },
          },
        },
      },
      rate_cards: {
        standard: {
          operations: {
            completion: {
              rules: [
                {
                  when: { model: { op: "in", values: ["gpt-4", "gpt-4o"] } },
                  charge: {
                    type: "per_unit",
                    measure: "input_tokens",
                    unit_size: "1000",
                    rate: "2.000000",
                  },
                },
                {
                  when: { premium: { op: "eq", value: true } },
                  charge: { type: "flat", amount: "3.000000" },
                },
                {
                  when: { model: { op: "not_in", values: ["other", "claude-3"] } },
                  charge: {
                    type: "package",
                    measure: "input_tokens",
                    units: "1000",
                    amount: "1.000000",
                    rounding: "nearest",
                  },
                },
                {
                  when: { model: { op: "prefix", value: "claude-" } },
                  charge: {
                    type: "graduated",
                    measure: "input_tokens",
                    tiers: [{ up_to: "1000", rate: "1.000000" }, { rate: "0.500000" }],
                  },
                },
                {
                  when: { weight: { op: "range", gte: "1", lt: "10" } },
                  charge: {
                    type: "volume",
                    measure: "input_tokens",
                    tiers: [{ up_to: "1000", rate: "0.250000" }, { rate: "0.100000" }],
                  },
                },
                {
                  when: { region: { op: "eq", value: "IN" } },
                  charge: {
                    type: "sum",
                    components: [
                      { type: "flat", amount: "0.500000" },
                      { type: "expression", formula: "input_tokens / 1000" },
                    ],
                  },
                },
              ],
              unmatched: {
                action: "charge",
                charge: {
                  type: "sum",
                  components: [
                    { type: "per_unit", measure: "input_tokens", rate: "1.000000" },
                    { type: "per_unit", measure: "output_tokens", rate: "0.500000" },
                  ],
                },
              },
            },
          },
        },
        inherited: { extends: "standard", operations: {} },
      },
    },
    credits: {
      buckets: {
        purchased: { priority: 10, expiry: { type: "never" } },
        cycle: {
          priority: 20,
          expiry: {
            type: "after_grant",
            interval: { unit: "month", count: 1 },
            timezone: "UTC",
          },
        },
      },
      default_bucket: "purchased",
      policies: { prepaid: { type: "prepaid" } },
      grant_programs: {},
      display: { currency: "USD", units_per_major: "1000" },
    },
    entitlements: { features: { premium: { type: "boolean", default: false } } },
    admission: {
      policies: {
        interactive: { max_in_flight: 4, operations: { completion: { max_in_flight: 2 } } },
      },
    },
    plans: {
      pro: {
        display_name: "Pro",
        rank: 1,
        rate_card: "inherited",
        allowed_operations: ["completion"],
        features: { premium: true },
        quotas: {},
        credit_policy: "prepaid",
      },
    },
    commerce: {
      providers: {
        stripe: { type: "stripe" },
        dodo: { type: "dodo" },
        storefront: { type: "custom", adapter: "storefront" },
      },
      offers: {
        pro_monthly: {
          type: "subscription",
          display_name: "Pro monthly",
          description: "Production learning plan",
          sort_order: 1,
          price: { amount_minor: 1900, currency: "USD", tax_behavior: "exclusive" },
          providers: { stripe: { type: "stripe_price", price_id: "price_pro" } },
          plan: "pro",
          billing_interval: { unit: "month" },
          trial: { unit: "day", count: 14 },
          cycle_grant: {
            amount: "100",
            bucket: "cycle",
            renewal: "replace_previous",
            expiry: { type: "subscription_end" },
          },
        },
        credit_pack: {
          type: "topup",
          display_name: "Credit pack",
          availability: { starts_at: "2026-01-01T00:00:00Z", regions: ["IN", "US"] },
          price: { amount_minor: 500, currency: "USD" },
          providers: { dodo: { type: "dodo_product", product_id: "prod_pack" } },
          credits_per_unit: "50",
          quantity: { minimum: 1, maximum: 3, default: 1 },
          bucket: "purchased",
          expiry: {
            type: "end_of_window",
            window: { type: "calendar", unit: "month", timezone: "UTC" },
          },
          lot_behavior: "merge_and_refresh",
        },
        custom_pack: {
          type: "topup",
          display_name: "Custom pack",
          price: { amount_minor: 100, currency: "USD" },
          providers: {
            storefront: {
              type: "custom_object",
              object_kind: "one_time",
              external_id: "custom_pack",
            },
          },
          credits_per_unit: "10",
          bucket: "purchased",
        },
      },
      subscription_changes: {
        upgrade: { effective: "immediate", proration: "prorated" },
        downgrade: { effective: "renewal", proration: "none", payment_failure: "apply_change" },
      },
      auto_recharge: {
        eligible_topups: ["credit_pack"],
        balance_below: { minimum: "5", maximum: "10", default: "7" },
        rearm_above: "20",
        quantity: { minimum: 1, maximum: 2, default: 1 },
        limits: {
          max_purchases: 5,
          window: { type: "rolling", duration: { unit: "day", count: 1 } },
          max_charge_minor: 1000,
          cooldown: { unit: "minute", count: 30 },
          max_consecutive_failures: 2,
        },
      },
    },
  };
}

describe("configuration and pricing public workflow", () => {
  it("loads a production-shaped JSON config and prices through an inherited card", async () => {
    const path = join(tempDir, "production.json");
    writeFileSync(path, JSON.stringify(productionConfig()));

    const raw = await loadConfigFile(path);
    const parsed = loadConfigFromDict(raw);
    const pricing = PricingEngine.fromDict(raw);

    expect(parsed.commerce.offers.pro_monthly?.type).toBe("subscription");
    expect(parsed.commerce.offers.credit_pack?.type).toBe("topup");
    expect(parsed.commerce.autoRecharge?.limits.window.type).toBe("rolling");
    expect(pricing.getRateCardForPlan("pro")).toBe("inherited");
    expect(
      pricing
        .calculate(
          {
            operation: "completion",
            measures: { input_tokens: 200 },
            dimensions: { model: "other", region: "IN" },
          },
          { rateCard: "inherited" },
        )
        .total.toString(),
    ).toBe("0.7");
    const catalog = projectPublicCatalog(parsed);
    expect(catalog.defaultPlan).toBe("pro");
    expect(catalog.plans[0]?.offers.map((offer) => offer.key)).toEqual(["pro_monthly"]);
    expect(JSON.stringify(catalog)).not.toContain("price_pro");
  });
});
