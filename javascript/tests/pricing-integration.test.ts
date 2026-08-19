import { Decimal } from "decimal.js";
import pg from "pg";
import { afterAll, beforeAll, beforeEach, describe, expect, inject, it } from "vitest";

import { Bursar } from "../src/bursar.js";
import type { BursarConfigData } from "../src/config.js";
import { PostgresStore } from "../src/credits/postgres/store.js";
import { TEST_TENANT_ID, applyMigrations, truncateBursarTables } from "./helpers/bootstrap.js";

const DATABASE_URL = inject("DATABASE_URL");
const ACCOUNT_ID = "00000000-0000-0000-0000-000000000940";

const CONFIG: BursarConfigData = {
  version: 1,
  catalog: { default_plan: "pro" },
  pricing: {
    operations: {
      completion: {
        measures: { tokens: { unit: "token" } },
        dimensions: {
          region: { type: "string", required: true },
          segment: { type: "string", required: true },
          seats: { type: "number", required: true },
          cached: { type: "boolean", required: true },
        },
      },
      batch: { measures: { jobs: { unit: "job" } }, dimensions: {} },
      storage: { measures: { gigabytes: { unit: "gigabyte" } }, dimensions: {} },
    },
    rate_cards: {
      standard: {
        operations: {
          completion: {
            rules: [
              {
                when: {
                  region: { op: "in", values: ["US", "CA"] },
                  segment: { op: "not_in", values: ["free"] },
                  seats: { op: "range", gte: "2", lte: "10" },
                  cached: { op: "eq", value: true },
                },
                charge: {
                  type: "per_unit",
                  measure: "tokens",
                  rate: "0.25",
                  unit_size: "100",
                },
              },
              {
                when: { seats: { op: "eq", value: "20" } },
                charge: { type: "flat", amount: "4" },
              },
              {
                when: { region: { op: "prefix", value: "EU-" } },
                charge: { type: "flat", amount: "3" },
              },
            ],
            unmatched: {
              action: "charge",
              charge: {
                type: "sum",
                components: [
                  { type: "flat", amount: "0.5" },
                  {
                    type: "per_unit",
                    measure: "tokens",
                    rate: "0.1",
                    unit_size: "100",
                  },
                ],
              },
            },
          },
          batch: {
            rules: [],
            unmatched: {
              action: "charge",
              charge: {
                type: "graduated",
                measure: "jobs",
                tiers: [
                  { up_to: "10", rate: "1" },
                  { up_to: null, rate: "0.5" },
                ],
              },
            },
          },
          storage: {
            rules: [],
            unmatched: {
              action: "charge",
              charge: {
                type: "volume",
                measure: "gigabytes",
                tiers: [
                  { up_to: "10", rate: "1" },
                  { up_to: null, rate: "2" },
                ],
              },
            },
          },
        },
      },
      enterprise: { extends: "standard", operations: {} },
    },
  },
  credits: {
    buckets: { general: { priority: 10 } },
    default_bucket: "general",
    policies: { prepaid: { type: "prepaid" } },
    display: { currency: "USD", units_per_major: "100" },
    grant_programs: {
      welcome: {
        trigger: "account_created",
        awards: [{ amount: "25", bucket: "general" }],
        eligibility: { plans: ["pro"], regions: ["US"] },
        max_awards_per_subject: 1,
        idempotency_scope: "subject",
      },
    },
  },
  entitlements: {
    features: { premium_support: { type: "boolean", default: false } },
  },
  admission: { policies: {} },
  plans: {
    pro: {
      display_name: "Pro",
      description: "Production workloads",
      rank: 1,
      rate_card: "enterprise",
      allowed_operations: ["completion", "batch", "storage"],
      features: { premium_support: true },
      quotas: {},
      credit_policy: "prepaid",
    },
  },
  commerce: {
    providers: { stripe: { type: "stripe" } },
    offers: {
      pro_monthly: {
        type: "subscription",
        display_name: "Pro monthly",
        description: "Monthly production access",
        sort_order: 1,
        plan: "pro",
        billing_interval: { unit: "month", count: 1 },
        price: { amount_minor: 2900, currency: "USD", tax_behavior: "exclusive" },
        providers: { stripe: { type: "stripe_price", price_id: "price_private" } },
      },
      credits_100: {
        type: "topup",
        display_name: "100 credits",
        sort_order: 2,
        credits_per_unit: "100",
        quantity: { minimum: 1, maximum: 10, default: 1 },
        bucket: "general",
        price: { amount_minor: 500, currency: "USD", tax_behavior: "inclusive" },
        providers: { stripe: { type: "stripe_price", price_id: "topup_private" } },
      },
    },
  },
};

describe("production pricing integration", () => {
  const pool = new pg.Pool({ connectionString: DATABASE_URL, max: 1 });
  const store = new PostgresStore({
    postgres: pool,
    tenantId: TEST_TENANT_ID,
    providerEnvironment: "test",
  });

  beforeAll(() => applyMigrations(pool), 60_000);
  beforeEach(() => truncateBursarTables(pool));
  afterAll(() => pool.end());

  it("onboards once and exposes a provider-secret-free sellable catalog", async () => {
    const bursar = new Bursar({ creditStore: store });
    await bursar.catalog.publishAndActivate(CONFIG, "production-catalog");

    const created = await bursar.accounts.onAccountCreated({
      accountId: ACCOUNT_ID,
      eventKey: "signup:940",
      region: "US",
      metadata: { referenceType: "signup" },
    });
    const replay = await bursar.accounts.onAccountCreated({
      accountId: ACCOUNT_ID,
      eventKey: "signup:940",
      region: "US",
    });
    const catalog = await bursar.catalog.publicView();

    expect(created).toMatchObject({ planKey: "pro", planAssigned: true });
    expect(created.grants).toHaveLength(1);
    expect(created.grants[0]).toMatchObject({ replayed: false, error: null });
    expect(replay).toMatchObject({ planKey: "pro", planAssigned: false });
    expect(replay.grants[0]).toMatchObject({ replayed: true, error: null });
    expect((await bursar.credits.getBalance(ACCOUNT_ID)).balance.toString()).toBe("25");
    expect(catalog.plans[0]).toMatchObject({
      key: "pro",
      displayName: "Pro",
      features: { premium_support: true },
      offers: [{ key: "pro_monthly", price: { amountMinor: 2900, currency: "USD" } }],
    });
    expect(catalog.topups[0]).toMatchObject({
      key: "credits_100",
      creditsPerUnit: "100",
      quantity: { minimum: 1, maximum: 10, default: 1 },
    });
    expect(JSON.stringify(catalog)).not.toContain("_private");
  });

  it("persists exact charges across routed, graduated, and volume prices", async () => {
    const bursar = new Bursar({ creditStore: store });
    await bursar.catalog.publishAndActivate(CONFIG);
    await bursar.credits.setUserPlan(ACCOUNT_ID, "pro");
    await bursar.credits.addCredits(ACCOUNT_ID, new Decimal(100), {
      type: "purchase",
      idempotencyKey: "pricing-funds-940",
    });

    const scenarios = [
      {
        metrics: {
          operation: "completion",
          measures: { tokens: 200 },
          dimensions: {
            region: "US",
            segment: "paid",
            seats: new Decimal(3),
            cached: true,
          },
        },
        expected: "0.5",
      },
      {
        metrics: {
          operation: "completion",
          measures: { tokens: 200 },
          dimensions: { region: "US", segment: "free", seats: 3, cached: true },
        },
        expected: "0.7",
      },
      {
        metrics: {
          operation: "completion",
          measures: { tokens: 1 },
          dimensions: { region: "APAC", segment: "paid", seats: 20, cached: false },
        },
        expected: "4",
      },
      {
        metrics: {
          operation: "completion",
          measures: { tokens: 1 },
          dimensions: { region: "EU-WEST", segment: "paid", seats: 1, cached: false },
        },
        expected: "3",
      },
      { metrics: { operation: "batch", measures: { jobs: 15 } }, expected: "12.5" },
      {
        metrics: { operation: "storage", measures: { gigabytes: 15 } },
        expected: "30",
      },
    ] as const;

    for (const [index, scenario] of scenarios.entries()) {
      const result = await bursar.credits.deduct(ACCOUNT_ID, scenario.metrics, {
        idempotencyKey: `pricing-charge-${index}`,
      });
      expect(result.amount.toString()).toBe(scenario.expected);
    }

    const balance = await bursar.credits.getBalance(ACCOUNT_ID);
    const usage = await bursar.credits.listUsageCharges(ACCOUNT_ID, { limit: 20 });
    expect(balance.balance.toString()).toBe("49.3");
    expect(usage.items).toHaveLength(scenarios.length);
    expect(Decimal.sum(...usage.items.map((item) => item.charged)).toString()).toBe("50.7");
  });
});
