import { describe, expect, it } from "vitest";
import { loadConfigFromDict } from "../src/config.js";

describe("auto-recharge Bursar configuration", () => {
  const base = {
    version: 1,
    metering: { models: { "*": "input_tokens + output_tokens" } },
    billing: {
      topups: {
        default: { deposit_to: "purchased", providers: { dodo: { product_id: "pdt_topup" } } },
      },
      auto_recharge: {
        enabled: true,
        threshold_credits: 5000,
        topup_key: "default",
        quantity: 1,
        max_recharges: 3,
        window_days: 30,
      },
    },
  };

  it("parses the configured top-up policy", () => {
    expect(loadConfigFromDict(base).billing?.autoRecharge).toEqual({
      enabled: true,
      thresholdCredits: 5000,
      topupKey: "default",
      quantity: 1,
      maxRecharges: 3,
      windowDays: 30,
    });
  });

  it("rejects an auto-recharge policy that references an unknown top-up", () => {
    expect(() =>
      loadConfigFromDict({
        ...base,
        billing: {
          ...base.billing,
          auto_recharge: { ...base.billing.auto_recharge, topup_key: "missing" },
        },
      }),
    ).toThrow(/unknown top-up/);
  });
});
