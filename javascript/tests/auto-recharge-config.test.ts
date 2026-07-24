import { describe, expect, it } from "vitest";

import { loadConfigFromDict } from "../src/config.js";

describe("auto-recharge Bursar configuration", () => {
  const base = {
    version: 1,
    credits: {
      buckets: { purchased: {} },
      spend_order: ["purchased"],
      default_bucket: "purchased",
    },
    payments: {
      topups: {
        small_pack: {
          credits: 1000,
          bucket: "purchased",
          providers: { dodo: { lookup: { type: "product_id", value: "pdt_topup" } } },
        },
      },
      auto_recharge: {
        trigger: { balance_below: 5000 },
        purchase: { topup: "small_pack", quantity: 1 },
        limit: { max_purchases: 3, period: { unit: "day", count: 30, anchor: "rolling" } },
      },
    },
  };

  it("parses one configured top-up policy", () => {
    expect(loadConfigFromDict(base).payments?.autoRecharge?.trigger.balanceBelow.toString()).toBe(
      "5000",
    );
  });

  it("rejects auto-recharge references to unknown top-ups", () => {
    expect(() =>
      loadConfigFromDict({
        ...base,
        payments: {
          ...base.payments,
          auto_recharge: { ...base.payments.auto_recharge, purchase: { topup: "missing" } },
        },
      }),
    ).toThrow(/unknown top-up/);
  });
});
