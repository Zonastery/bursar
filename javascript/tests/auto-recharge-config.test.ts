import { expect, it } from "vitest";

import { loadConfigFromDict } from "../src/config.js";

it("parses opt-in auto-recharge guardrails", () => {
  const config = loadConfigFromDict({
    version: 1,
    credits: {
      accounting: { unit: "credit", scale: 6, rounding: "half_up" },
      buckets: { purchased: { priority: 10 } },
      default_bucket: "purchased",
    },
    commerce: {
      providers: { stripe: { type: "stripe" } },
      offers: {
        small_pack: {
          type: "topup",
          display_name: "1,000 credits",
          price: { amount_minor: 500, currency: "USD" },
          providers: {
            stripe: { type: "stripe_price", price_id: "price_pack" },
          },
          credits_per_unit: "1000.000000",
          quantity: { minimum: 1, maximum: 3, default: 1 },
          bucket: "purchased",
        },
      },
      auto_recharge: {
        eligible_topups: ["small_pack"],
        balance_below: {
          minimum: "100.000000",
          maximum: "5000.000000",
          default: "1000.000000",
        },
        rearm_above: "6000.000000",
        quantity: { minimum: 1, maximum: 3, default: 1 },
        limits: {
          max_purchases: 3,
          window: {
            type: "rolling",
            duration: { unit: "day", count: 30 },
          },
          max_charge_minor: 1500,
          cooldown: { unit: "hour", count: 1 },
        },
      },
    },
  });
  expect(config.commerce.autoRecharge?.balanceBelow.default.toString()).toBe("1000");
});
