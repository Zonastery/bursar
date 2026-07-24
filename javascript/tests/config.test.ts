import { describe, expect, it } from "vitest";

import { canonicalBursarConfigDict, loadConfigFromDict } from "../src/config.js";
import { ConfigError } from "../src/errors.js";

const baseConfig = () => ({
  version: 1,
  usage: {
    operations: {
      completion: { measures: ["input_tokens", "output_tokens"], dimensions: ["model"] },
    },
    rate_cards: {
      standard: {
        prices: {
          completion: [
            { match: { model: { prefix: "gpt-" } }, formula: "input_tokens * 2" },
            { default: true, formula: "input_tokens + output_tokens" },
          ],
        },
      },
    },
  },
  credits: {
    buckets: { gifted: { expires_after: { unit: "day", count: 7 } }, purchased: {} },
    spend_order: ["gifted", "purchased"],
    default_bucket: "purchased",
    overdraft_bucket: "purchased",
  },
  plans: { pro: { display_name: "Pro", rate_card: "standard" } },
});

describe("redesigned v1 config", () => {
  it("accepts generic operation pricing", () => {
    expect(loadConfigFromDict(baseConfig()).usage?.operations.completion.measures).toEqual([
      "input_tokens",
      "output_tokens",
    ]);
  });

  it("canonicalizes decimals and uses snake_case", () => {
    const config = baseConfig();
    config.credits.signup_grant = { amount: "10.25", bucket: "gifted" };
    const canonical = canonicalBursarConfigDict(config);
    expect(canonical.credits.signup_grant.amount).toBe("10.25");
    expect(canonical).not.toHaveProperty("metering");
  });

  it.each([{ metering: { models: { "*": "1" } } }, { ledger: {} }, { billing: {} }])(
    "rejects legacy sections",
    (legacy) => expect(() => loadConfigFromDict({ version: 1, ...legacy })).toThrow(ConfigError),
  );

  it("rejects an unpriced operation", () => {
    const config = baseConfig();
    config.usage.operations.image = { measures: ["images"], dimensions: [] };
    expect(() => loadConfigFromDict(config)).toThrow(/no price/);
  });

  it("requires one final default rule", () => {
    const config = baseConfig();
    config.usage.rate_cards.standard.prices.completion = [
      { default: true, formula: "input_tokens" },
      { match: { model: { exact: "x" } }, formula: "input_tokens" },
    ];
    expect(() => loadConfigFromDict(config)).toThrow(/default rule/);
  });

  it("requires explicit credit stacking", () => {
    const config = baseConfig();
    config.plans.pro.included_credits = { amount: 10, reset: { unit: "month" } };
    config.payments = {
      subscriptions: {
        pro_monthly: {
          plan: "pro",
          billing_period: { unit: "month" },
          providers: { stripe: { lookup: { type: "price_id", value: "price_pro" } } },
          renewal_credits: {
            amount: 20,
            bucket: "purchased",
            behavior: "replace",
            on_subscription_end: "expire",
          },
        },
      },
    };
    expect(() => loadConfigFromDict(config)).toThrow(/stack_credits/);
  });
});
