import { describe, expect, it } from "vitest";
import { canonicalBursarConfigDict } from "../src/config.js";
import { PricingEngine } from "../src/engine.js";

const starter = {
  version: 1,
  catalog: { default_plan: "free" },
  credits: { on_signup: null },
  plans: { free: { display_name: "Free", allowed_operations: ["report"] } },
  pricing: {
    operations: { report: { charge: { type: "flat", amount: "2" } } },
    rate_cards: { custom: { operations: {} } },
  },
};

describe("configuration shortcuts", () => {
  it("infers the sole named card and treats null signup as omitted", () => {
    const config = canonicalBursarConfigDict(starter);
    expect(config.plans?.free?.rate_card).toBe("custom");
    expect(config.credits?.grant_programs).toEqual({});
    expect(canonicalBursarConfigDict(config)).toEqual(config);
  });
  it("requires card selection with multiple cards and respects explicit inherited prices", () => {
    const priced = { unmatched: { action: "charge", charge: { type: "flat", amount: "3" } } };
    const source = {
      ...starter,
      pricing: {
        ...starter.pricing,
        rate_cards: {
          basic: { operations: { report: priced } },
          premium: { extends: "basic" },
          other: { operations: {} },
        },
      },
    };
    expect(() => canonicalBursarConfigDict(source)).toThrow();
    const config = canonicalBursarConfigDict({
      ...source,
      plans: { free: { ...starter.plans.free, rate_card: "premium" } },
    });
    const engine = PricingEngine.fromDict(config);
    expect(
      engine.calculate({ operation: "report" }, { rateCard: "premium" }).total.toString(),
    ).toBe("3");
    expect(engine.calculate({ operation: "report" }, { rateCard: "other" }).total.toString()).toBe(
      "2",
    );
  });
  it("infers Stripe references and rejects irrelevant fields", () => {
    const commerce = {
      providers: { stripe: { type: "stripe" } },
      offers: {
        pack: {
          type: "topup",
          display_name: "Pack",
          price: { amount_minor: 100, currency: "USD" },
          credits_per_unit: "10",
          bucket: "default",
          providers: { stripe: { price_id: "price_1" } },
        },
      },
    };
    const config = canonicalBursarConfigDict({ ...starter, commerce });
    expect(config.commerce?.offers?.pack?.providers.stripe?.type).toBe("stripe_price");
    const invalid = {
      ...commerce,
      offers: { pack: { ...commerce.offers.pack, providers: { stripe: { product_id: "wrong" } } } },
    };
    expect(() => canonicalBursarConfigDict({ ...starter, commerce: invalid })).toThrow();
  });
});
