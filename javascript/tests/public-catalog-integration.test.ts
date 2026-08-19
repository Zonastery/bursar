import { Decimal } from "decimal.js";
import pg from "pg";
import { afterAll, beforeAll, beforeEach, describe, expect, inject, it } from "vitest";

import { Bursar } from "../src/bursar.js";
import { PostgresBillingStore } from "../src/billing/index.js";
import type { BursarConfigData } from "../src/config.js";
import { PostgresStore } from "../src/credits/postgres/store.js";
import type { PaymentProvider } from "../src/providers/types.js";
import { TEST_TENANT_ID, applyMigrations, truncateBursarTables } from "./helpers/bootstrap.js";

const DATABASE_URL = inject("DATABASE_URL");
const USER_ID = "00000000-0000-0000-0000-000000000960";

const CONFIG = {
  version: 1,
  catalog: { default_plan: "alpha" },
  pricing: {
    operations: {
      job: { measures: { jobs: { unit: "job" } }, dimensions: {} },
    },
    rate_cards: {
      standard: {
        operations: {
          job: {
            rules: [],
            unmatched: { action: "charge", charge: { type: "flat", amount: "1" } },
          },
        },
      },
    },
  },
  credits: {
    buckets: { general: { priority: 10, expiry: { type: "never" } } },
    default_bucket: "general",
    policies: { prepaid: { type: "prepaid" } },
    grant_programs: {},
    display: { currency: "USD", units_per_major: "100" },
  },
  entitlements: { features: {} },
  admission: { policies: {} },
  plans: {
    alpha: {
      display_name: "Alpha",
      rank: 1,
      rate_card: "standard",
      allowed_operations: ["job"],
      features: {},
      quotas: {},
      credit_allowance: {
        amount: "3",
        priority: 1,
        window: {
          type: "plan_assignment",
          interval: { unit: "month", count: 1 },
          timezone: "UTC",
        },
      },
      credit_policy: "prepaid",
    },
    zebra: {
      display_name: "Zebra",
      rank: 1,
      rate_card: "standard",
      allowed_operations: ["job"],
      features: {},
      quotas: {},
      credit_policy: "prepaid",
    },
  },
  commerce: {
    providers: { stripe: { type: "stripe" } },
    offers: {
      alpha_primary: {
        type: "subscription",
        display_name: "Alpha primary",
        sort_order: 1,
        plan: "alpha",
        billing_interval: { unit: "month", count: 1 },
        price: { amount_minor: 1000, currency: "USD" },
        providers: { stripe: { type: "stripe_price", price_id: "price_alpha_primary" } },
      },
      alpha_secondary: {
        type: "subscription",
        display_name: "Alpha secondary",
        sort_order: 1,
        plan: "alpha",
        billing_interval: { unit: "month", count: 1 },
        price: { amount_minor: 1200, currency: "USD" },
        providers: { stripe: { type: "stripe_price", price_id: "price_alpha_secondary" } },
      },
    },
  },
} satisfies BursarConfigData;

const PROVIDER: PaymentProvider = {
  provider: "stripe",
  async createCheckoutSession({ returnUrl }) {
    return { url: returnUrl };
  },
  async handleWebhook() {
    return {
      received: true,
      retryable: false,
      provider: "stripe",
      eventId: null,
      eventType: null,
    };
  },
};

describe.runIf(DATABASE_URL)("public catalog commerce integration", () => {
  const pool = new pg.Pool({ connectionString: DATABASE_URL, max: 1 });

  beforeAll(() => applyMigrations(pool), 60_000);
  beforeEach(() => truncateBursarTables(pool));
  afterAll(() => pool.end());

  it("projects tie-broken catalog windows and runs the configured facade hook", async () => {
    const bursar = new Bursar({
      creditStore: new PostgresStore({
        postgres: pool,
        tenantId: TEST_TENANT_ID,
        providerEnvironment: "test",
      }),
      billingStore: new PostgresBillingStore({
        postgres: pool,
        tenantId: TEST_TENANT_ID,
        providerEnvironment: "test",
      }),
      commerceOptions: {
        providerEnvironment: "test",
        providers: { stripe: () => PROVIDER },
      },
    });
    await bursar.catalog.publishAndActivate(CONFIG);

    const catalog = await bursar.catalog.publicView();
    expect(bursar.requireCommerce()).toBe(bursar.commerce);
    expect(catalog.plans.map((plan) => plan.key)).toEqual(["alpha", "zebra"]);
    expect(catalog.plans[0]?.allowance).toMatchObject({
      amount: "3",
      window: { type: "plan_assignment", unit: "month", count: 1, timezone: "UTC" },
    });
    expect(catalog.plans[0]?.offers.map((offer) => offer.key)).toEqual([
      "alpha_primary",
      "alpha_secondary",
    ]);

    await bursar.credits.setUserPlan(USER_ID, "zebra");
    await bursar.credits.addCredits(USER_ID, new Decimal(2), {
      type: "purchase",
      idempotencyKey: "public-catalog-seed",
    });
    const charge = await bursar.credits.deductFlatJob(USER_ID, "job", {
      idempotencyKey: "public-catalog-charge",
    });
    expect(charge.amount.toString()).toBe("1");
    expect((await bursar.credits.getBalance(USER_ID)).balance.toString()).toBe("1");
  });
});
