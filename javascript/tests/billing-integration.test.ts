/**
 * Integration tests for PostgresBillingStore against a real Postgres.
 *
 * Mirrors Python test_billing_integration.py.
 */

import { describe, it, expect, beforeAll, beforeEach, afterAll, inject, vi } from "vitest";
import { Decimal } from "decimal.js";
import pg from "pg";
import { PostgresStore } from "../src/credits/postgres/store.js";
import { CreditsService } from "../src/credits/service.js";
import { PostgresBillingStore, BillingService, BillingEventType } from "../src/billing/index.js";
import type { BursarConfigData } from "../src/config.js";
import type {
  BillingEvent,
  BillingPreferences,
  BillingSubscriptionState,
} from "../src/billing/index.js";
import type { PaymentProvider } from "../src/providers/types.js";
import { TEST_TENANT_ID, applyMigrations, truncateBursarTables } from "./helpers/bootstrap.js";
import { mapDodoEvent } from "./helpers/dodo-fixtures.js";

const DATABASE_URL = inject("DATABASE_URL");

const USER_ID = "00000000-0000-0000-0000-000000000001";
const USER_ID2 = "00000000-0000-0000-0000-000000000002";
const USER_ID3 = "00000000-0000-0000-0000-000000000003";
const USER_ID4 = "00000000-0000-0000-0000-000000000004";
const USER_ID5 = "00000000-0000-0000-0000-000000000005";
const PROVIDER = "stripe";
const CUSTOMER_ID = "cus_test123";
const CUSTOMER_ID2 = "cus_test456";
const SUB_ID = "sub_test789";
const SUB_ID2 = "sub_test012";
const PRODUCT_ID = "prod_monthly";
const PRICE_ID = "price_monthly_1000";
const PRICE_ID_TOPUP = "price_topup_credits";
const EVENT_ID = "evt_test_001";
const DODO_PRODUCT_ID = "prod_dodo_monthly";
const TEST_INSTANT = "2025-01-01T00:00:00.000Z";

const PRICING_DICT = {
  version: 1,
  catalog: { default_plan: "free" },
  pricing: {
    operations: {
      inference: {
        measures: { tokens: { unit: "token" } },
        dimensions: {},
      },
    },
    rate_cards: {
      standard: {
        operations: {
          inference: {
            rules: [],
            unmatched: {
              action: "charge",
              charge: {
                type: "per_unit",
                measure: "tokens",
                rate: "1",
              },
            },
          },
        },
      },
    },
  },
  credits: {
    buckets: {
      purchased: {
        priority: 10,
        expiry: { type: "never" },
      },
    },
    default_bucket: "purchased",
  },
  plans: {
    free: {
      display_name: "Free",
      rank: 0,
      rate_card: "standard",
      credit_allowance: {
        amount: "1000",
        priority: 5,
        window: {
          type: "calendar",
          unit: "month",
          count: 1,
          timezone: "UTC",
        },
      },
    },
    pro: {
      display_name: "Pro",
      rank: 1,
      rate_card: "standard",
      credit_allowance: {
        amount: "100000",
        priority: 5,
        window: {
          type: "calendar",
          unit: "month",
          count: 1,
          timezone: "UTC",
        },
      },
    },
    enterprise: {
      display_name: "Enterprise",
      rank: 2,
      rate_card: "standard",
      credit_allowance: {
        amount: "1000000",
        priority: 5,
        window: {
          type: "calendar",
          unit: "month",
          count: 1,
          timezone: "UTC",
        },
      },
    },
  },
  commerce: {
    providers: {
      stripe: { type: "stripe" },
      dodo: { type: "dodo" },
    },
    offers: {
      pro_monthly: {
        type: "subscription",
        display_name: "Pro Monthly",
        price: { amount_minor: 1000, currency: "USD" },
        providers: {
          stripe: {
            type: "stripe_price",
            price_id: "price_monthly_1000",
          },
          dodo: {
            type: "dodo_product",
            product_id: "prod_dodo_monthly",
          },
        },
        plan: "pro",
        billing_interval: { unit: "month", count: 1 },
      },
      enterprise_yearly: {
        type: "subscription",
        display_name: "Enterprise Yearly",
        price: { amount_minor: 10000, currency: "USD" },
        providers: {
          stripe: {
            type: "stripe_price",
            price_id: "price_yearly_10000",
          },
        },
        plan: "enterprise",
        billing_interval: { unit: "year", count: 1 },
      },
      cycle_grant_monthly: {
        type: "subscription",
        display_name: "Cycle Grant Monthly",
        price: { amount_minor: 5000, currency: "USD" },
        providers: {
          stripe: {
            type: "stripe_price",
            price_id: "price_cycle_grant_5000",
          },
        },
        plan: "pro",
        billing_interval: { unit: "month", count: 1 },
        cycle_grant: {
          amount: "5000",
          bucket: "purchased",
          renewal: "replace_previous",
          expiry: { type: "subscription_end" },
        },
      },
      standard_topup: {
        type: "topup",
        display_name: "Standard Top-up",
        price: { amount_minor: 1000, currency: "USD" },
        providers: {
          stripe: {
            type: "stripe_price",
            price_id: "price_topup_credits",
          },
        },
        credits_per_unit: "1000",
        bucket: "purchased",
        quantity: { minimum: 1, maximum: 100, default: 1 },
      },
    },
  },
} satisfies BursarConfigData;

async function makePgComponents(pool: pg.Pool) {
  const cs = new PostgresStore({
    postgres: pool,
    tenantId: TEST_TENANT_ID,
    providerEnvironment: "test",
  });
  const cm = new CreditsService(cs);
  await cm.publishAndActivateCatalog(PRICING_DICT);
  const bs = new PostgresBillingStore({
    postgres: pool,
    tenantId: TEST_TENANT_ID,
    providerEnvironment: "test",
  });
  const bm = new BillingService(bs, { provisioning: cm });
  return { cs, cm, bs, bm };
}

// ── PostgresBillingStore (requires real Postgres) ────────────────────────

describe.runIf(DATABASE_URL)("PostgresBillingStore integration", () => {
  let pool: pg.Pool;

  beforeAll(async () => {
    pool = new pg.Pool({ connectionString: DATABASE_URL!, max: 1 });
    await applyMigrations(pool);
    await truncateBursarTables(pool);
  }, 60000);

  beforeEach(async () => {
    await truncateBursarTables(pool);
  });

  afterAll(async () => {
    if (pool) await pool.end();
  });

  // ── Sync + Resolve ───────────────────────────────────────────────────

  it("sync billing config round-trip", async () => {
    const { bs } = await makePgComponents(pool);
    const offer = await bs.resolveBillingOffer(PROVIDER, null, PRICE_ID);
    expect(offer).not.toBeNull();
    expect(offer!.offerKey).toBe("pro_monthly");
    expect(offer!.plan).toBe("pro");
  });

  it("sync billing config resolves the same offer by product id", async () => {
    const { bs } = await makePgComponents(pool);
    const offer = await bs.resolveBillingOffer("dodo", DODO_PRODUCT_ID, null);
    expect(offer).not.toBeNull();
    expect(offer!.offerKey).toBe("pro_monthly");
    expect(offer!.plan).toBe("pro");
  });

  it("sync topup config round-trip", async () => {
    const { bs } = await makePgComponents(pool);
    const topup = await bs.resolveCreditTopup(PROVIDER, null, PRICE_ID_TOPUP);
    expect(topup).not.toBeNull();
    expect(topup!.topupKey).toBe("standard_topup");
    expect(topup!.creditsPerUnit.toString()).toBe("1000");
  });

  it("unresolved offer returns null", async () => {
    const { bs } = await makePgComponents(pool);
    expect(await bs.resolveBillingOffer(PROVIDER, null, "nonexistent")).toBeNull();
  });

  it("resolve billing offer no match", async () => {
    const { bs } = await makePgComponents(pool);
    expect(await bs.resolveBillingOffer("nonexistent_provider", null, PRICE_ID)).toBeNull();
  });

  // ── Customer CRUD ────────────────────────────────────────────────────

  it("customer created roundtrip", async () => {
    const { bs } = await makePgComponents(pool);
    await bs.upsertBillingCustomer(PROVIDER, CUSTOMER_ID, USER_ID, "test@example.com");
    const uid = await bs.getBillingCustomer(PROVIDER, CUSTOMER_ID);
    expect(uid).toBe(USER_ID);
  });

  it("customer not found", async () => {
    const { bs } = await makePgComponents(pool);
    expect(await bs.getBillingCustomer(PROVIDER, "nonexistent_cus")).toBeNull();
  });

  it("customer identity cannot be rebound to another user", async () => {
    const { bs } = await makePgComponents(pool);
    await bs.upsertBillingCustomer(PROVIDER, CUSTOMER_ID, USER_ID);
    await expect(bs.upsertBillingCustomer(PROVIDER, CUSTOMER_ID, USER_ID2)).rejects.toMatchObject({
      code: "STORE_ERROR",
      details: { sqlState: "23505" },
    });
    expect(await bs.getBillingCustomer(PROVIDER, CUSTOMER_ID)).toBe(USER_ID);
  });

  it("multiple providers same customer id", async () => {
    const { bs } = await makePgComponents(pool);
    await bs.upsertBillingCustomer("stripe", CUSTOMER_ID, USER_ID);
    await bs.upsertBillingCustomer("dodo", CUSTOMER_ID, USER_ID2);
    expect(await bs.getBillingCustomer("stripe", CUSTOMER_ID)).toBe(USER_ID);
    expect(await bs.getBillingCustomer("dodo", CUSTOMER_ID)).toBe(USER_ID2);
  });

  // ── Subscription CRUD ────────────────────────────────────────────────

  it("subscription upsert and read", async () => {
    const { bs } = await makePgComponents(pool);
    const state: BillingSubscriptionState = {
      userId: USER_ID,
      provider: PROVIDER,
      providerSubscriptionId: SUB_ID,
      providerCustomerId: CUSTOMER_ID,
      offerKey: "pro_monthly",
      plan: "pro",
      status: "active",
      currentPeriodStart: "2025-01-01T00:00:00Z",
      currentPeriodEnd: "2025-02-01T00:00:00Z",
      providerUpdatedAt: "2025-01-01T00:00:00Z",
      cancelAtPeriodEnd: false,
    };
    await bs.upsertBillingSubscription(state);
    const result = await bs.getBillingSubscription(PROVIDER, SUB_ID);
    expect(result).not.toBeNull();
    expect(result!.userId).toBe(USER_ID);
    expect(result!.status).toBe("active");
    expect(result!.plan).toBe("pro");
  });

  it("subscription not found", async () => {
    const { bs } = await makePgComponents(pool);
    expect(await bs.getBillingSubscription(PROVIDER, "nonexistent_sub")).toBeNull();
  });

  it("subscription update", async () => {
    const { bs } = await makePgComponents(pool);
    await bs.upsertBillingSubscription({
      userId: USER_ID,
      provider: PROVIDER,
      providerSubscriptionId: SUB_ID,
      offerKey: "pro_monthly",
      status: "active",
      providerUpdatedAt: "2025-01-01T00:00:00Z",
      cancelAtPeriodEnd: false,
    });
    await bs.upsertBillingSubscription({
      userId: USER_ID,
      provider: PROVIDER,
      providerSubscriptionId: SUB_ID,
      offerKey: "pro_monthly",
      status: "canceled",
      providerUpdatedAt: "2025-01-02T00:00:00Z",
      cancelAtPeriodEnd: false,
    });
    const sub = await bs.getBillingSubscription(PROVIDER, SUB_ID);
    expect(sub!.status).toBe("canceled");
  });

  // ── Event idempotency ────────────────────────────────────────────────

  it("event idempotency", async () => {
    const { bm } = await makePgComponents(pool);
    const event: BillingEvent = {
      provider: PROVIDER,
      eventId: EVENT_ID,
      eventType: "customer.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      customer: { providerCustomerId: "cus_event_idempotency" },
    };
    const r1 = await bm.ingestBillingEvent(event);
    expect(r1.handled).toBe(true);
    const r2 = await bm.ingestBillingEvent(event);
    expect(r2.action).toBe("duplicate");
  });

  it("event claim complete fail cycle", async () => {
    const { bs } = await makePgComponents(pool);
    const c1 = await bs.claimBillingEvent(PROVIDER, "evt_claim_cycle", "test.event");
    expect(c1.status).toBe("claimed");
    if (c1.status !== "claimed") throw new Error("expected event claim token");
    const activeClaim = await bs.claimBillingEvent(PROVIDER, "evt_claim_cycle", "test.event");
    expect(activeClaim.status).toBe("busy");
    await bs.completeBillingEvent(PROVIDER, "evt_claim_cycle", c1.claimToken);
    const c2 = await bs.claimBillingEvent(PROVIDER, "evt_claim_cycle", "test.event");
    expect(c2.status).toBe("duplicate");
  });

  it("concurrent event claims admit one worker", async () => {
    const { bs } = await makePgComponents(pool);
    const workers = Array.from(
      { length: 12 },
      () => new pg.Pool({ connectionString: DATABASE_URL!, max: 1 }),
    );
    const ready = new Set<number>();
    let release!: () => void;
    const start = new Promise<void>((resolve) => {
      release = resolve;
    });
    try {
      const claims = await Promise.all(
        workers.map(async (worker, i) => {
          const local = new PostgresBillingStore({
            postgres: worker,
            tenantId: TEST_TENANT_ID,
            providerEnvironment: "test",
          });
          ready.add(i);
          if (ready.size === workers.length) release();
          await start;
          return local.claimBillingEvent(PROVIDER, "evt_concurrent_claim", "test.event");
        }),
      );
      expect(claims.filter((claim) => claim.status === "claimed")).toHaveLength(1);
      expect(claims.filter((claim) => claim.status === "busy")).toHaveLength(11);
      const winner = claims.find((claim) => claim.status === "claimed");
      if (winner?.status !== "claimed") throw new Error("expected one claim winner");
      await bs.completeBillingEvent(PROVIDER, "evt_concurrent_claim", winner.claimToken);
      expect(
        (await bs.claimBillingEvent(PROVIDER, "evt_concurrent_claim", "test.event")).status,
      ).toBe("duplicate");
    } finally {
      await Promise.all(workers.map((worker) => worker.end()));
    }
  }, 30000);

  it("event fail then reclaim", async () => {
    const { bs } = await makePgComponents(pool);
    const c1 = await bs.claimBillingEvent(PROVIDER, "evt_fail_retry", "test.event");
    expect(c1.status).toBe("claimed");
    if (c1.status !== "claimed") throw new Error("expected event claim token");
    await bs.failBillingEvent(PROVIDER, "evt_fail_retry", c1.claimToken, "retryable test failure");
    const c2 = await bs.claimBillingEvent(PROVIDER, "evt_fail_retry", "test.event");
    expect(c2.status).toBe("claimed");
  });

  it("rejects an invalid event claim without storing an event", async () => {
    const { bs } = await makePgComponents(pool);

    await expect(bs.claimBillingEvent(PROVIDER, "evt_invalid_claim", "")).resolves.toEqual({
      status: "invalid_request",
    });

    const result = await pool.query<{ event_count: number }>(
      `SELECT count(*)::integer AS event_count
       FROM bursar.billing_events
       WHERE tenant_id = $1::uuid
         AND provider = $2
         AND provider_environment = 'test'
         AND provider_event_id = $3`,
      [TEST_TENANT_ID, PROVIDER, "evt_invalid_claim"],
    );
    expect(result.rows[0]?.event_count).toBe(0);
  });

  it("preserves an idempotency conflict without creating another event or payload", async () => {
    const { bs } = await makePgComponents(pool);
    const eventId = "evt_claim_conflict";
    const first = await bs.claimBillingEvent(PROVIDER, eventId, "test.event", {
      amount: 100,
    });
    expect(first.status).toBe("claimed");
    if (first.status !== "claimed") throw new Error("expected initial event claim");
    await expect(bs.completeBillingEvent(PROVIDER, eventId, first.claimToken)).resolves.toBe(true);

    const conflict = await bs.claimBillingEvent(PROVIDER, eventId, "test.event", { amount: 200 });
    expect(conflict).toEqual({
      status: "idempotency_conflict",
      billingEventId: first.billingEventId,
    });

    const result = await pool.query<{
      id: string;
      status: string;
      attempt_count: number;
      payload_count: number;
    }>(
      `SELECT event.id,
              event.status,
              event.attempt_count,
              (SELECT count(*)::integer
               FROM bursar.billing_event_payloads AS payload
               WHERE payload.event_id = event.id) AS payload_count
       FROM bursar.billing_events AS event
       WHERE event.tenant_id = $1::uuid
         AND event.provider = $2
         AND event.provider_environment = 'test'
         AND event.provider_event_id = $3`,
      [TEST_TENANT_ID, PROVIDER, eventId],
    );
    expect(result.rows).toEqual([
      {
        id: first.billingEventId,
        status: "completed",
        attempt_count: 1,
        payload_count: 1,
      },
    ]);
  });

  it("preserves a terminal event claim after the retry budget is exhausted", async () => {
    const { bs } = await makePgComponents(pool);
    const eventId = "evt_claim_exhausted";
    const first = await bs.claimBillingEvent(PROVIDER, eventId, "test.event");
    expect(first.status).toBe("claimed");
    if (first.status !== "claimed") throw new Error("expected initial event claim");
    await expect(
      bs.failBillingEvent(PROVIDER, eventId, first.claimToken, "attempt 1"),
    ).resolves.toBe(true);

    for (let attempt = 2; attempt <= 3; attempt += 1) {
      const retry = await bs.claimBillingEvent(PROVIDER, eventId, "test.event");
      expect(retry.status).toBe("claimed");
      if (retry.status !== "claimed") throw new Error(`expected event retry ${attempt}`);
      expect(retry.billingEventId).toBe(first.billingEventId);
      await expect(
        bs.failBillingEvent(PROVIDER, eventId, retry.claimToken, `attempt ${attempt}`),
      ).resolves.toBe(true);
    }

    await expect(bs.claimBillingEvent(PROVIDER, eventId, "test.event")).resolves.toEqual({
      status: "max_retries_exceeded",
      billingEventId: first.billingEventId,
    });

    const result = await pool.query<{
      id: string;
      status: string;
      attempt_count: number;
      payload_count: number;
    }>(
      `SELECT event.id,
              event.status,
              event.attempt_count,
              (SELECT count(*)::integer
               FROM bursar.billing_event_payloads AS payload
               WHERE payload.event_id = event.id) AS payload_count
       FROM bursar.billing_events AS event
       WHERE event.tenant_id = $1::uuid
         AND event.provider = $2
         AND event.provider_environment = 'test'
         AND event.provider_event_id = $3`,
      [TEST_TENANT_ID, PROVIDER, eventId],
    );
    expect(result.rows).toEqual([
      {
        id: first.billingEventId,
        status: "failed",
        attempt_count: 3,
        payload_count: 1,
      },
    ]);
  });

  // ── Topup credits ────────────────────────────────────────────────────

  it("compute topup credits", async () => {
    const { bs } = await makePgComponents(pool);
    const credits = await bs.computeTopupCredits(2000, {
      topupId: "topup-compute",
      topupKey: "compute",
      amountMinor: 1000,
      creditsPerUnit: new Decimal(1000),
      depositTo: "purchased",
      currency: "USD",
      minQuantity: 1,
      maxQuantity: 100,
      defaultQuantity: 1,
      minAmountMinor: 1000,
      maxAmountMinor: 100_000,
    });
    expect(credits.toString()).toBe("2000");
  });

  it("rejects a topup amount that is not an exact catalog quantity", async () => {
    const { bs } = await makePgComponents(pool);
    const credits = await bs.computeTopupCredits(1999, {
      topupId: "topup-reject",
      topupKey: "reject",
      amountMinor: 1000,
      creditsPerUnit: new Decimal(1000),
      depositTo: "purchased",
      currency: "USD",
      minQuantity: 1,
      maxQuantity: 100,
      defaultQuantity: 1,
      minAmountMinor: 1000,
      maxAmountMinor: 100_000,
    });
    expect(credits.toString()).toBe("0");
  });

  // ── BillingService lifecycle ─────────────────────────────────────────

  it("subscription lifecycle full", async () => {
    const { cm, bm, bs } = await makePgComponents(pool);
    await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_customer_1",
      eventType: "customer.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      invoice: {
        providerInvoiceId: "in_unhandled",
        status: "draft",
        amountPaidMinor: 0,
        amountDueMinor: 0,
        currency: "USD",
      },
      customer: { providerCustomerId: CUSTOMER_ID },
    });
    await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_sub_create_1",
      eventType: "subscription.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      customer: { providerCustomerId: CUSTOMER_ID },
      subscription: {
        providerSubscriptionId: SUB_ID,
        status: "active",
        periodStart: "2025-06-01T00:00:00Z",
        periodEnd: "2025-07-01T00:00:00Z",
        refs: { productId: PRODUCT_ID, priceId: PRICE_ID },
        interval: "month",
        intervalCount: 1,
      },
    });
    const storedSub = await bs.getBillingSubscription(PROVIDER, SUB_ID);
    expect(storedSub).not.toBeNull();
    expect(storedSub!.currentPeriodStart).toBe("2025-06-01T00:00:00.000Z");
    expect(storedSub!.currentPeriodEnd).toBe("2025-07-01T00:00:00.000Z");
    expect(storedSub!.interval).toBe("month");
    expect(storedSub!.intervalCount).toBe(1);
    const plan = await cm.getUserPlan(USER_ID);
    expect(plan.planId).not.toBeNull();
    expect(plan.planAssignedAt).not.toBeNull();

    const cancelResult = await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_sub_cancel_1",
      eventType: "subscription.canceled",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      customer: { providerCustomerId: CUSTOMER_ID },
      subscription: {
        providerSubscriptionId: SUB_ID,
        status: "canceled",
        refs: { productId: PRODUCT_ID, priceId: PRICE_ID },
      },
    });
    expect(cancelResult).toEqual({
      handled: true,
      action: "subscription_canceled",
    });
    const plan2 = await cm.getUserPlan(USER_ID);
    expect(plan2.planId).toBeNull();
  });

  it("does not revoke a newer active subscription when an older one is cancelled", async () => {
    const { cm, bm, bs } = await makePgComponents(pool);

    await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_sub_stale_old_create",
      eventType: "subscription.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID3,
      subscription: {
        providerSubscriptionId: "sub_stale_old",
        status: "active",
        periodStart: "2025-06-01T00:00:00Z",
        refs: { productId: PRODUCT_ID, priceId: PRICE_ID },
      },
    });
    await bs.upsertBillingSubscription({
      provider: "dodo",
      providerSubscriptionId: "sub_stale_new",
      userId: USER_ID3,
      offerKey: "pro_monthly",
      status: "active",
      providerUpdatedAt: "2025-01-01T00:00:00Z",
      cancelAtPeriodEnd: false,
    });
    await cm.setUserPlan(USER_ID3, "pro");

    await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_sub_stale_old_cancel",
      eventType: "subscription.canceled",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID3,
      subscription: {
        providerSubscriptionId: "sub_stale_old",
        status: "canceled",
      },
    });

    const plan = await cm.getUserPlan(USER_ID3);
    expect(plan.planId).not.toBeNull();
    expect((await bs.getBillingSubscription("dodo", "sub_stale_new"))?.status).toBe("active");
  });

  it("switches cross-provider entitlement without falsifying provider status", async () => {
    const { bm, bs } = await makePgComponents(pool);
    await bm.ingestBillingEvent({
      provider: "stripe",
      eventId: "evt_provider_migration_stripe",
      eventType: "subscription.created",
      occurredAt: "2025-06-01T00:00:00Z",
      accountId: USER_ID2,
      subscription: {
        providerSubscriptionId: "sub_provider_migration_stripe",
        status: "active",
        refs: { productId: PRODUCT_ID, priceId: PRICE_ID },
      },
    });
    await bm.ingestBillingEvent({
      provider: "dodo",
      eventId: "evt_provider_migration_dodo",
      eventType: "subscription.created",
      occurredAt: "2025-06-02T00:00:00Z",
      accountId: USER_ID2,
      subscription: {
        providerSubscriptionId: "sub_provider_migration_dodo",
        status: "active",
        refs: { productId: "prod_dodo_monthly" },
      },
    });

    expect(
      (await bs.getBillingSubscription("stripe", "sub_provider_migration_stripe"))?.status,
    ).toBe("active");
    const selected = await pool.query(
      `SELECT subscription.provider
       FROM bursar.billing_entitlement_sources AS source
       JOIN bursar.billing_subscriptions AS subscription
         ON subscription.id = source.subscription_id
       WHERE source.subject_id = $1 AND source.selected`,
      [USER_ID2],
    );
    expect(selected.rows).toEqual([{ provider: "dodo" }]);
  });

  it("quarantines a second current subscription for the same provider", async () => {
    const { bm, bs } = await makePgComponents(pool);
    const first = await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_sub_conflict_first",
      eventType: "subscription.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID3,
      subscription: {
        providerSubscriptionId: "sub_conflict_first",
        status: "active",
        refs: { productId: PRODUCT_ID, priceId: PRICE_ID },
      },
    });
    const second = await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_sub_conflict_second",
      eventType: "subscription.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID3,
      subscription: {
        providerSubscriptionId: "sub_conflict_second",
        status: "active",
        refs: { productId: PRODUCT_ID, priceId: PRICE_ID },
      },
    });
    expect(first).toEqual({ handled: true, action: "subscription_created" });
    expect(second).toEqual({ handled: false, error: "subscription_conflict" });
    expect(await bs.getBillingSubscription(PROVIDER, "sub_conflict_second")).toBeNull();
    const conflicts = await pool.query(
      `SELECT duplicate_provider_subscription_id, existing_subscription_id IS NOT NULL AS linked
       FROM bursar.billing_subscription_conflicts`,
    );
    expect(conflicts.rows).toEqual([
      {
        duplicate_provider_subscription_id: "sub_conflict_second",
        linked: true,
      },
    ]);
  });

  it("closes checkout intents when payment failure or expiry is received", async () => {
    const { bm, bs } = await makePgComponents(pool);
    const failedIntent = await bs.createOrGetCheckoutIntent({
      subjectId: USER_ID,
      provider: PROVIDER,
      operationKey: "checkout-payment-failed",
      checkoutKind: "subscription",
      productKey: "pro_monthly",
      requestDigest: "11".repeat(32),
      expiresAt: new Date(Date.now() + 86_400_000).toISOString(),
    });
    const replayedIntent = await bs.createOrGetCheckoutIntent({
      subjectId: USER_ID,
      provider: PROVIDER,
      operationKey: "checkout-payment-failed",
      checkoutKind: "subscription",
      productKey: "pro_monthly",
      requestDigest: "11".repeat(32),
      expiresAt: new Date(Date.now() + 86_400_000).toISOString(),
    });
    expect(replayedIntent.id).toBe(failedIntent.id);

    await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_checkout_payment_failed",
      eventType: "payment.failed",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      metadata: { checkout_intent_id: failedIntent.id },
      payment: {
        providerPaymentId: "pay_checkout_failed",
        amountMinor: 1900,
        taxMinor: 0,
        currency: "USD",
        purpose: "subscription",
        status: "failed",
      },
    });

    const failedRow = await pool.query(
      "SELECT status FROM bursar.billing_checkout_intents WHERE id = $1",
      [failedIntent.id],
    );
    expect(failedRow.rows[0]?.status).toBe("failed");

    const expiredIntent = await bs.createOrGetCheckoutIntent({
      subjectId: USER_ID2,
      provider: PROVIDER,
      operationKey: "checkout-expired",
      checkoutKind: "subscription",
      productKey: "pro_monthly",
      requestDigest: "22".repeat(32),
      expiresAt: new Date(Date.now() + 86_400_000).toISOString(),
    });
    await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_checkout_expired",
      eventType: "checkout.expired",
      occurredAt: new Date().toISOString(),
      metadata: { checkout_intent_id: expiredIntent.id },
    });
    const expiredRow = await pool.query(
      "SELECT status FROM bursar.billing_checkout_intents WHERE id = $1",
      [expiredIntent.id],
    );
    expect(expiredRow.rows[0]?.status).toBe("expired");
  });

  it("topup credit grant", async () => {
    const { cm, bm } = await makePgComponents(pool);
    await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_customer_2",
      eventType: "customer.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID2,
      customer: { providerCustomerId: CUSTOMER_ID2 },
    });
    await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_payment_2",
      eventType: "payment.succeeded",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID2,
      customer: { providerCustomerId: CUSTOMER_ID2 },
      payment: {
        providerPaymentId: "py_test456",
        amountMinor: 2000,
        taxMinor: 0,
        currency: "USD",
        refs: { productId: "prod_topup", priceId: PRICE_ID_TOPUP },
        purpose: "credit_topup",
        status: "succeeded",
      },
    });
    const balance = await cm.getBalance(USER_ID2);
    expect(balance.balance.toString()).toBe("2000");
  });

  it("derives topup credits from the settled amount and ignores metadata credit claims", async () => {
    const { cm, bm } = await makePgComponents(pool);
    await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_payment_metadata_credits",
      eventType: "payment.succeeded",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID2,
      metadata: { credits: "999999" },
      payment: {
        providerPaymentId: "py_metadata_credits",
        amountMinor: 2000,
        taxMinor: 0,
        currency: "USD",
        refs: { productId: "prod_topup", priceId: PRICE_ID_TOPUP },
        purpose: "credit_topup",
        status: "succeeded",
      },
    });

    const balance = await cm.getBalance(USER_ID2);
    expect(balance.balance.toString()).toBe("2000");
  });

  it("subscription pause resume", async () => {
    const { cm, bm } = await makePgComponents(pool);
    await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_cus_pause",
      eventType: "customer.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID2,
      customer: { providerCustomerId: CUSTOMER_ID2 },
    });
    await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_sub_pause_1",
      eventType: "subscription.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID2,
      customer: { providerCustomerId: CUSTOMER_ID2 },
      subscription: {
        providerSubscriptionId: SUB_ID2,
        status: "active",
        refs: { productId: PRODUCT_ID, priceId: PRICE_ID },
      },
    });
    expect((await cm.getUserPlan(USER_ID2)).planId).not.toBeNull();

    await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_sub_pause_2",
      eventType: "subscription.paused",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID2,
      customer: { providerCustomerId: CUSTOMER_ID2 },
      subscription: { providerSubscriptionId: SUB_ID2 },
    });
    expect((await cm.getUserPlan(USER_ID2)).planId).toBeNull();

    await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_sub_pause_3",
      eventType: "subscription.resumed",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID2,
      customer: { providerCustomerId: CUSTOMER_ID2 },
      subscription: {
        providerSubscriptionId: SUB_ID2,
        status: "active",
        refs: { productId: PRODUCT_ID, priceId: PRICE_ID },
      },
    });
    expect((await cm.getUserPlan(USER_ID2)).planId).not.toBeNull();
  });

  it("fails a valid event type without a registered handler", async () => {
    const { bm } = await makePgComponents(pool);
    const result = await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_unhandled",
      eventType: "invoice.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      invoice: {
        providerInvoiceId: "in_unhandled",
        status: "draft",
        amountPaidMinor: 0,
        amountDueMinor: 0,
        currency: "USD",
      },
    });
    expect(result.handled).toBe(false);
    expect(result.error).toBe("unhandled_event_type");
  });

  it("duplicate event skips side effects", async () => {
    const { bs, bm } = await makePgComponents(pool);
    await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_cus_dup",
      eventType: "customer.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      customer: { providerCustomerId: "cus_dup_test" },
    });
    expect(await bs.getBillingCustomer(PROVIDER, "cus_dup_test")).toBe(USER_ID);
    await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_cus_dup",
      eventType: "customer.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID2,
      customer: { providerCustomerId: "cus_dup_test" },
    });
    expect(await bs.getBillingCustomer(PROVIDER, "cus_dup_test")).toBe(USER_ID);
  });

  it("provider scoped event id", async () => {
    const { bs } = await makePgComponents(pool);
    expect((await bs.claimBillingEvent("stripe", "evt_prov_scope", "test.event")).status).toBe(
      "claimed",
    );
    expect((await bs.claimBillingEvent("dodo", "evt_prov_scope", "test.event")).status).toBe(
      "claimed",
    );
  });

  // ── Dodo-shaped integration tests (regression: realistic payloads) ────

  const dodoProductId = "prod_dodo_monthly";

  it("dodo: full subscription lifecycle through ingest (regression: no data.id, JS dates)", async () => {
    const { cm, bm, bs } = await makePgComponents(pool);

    // Step 1: customer created — ingested directly like Dodo mapper would
    await bm.ingestBillingEvent({
      provider: "dodo",
      eventId: "dodo:customer.created:cus_dodo_lifecycle",
      eventType: "customer.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID5,
      customer: { providerCustomerId: "cus_dodo_lifecycle" },
    });

    // Step 2: subscription.active → subscription.created via Dodo mapper
    // Payload has no data.id and JS toString dates — realistic Dodo payload
    await mapDodoEvent(
      "subscription.active",
      {
        subscription_id: "sub_dodo_lifecycle",
        status: "active",
        product_id: dodoProductId,
        payment_frequency_interval: "Month",
        payment_frequency_count: 1,
        previous_billing_date: new Date().toString(),
        next_billing_date: new Date(Date.now() + 86400000 * 30).toString(),
      },
      USER_ID5,
      {},
      bm,
    );

    // Verify subscription was created
    const stored = await bs.getBillingSubscription("dodo", "sub_dodo_lifecycle");
    expect(stored).not.toBeNull();
    expect(stored!.status).toBe("active");
    expect(stored!.interval).toBe("month");
    expect(stored!.intervalCount).toBe(1);
    // Dates should be valid ISO 8601 (not raw JS toString)
    expect(stored!.currentPeriodStart).toMatch(/^\d{4}-\d{2}-\d{2}T/);
    expect(stored!.currentPeriodEnd).toMatch(/^\d{4}-\d{2}-\d{2}T/);

    // Verify plan was provisioned
    const plan = await cm.getUserPlan(USER_ID5);
    expect(plan.planId).not.toBeNull();
    expect(plan.planAssignedAt).not.toBeNull();
  });

  it("dodo: subscription.active provisions a plan while the provider is trialing", async () => {
    const { cm, bm, bs } = await makePgComponents(pool);

    await mapDodoEvent(
      "subscription.active",
      {
        subscription_id: "sub_dodo_trialing",
        customer_id: "cus_dodo_trialing",
        status: "trialing",
        product_id: dodoProductId,
        payment_frequency_interval: "Month",
        payment_frequency_count: 1,
        next_billing_date: new Date(Date.now() + 86400000 * 30).toString(),
      },
      USER_ID5,
      {},
      bm,
    );

    const subscription = await bs.getBillingSubscription("dodo", "sub_dodo_trialing");
    expect(subscription?.status).toBe("trialing");
    expect((await cm.getUserPlan(USER_ID5)).planId).not.toBeNull();
  });

  it("dodo: duplicate event ID returns duplicate via event mapper (regression: no rawId collision)", async () => {
    const { bs, bm } = await makePgComponents(pool);

    // First call — should succeed
    await mapDodoEvent(
      "subscription.active",
      {
        subscription_id: "sub_dodo_dup",
        status: "active",
        product_id: dodoProductId,
      },
      USER_ID5,
      {},
      bm,
    );
    expect((await bs.getBillingSubscription("dodo", "sub_dodo_dup"))?.status).toBe("active");

    // Second call with same payload — should be handled as duplicate (not error)
    await mapDodoEvent(
      "subscription.active",
      {
        subscription_id: "sub_dodo_dup",
        status: "active",
        product_id: dodoProductId,
      },
      USER_ID5,
      {},
      bm,
    );

    // Subscription should still be active (not overwritten by duplicate)
    expect((await bs.getBillingSubscription("dodo", "sub_dodo_dup"))?.status).toBe("active");
  });

  it("dodo: multiple subscription events with distinct IDs all succeed (regression: original Bug 1)", async () => {
    const { bs, bm } = await makePgComponents(pool);

    // Pre-create customer + user
    await bm.ingestBillingEvent({
      provider: "dodo",
      eventId: "evt_dodo_multi_cus",
      eventType: "customer.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID5,
      customer: { providerCustomerId: "cus_dodo_multi" },
    });

    // Three different subscription events — each should produce a unique event ID
    await mapDodoEvent(
      "subscription.active",
      {
        subscription_id: "sub_dodo_multi_1",
        status: "active",
        product_id: dodoProductId,
      },
      USER_ID5,
      {},
      bm,
    );
    await mapDodoEvent(
      "subscription.renewed",
      {
        subscription_id: "sub_dodo_multi_1",
        status: "active",
        product_id: dodoProductId,
      },
      USER_ID5,
      {},
      bm,
    );
    await mapDodoEvent(
      "subscription.updated",
      { subscription_id: "sub_dodo_multi_1", status: "active" },
      USER_ID5,
      {},
      bm,
    );

    // All three should have created billing_events entries without collisions
    const sub = await bs.getBillingSubscription("dodo", "sub_dodo_multi_1");
    expect(sub).not.toBeNull();
  });

  it("dodo: dates in JS toString() format stored as valid timestamptz (regression: Bug 2)", async () => {
    const { bs, bm } = await makePgComponents(pool);

    await bm.ingestBillingEvent({
      provider: "dodo",
      eventId: "evt_dodo_date_cus",
      eventType: "customer.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID5,
      customer: { providerCustomerId: "cus_dodo_date" },
    });

    // Use JS toString() dates — the exact format Dodo sends
    const jsDate = new Date().toString();
    const jsDateFuture = new Date(Date.now() + 86400000 * 30).toString();

    await mapDodoEvent(
      "subscription.active",
      {
        subscription_id: "sub_dodo_date",
        status: "active",
        product_id: dodoProductId,
        previous_billing_date: jsDate,
        next_billing_date: jsDateFuture,
      },
      USER_ID5,
      {},
      bm,
    );

    const sub = await bs.getBillingSubscription("dodo", "sub_dodo_date");
    expect(sub).not.toBeNull();
    // Must be valid ISO 8601, not the raw JS toString format
    expect(sub!.currentPeriodStart).toMatch(/^\d{4}-\d{2}-\d{2}T/);
    expect(sub!.currentPeriodEnd).toMatch(/^\d{4}-\d{2}-\d{2}T/);
    // Verify stored timestamps are parseable by Postgres (would error if not)
    expect(() => new Date(sub!.currentPeriodStart!).toISOString()).not.toThrow();
    expect(() => new Date(sub!.currentPeriodEnd!).toISOString()).not.toThrow();
  });

  it("sync offers adds new", async () => {
    const { bs, cm } = await makePgComponents(pool);
    await cm.publishAndActivateCatalog({
      ...PRICING_DICT,
      commerce: {
        ...PRICING_DICT.commerce,
        offers: {
          ...PRICING_DICT.commerce.offers,
          new_offer: {
            type: "subscription",
            display_name: "New Offer",
            price: { amount_minor: 1000, currency: "USD" },
            plan: "free",
            billing_interval: { unit: "month", count: 1 },
            providers: {
              stripe: {
                type: "stripe_price",
                price_id: "price_new_offer",
              },
            },
          },
        },
      },
    });
    const newOffer = await bs.resolveBillingOffer("stripe", null, "price_new_offer");
    expect(newOffer).not.toBeNull();
    expect(newOffer!.offerKey).toBe("new_offer");
  });

  it("cycle grant credits granted", async () => {
    const { cm, bm } = await makePgComponents(pool);
    await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_cus_cg1",
      eventType: "customer.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID3,
      customer: { providerCustomerId: CUSTOMER_ID2 },
    });
    await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_sub_cg1",
      eventType: "subscription.renewed",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID3,
      customer: { providerCustomerId: CUSTOMER_ID2 },
      subscription: {
        providerSubscriptionId: "sub_cg_test",
        status: "active",
        periodStart: "2025-06-01T00:00:00Z",
        periodEnd: "2025-07-01T00:00:00Z",
        refs: {
          productId: "prod_cycle_grant",
          priceId: "price_cycle_grant_5000",
        },
        interval: "month",
        intervalCount: 1,
      },
    });
    const balance = await cm.getBalance(USER_ID3);
    expect(balance.balance.toString()).toBe("5000");
  });

  it("refund clawback deducts credits", async () => {
    const { cm, bm } = await makePgComponents(pool);
    const uid = "00000000-0000-0000-0000-000000000005";
    const paymentId = "py_refund_clawback";
    await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_cus_refund",
      eventType: "customer.created",
      occurredAt: new Date().toISOString(),
      accountId: uid,
      customer: { providerCustomerId: "cus_refund_test" },
    });
    await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_pay_refund",
      eventType: "payment.succeeded",
      occurredAt: new Date().toISOString(),
      accountId: uid,
      customer: { providerCustomerId: "cus_refund_test" },
      payment: {
        providerPaymentId: paymentId,
        amountMinor: 2000,
        taxMinor: 0,
        currency: "USD",
        refs: { productId: "prod_topup", priceId: PRICE_ID_TOPUP },
        purpose: "credit_topup",
        status: "succeeded",
      },
    });
    const balanceAfterGrant = await cm.getBalance(uid);
    expect(balanceAfterGrant.balance.toString()).toBe("2000");

    const result = await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_refund_1",
      eventType: "refund.created",
      occurredAt: new Date().toISOString(),
      accountId: uid,
      customer: { providerCustomerId: "cus_refund_test" },
      refund: {
        providerRefundId: "refund_1",
        providerPaymentId: paymentId,
        amountMinor: 2000,
        currency: "USD",
        status: "succeeded",
      },
      metadata: { provider_case: "full_clawback" },
    });
    expect(result.handled).toBe(true);
    const balanceAfterRefund = await cm.getBalance(uid);
    expect(balanceAfterRefund.balance.toString()).toBe("0");
    const refundAudit = await pool.query(
      `SELECT metadata
       FROM bursar.billing_refunds
       WHERE provider = $1 AND provider_refund_id = $2`,
      [PROVIDER, "refund_1"],
    );
    expect(refundAudit.rows[0]?.metadata).toEqual({
      provider_case: "full_clawback",
    });
  });

  it("partial refund rounds the credit clawback to six decimal places", async () => {
    const { cm, bm } = await makePgComponents(pool);
    const preciseConfig = structuredClone(PRICING_DICT);
    preciseConfig.commerce!.offers.standard_topup.credits_per_unit = "1234.567891";
    await cm.publishAndActivateCatalog(preciseConfig);

    const uid = USER_ID5;
    const paymentId = "py_refund_partial_precision";
    await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_pay_refund_partial_precision",
      eventType: "payment.succeeded",
      occurredAt: new Date().toISOString(),
      accountId: uid,
      payment: {
        providerPaymentId: paymentId,
        amountMinor: 1000,
        taxMinor: 0,
        currency: "USD",
        refs: { productId: "prod_topup", priceId: PRICE_ID_TOPUP },
        purpose: "credit_topup",
        status: "succeeded",
      },
    });
    expect((await cm.getBalance(uid)).balance.toString()).toBe("1234.567891");

    const result = await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_refund_partial_precision",
      eventType: "refund.created",
      occurredAt: new Date().toISOString(),
      accountId: uid,
      refund: {
        providerRefundId: "refund_partial_precision",
        providerPaymentId: paymentId,
        amountMinor: 1,
        currency: "USD",
        status: "succeeded",
      },
    });
    expect(result.handled).toBe(true);
    expect((await cm.getBalance(uid)).balance.toString()).toBe("1233.333323");

    const allocation = await pool.query(
      `SELECT allocation.credit_amount, COUNT(ledger.id)::int AS ledger_count
       FROM bursar.billing_refund_grants AS allocation
       JOIN bursar.billing_refunds AS refund ON refund.id = allocation.refund_id
       JOIN bursar.credit_ledger_entries AS ledger ON ledger.id = allocation.ledger_entry_id
       WHERE refund.provider = $1 AND refund.provider_refund_id = $2
       GROUP BY allocation.credit_amount`,
      [PROVIDER, "refund_partial_precision"],
    );
    expect(allocation.rows).toEqual([{ credit_amount: "1.234568", ledger_count: 1 }]);
  });

  it("duplicate refund identity with a new event id replays one clawback", async () => {
    const { cm, bm } = await makePgComponents(pool);
    const uid = USER_ID4;
    const paymentId = "py_refund_duplicate_identity";
    await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_pay_refund_duplicate_identity",
      eventType: "payment.succeeded",
      occurredAt: new Date().toISOString(),
      accountId: uid,
      payment: {
        providerPaymentId: paymentId,
        amountMinor: 2000,
        taxMinor: 0,
        currency: "USD",
        refs: { productId: "prod_topup", priceId: PRICE_ID_TOPUP },
        purpose: "credit_topup",
        status: "succeeded",
      },
    });
    const refund = {
      providerRefundId: "refund_duplicate_identity",
      providerPaymentId: paymentId,
      amountMinor: 2000,
      currency: "USD",
      status: "succeeded" as const,
    };
    const first = await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_refund_duplicate_identity_1",
      eventType: "refund.created",
      occurredAt: new Date().toISOString(),
      accountId: uid,
      refund,
    });
    const second = await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_refund_duplicate_identity_2",
      eventType: "refund.created",
      occurredAt: new Date().toISOString(),
      accountId: uid,
      refund,
    });
    expect(first.handled).toBe(true);
    expect(second).toMatchObject({ handled: true, action: "refund_clawback" });
    expect((await cm.getBalance(uid)).balance.toString()).toBe("0");

    const counts = await pool.query(
      `SELECT
         (SELECT COUNT(*)::int FROM bursar.billing_refunds
          WHERE provider = $1 AND provider_refund_id = $2) AS refund_count,
         (SELECT COUNT(*)::int FROM bursar.billing_refund_grants AS allocation
          JOIN bursar.billing_refunds AS refund ON refund.id = allocation.refund_id
          WHERE refund.provider = $1 AND refund.provider_refund_id = $2) AS allocation_count,
         (SELECT COUNT(*)::int FROM bursar.credit_ledger_entries AS ledger
          JOIN bursar.credit_accounts AS account ON account.id = ledger.account_id
          WHERE account.subject_id = $3 AND ledger.kind = 'refund_clawback') AS ledger_count`,
      [PROVIDER, "refund_duplicate_identity", uid],
    );
    expect(counts.rows[0]).toEqual({
      refund_count: 1,
      allocation_count: 1,
      ledger_count: 1,
    });
  });

  it("concurrent distinct refund events with one provider identity claw back once", async () => {
    const { cm, bm } = await makePgComponents(pool);
    const uid = USER_ID;
    const paymentId = "py_refund_concurrent_identity";
    await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_pay_refund_concurrent_identity",
      eventType: "payment.succeeded",
      occurredAt: new Date().toISOString(),
      accountId: uid,
      payment: {
        providerPaymentId: paymentId,
        amountMinor: 2000,
        taxMinor: 0,
        currency: "USD",
        refs: { productId: "prod_topup", priceId: PRICE_ID_TOPUP },
        purpose: "credit_topup",
        status: "succeeded",
      },
    });

    const workers = Array.from(
      { length: 12 },
      () => new pg.Pool({ connectionString: DATABASE_URL!, max: 1 }),
    );
    const refund = {
      providerRefundId: "refund_concurrent_identity",
      providerPaymentId: paymentId,
      amountMinor: 2000,
      currency: "USD",
      status: "succeeded" as const,
    };
    try {
      const results = await Promise.all(
        workers.map((worker, index) => {
          const localStore = new PostgresBillingStore({
            postgres: worker,
            tenantId: TEST_TENANT_ID,
            providerEnvironment: "test",
          });
          const localBilling = new BillingService(localStore);
          return localBilling.ingestBillingEvent({
            provider: PROVIDER,
            eventId: `evt_refund_concurrent_identity_${index}`,
            eventType: "refund.created",
            occurredAt: new Date(Date.now() + index).toISOString(),
            accountId: uid,
            refund,
          });
        }),
      );
      expect(results).toHaveLength(workers.length);
      expect(results.every((result) => result.handled && result.action === "refund_clawback")).toBe(
        true,
      );
      expect((await cm.getBalance(uid)).balance.toString()).toBe("0");

      const counts = await pool.query(
        `SELECT
           (SELECT COUNT(*)::int FROM bursar.billing_refunds
            WHERE provider = $1 AND provider_refund_id = $2) AS refund_count,
           (SELECT COUNT(*)::int FROM bursar.billing_refund_grants AS allocation
            JOIN bursar.billing_refunds AS refund ON refund.id = allocation.refund_id
            WHERE refund.provider = $1 AND refund.provider_refund_id = $2) AS allocation_count,
           (SELECT COUNT(*)::int FROM bursar.credit_ledger_entries AS ledger
            JOIN bursar.credit_accounts AS account ON account.id = ledger.account_id
            WHERE account.subject_id = $3 AND ledger.kind = 'refund_clawback') AS ledger_count`,
        [PROVIDER, refund.providerRefundId, uid],
      );
      expect(counts.rows[0]).toEqual({
        refund_count: 1,
        allocation_count: 1,
        ledger_count: 1,
      });
    } finally {
      await Promise.all(workers.map((worker) => worker.end()));
    }
  }, 30000);

  it("cumulative partial refunds reach an exact six-decimal full clawback", async () => {
    const { cm, bm } = await makePgComponents(pool);
    const preciseConfig = structuredClone(PRICING_DICT);
    preciseConfig.commerce!.offers.standard_topup.credits_per_unit = "1234.567892";
    await cm.publishAndActivateCatalog(preciseConfig);

    const uid = USER_ID2;
    const paymentId = "py_refund_cumulative_precision";
    await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_pay_refund_cumulative_precision",
      eventType: "payment.succeeded",
      occurredAt: new Date().toISOString(),
      accountId: uid,
      payment: {
        providerPaymentId: paymentId,
        amountMinor: 1000,
        taxMinor: 0,
        currency: "USD",
        refs: { productId: "prod_topup", priceId: PRICE_ID_TOPUP },
        purpose: "credit_topup",
        status: "succeeded",
      },
    });
    expect((await cm.getBalance(uid)).balance.toString()).toBe("1234.567892");

    const refunds = [
      { amountMinor: 333, creditAmount: "411.111108" },
      { amountMinor: 333, creditAmount: "411.111108" },
      { amountMinor: 334, creditAmount: "412.345676" },
    ];
    for (const [index, expected] of refunds.entries()) {
      const result = await bm.ingestBillingEvent({
        provider: PROVIDER,
        eventId: `evt_refund_cumulative_precision_${index}`,
        eventType: "refund.created",
        occurredAt: new Date(Date.now() + index).toISOString(),
        accountId: uid,
        refund: {
          providerRefundId: `refund_cumulative_precision_${index}`,
          providerPaymentId: paymentId,
          amountMinor: expected.amountMinor,
          currency: "USD",
          status: "succeeded",
        },
      });
      expect(result).toEqual({ handled: true, action: "refund_clawback" });
    }

    expect((await cm.getBalance(uid)).balance.toString()).toBe("0");
    const allocations = await pool.query(
      `SELECT refund.amount_minor, allocation.credit_amount
       FROM bursar.billing_refund_grants AS allocation
       JOIN bursar.billing_refunds AS refund ON refund.id = allocation.refund_id
       WHERE refund.provider = $1 AND refund.payment_id = (
         SELECT id FROM bursar.billing_payments
         WHERE provider = $1 AND provider_payment_id = $2
       )
       ORDER BY refund.amount_minor, refund.provider_refund_id`,
      [PROVIDER, paymentId],
    );
    expect(allocations.rows).toEqual([
      { amount_minor: "333", credit_amount: "411.111108" },
      { amount_minor: "333", credit_amount: "411.111108" },
      { amount_minor: "334", credit_amount: "412.345676" },
    ]);
    const ledger = await pool.query(
      `SELECT COUNT(*)::int AS ledger_count, COALESCE(SUM(amount), 0)::text AS clawback_total
       FROM bursar.credit_ledger_entries AS entry
       JOIN bursar.credit_accounts AS account ON account.id = entry.account_id
       WHERE account.subject_id = $1 AND entry.kind = 'refund_clawback'`,
      [uid],
    );
    expect(ledger.rows[0]).toEqual({
      ledger_count: 3,
      clawback_total: "-1234.567892",
    });
  });

  it("pending refund records no clawback until succeeded, then replay stays idempotent", async () => {
    const { cm, bm } = await makePgComponents(pool);
    const uid = USER_ID3;
    const paymentId = "py_refund_pending_lifecycle";
    const refundId = "refund_pending_lifecycle";
    await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_pay_refund_pending_lifecycle",
      eventType: "payment.succeeded",
      occurredAt: new Date().toISOString(),
      accountId: uid,
      payment: {
        providerPaymentId: paymentId,
        amountMinor: 1000,
        taxMinor: 0,
        currency: "USD",
        refs: { productId: "prod_topup", priceId: PRICE_ID_TOPUP },
        purpose: "credit_topup",
        status: "succeeded",
      },
    });
    const pending = await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_refund_pending_lifecycle_created",
      eventType: "refund.created",
      occurredAt: "2025-01-01T00:00:00.000Z",
      accountId: uid,
      refund: {
        providerRefundId: refundId,
        providerPaymentId: paymentId,
        amountMinor: 1000,
        currency: "USD",
        status: "pending",
      },
    });
    expect(pending).toEqual({ handled: true, action: "refund_recorded" });
    expect((await cm.getBalance(uid)).balance.toString()).toBe("1000");
    const pendingCounts = await pool.query(
      `SELECT
         (SELECT status FROM bursar.billing_refunds
          WHERE provider = $1 AND provider_refund_id = $2) AS status,
         (SELECT COUNT(*)::int FROM bursar.billing_refund_grants AS allocation
          JOIN bursar.billing_refunds AS refund ON refund.id = allocation.refund_id
          WHERE refund.provider = $1 AND refund.provider_refund_id = $2) AS allocation_count`,
      [PROVIDER, refundId],
    );
    expect(pendingCounts.rows[0]).toEqual({
      status: "pending",
      allocation_count: 0,
    });

    const succeeded = await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_refund_pending_lifecycle_succeeded",
      eventType: "refund.updated",
      occurredAt: "2025-01-02T00:00:00.000Z",
      accountId: uid,
      refund: {
        providerRefundId: refundId,
        providerPaymentId: paymentId,
        amountMinor: 1000,
        currency: "USD",
        status: "succeeded",
      },
    });
    expect(succeeded).toEqual({ handled: true, action: "refund_clawback" });
    expect((await cm.getBalance(uid)).balance.toString()).toBe("0");

    const replay = await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_refund_pending_lifecycle_replay",
      eventType: "refund.updated",
      occurredAt: "2025-01-03T00:00:00.000Z",
      accountId: uid,
      refund: {
        providerRefundId: refundId,
        providerPaymentId: paymentId,
        amountMinor: 1000,
        currency: "USD",
        status: "succeeded",
      },
    });
    expect(replay).toEqual({ handled: true, action: "refund_clawback" });
    const finalCounts = await pool.query(
      `SELECT
         (SELECT COUNT(*)::int FROM bursar.billing_refunds
          WHERE provider = $1 AND provider_refund_id = $2) AS refund_count,
         (SELECT COUNT(*)::int FROM bursar.billing_refund_grants AS allocation
          JOIN bursar.billing_refunds AS refund ON refund.id = allocation.refund_id
          WHERE refund.provider = $1 AND refund.provider_refund_id = $2) AS allocation_count,
         (SELECT COUNT(*)::int FROM bursar.credit_ledger_entries AS entry
          JOIN bursar.credit_accounts AS account ON account.id = entry.account_id
          WHERE account.subject_id = $3 AND entry.kind = 'refund_clawback') AS ledger_count`,
      [PROVIDER, refundId, uid],
    );
    expect(finalCounts.rows[0]).toEqual({
      refund_count: 1,
      allocation_count: 1,
      ledger_count: 1,
    });
  });

  it("over-refund delivery is rejected without a second credit mutation", async () => {
    const { cm, bm } = await makePgComponents(pool);
    const uid = USER_ID3;
    const paymentId = "py_refund_overallocation";
    await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_pay_refund_overallocation",
      eventType: "payment.succeeded",
      occurredAt: new Date().toISOString(),
      accountId: uid,
      payment: {
        providerPaymentId: paymentId,
        amountMinor: 2000,
        taxMinor: 0,
        currency: "USD",
        refs: { productId: "prod_topup", priceId: PRICE_ID_TOPUP },
        purpose: "credit_topup",
        status: "succeeded",
      },
    });
    const first = await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_refund_overallocation_1",
      eventType: "refund.created",
      occurredAt: new Date().toISOString(),
      accountId: uid,
      refund: {
        providerRefundId: "refund_overallocation_1",
        providerPaymentId: paymentId,
        amountMinor: 1500,
        currency: "USD",
        status: "succeeded",
      },
    });
    const second = await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_refund_overallocation_2",
      eventType: "refund.created",
      occurredAt: new Date().toISOString(),
      accountId: uid,
      refund: {
        providerRefundId: "refund_overallocation_2",
        providerPaymentId: paymentId,
        amountMinor: 600,
        currency: "USD",
        status: "succeeded",
      },
    });
    expect(first.handled).toBe(true);
    expect(second.handled).toBe(false);
    expect(second.error).toBeTruthy();
    expect((await cm.getBalance(uid)).balance.toString()).toBe("500");

    const counts = await pool.query(
      `SELECT COUNT(*)::int AS refund_count
       FROM bursar.billing_refunds
       WHERE provider = $1 AND payment_id = (
         SELECT id FROM bursar.billing_payments
         WHERE provider = $1 AND provider_payment_id = $2
       ) AND status = 'succeeded'`,
      [PROVIDER, paymentId],
    );
    expect(counts.rows[0]?.refund_count).toBe(1);
    const ledger = await pool.query(
      `SELECT COUNT(*)::int AS ledger_count
       FROM bursar.credit_ledger_entries AS ledger
       JOIN bursar.credit_accounts AS account ON account.id = ledger.account_id
       WHERE account.subject_id = $1 AND ledger.kind = 'refund_clawback'`,
      [uid],
    );
    expect(ledger.rows[0]?.ledger_count).toBe(1);
  });

  it("cycle grant replace prior", async () => {
    const { cm, bm } = await makePgComponents(pool);
    await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_cus_cg2",
      eventType: "customer.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID4,
      customer: { providerCustomerId: "cus_cg_replace" },
    });
    await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_sub_cg2a",
      eventType: "subscription.renewed",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID4,
      customer: { providerCustomerId: "cus_cg_replace" },
      subscription: {
        providerSubscriptionId: "sub_cg_replace",
        status: "active",
        periodStart: "2025-06-01T00:00:00Z",
        periodEnd: "2025-07-01T00:00:00Z",
        refs: {
          productId: "prod_cycle_grant",
          priceId: "price_cycle_grant_5000",
        },
        interval: "month",
        intervalCount: 1,
      },
    });
    const balance1 = await cm.getBalance(USER_ID4);
    expect(balance1.balance.toString()).toBe("5000");

    // Renew — should revoke prior cycle_grant and grant new 5000
    await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_sub_cg2b",
      eventType: "subscription.renewed",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID4,
      customer: { providerCustomerId: "cus_cg_replace" },
      subscription: {
        providerSubscriptionId: "sub_cg_replace",
        status: "active",
        periodStart: "2025-07-01T00:00:00Z",
        periodEnd: "2025-08-01T00:00:00Z",
        refs: {
          productId: "prod_cycle_grant",
          priceId: "price_cycle_grant_5000",
        },
        interval: "month",
        intervalCount: 1,
      },
    });
    const balance2 = await cm.getBalance(USER_ID4);
    expect(balance2.balance.toString()).toBe("5000");
  });

  // ── Invoice + Dispute ───────────────────────────────────────────────────

  it("invoice upsert and dispute upsert", async () => {
    const { bs } = await makePgComponents(pool);
    await bs.upsertBillingSubscription({
      userId: USER_ID,
      provider: PROVIDER,
      providerSubscriptionId: SUB_ID,
      offerKey: "pro_monthly",
      status: "active",
      providerUpdatedAt: "2025-01-01T00:00:00Z",
      cancelAtPeriodEnd: false,
    });
    await bs.upsertBillingPayment({
      provider: PROVIDER,
      providerPaymentId: "py_001",
      providerInvoiceId: "in_001",
      userId: USER_ID,
      amountMinor: 1000,
      taxMinor: 0,
      currency: "USD",
      purpose: "subscription",
      status: "succeeded",
      providerUpdatedAt: TEST_INSTANT,
      metadata: { reconciliation_source: "invoice_test" },
    });
    await bs.upsertBillingInvoice({
      provider: PROVIDER,
      providerInvoiceId: "in_001",
      providerSubscriptionId: SUB_ID,
      userId: USER_ID,
      status: "paid",
      amountPaidMinor: 1000,
      amountDueMinor: 1000,
      currency: "USD",
      periodStart: "2025-06-01T00:00:00Z",
      periodEnd: "2025-07-01T00:00:00Z",
      providerUpdatedAt: TEST_INSTANT,
    });
    await bs.upsertBillingDispute({
      provider: PROVIDER,
      providerDisputeId: "dp_001",
      providerPaymentId: "py_001",
      status: "needs_response",
      reason: "fraudulent",
      providerUpdatedAt: TEST_INSTANT,
    });
    const payResult = await bs.getBillingPayment(PROVIDER, "py_001");
    expect(payResult).not.toBeNull();
    const paymentAudit = await pool.query(
      `SELECT provider_invoice_id, metadata
       FROM bursar.billing_payments
       WHERE provider = $1 AND provider_payment_id = $2`,
      [PROVIDER, "py_001"],
    );
    expect(paymentAudit.rows[0]).toMatchObject({
      provider_invoice_id: "in_001",
      metadata: { reconciliation_source: "invoice_test" },
    });
  });

  // ── Billing Preferences ─────────────────────────────────────────────────

  it("billing preferences crud", async () => {
    const { bs, bm } = await makePgComponents(pool);
    const prefs: BillingPreferences = {
      userId: USER_ID,
      autoRecharge: true,
      overageProtection: true,
      emailNotifications: false,
      usageAlerts: true,
      invoiceReminders: false,
    };
    await bs.upsertBillingPreferences(prefs);
    const got = await bs.getBillingPreferences(USER_ID);
    expect(got).not.toBeNull();
    expect(got!.autoRecharge).toBe(true);
    expect(got!.emailNotifications).toBe(false);

    const viaManager = await bm.getUserPreferences(USER_ID);
    expect(viaManager).not.toBeNull();
    expect(viaManager!.autoRecharge).toBe(true);

    await bm.updateUserPreferences({ ...prefs, autoRecharge: false });
    const updated = await bs.getBillingPreferences(USER_ID);
    expect(updated!.autoRecharge).toBe(false);
  });

  it("billing preferences not found", async () => {
    const { bs } = await makePgComponents(pool);
    expect(await bs.getBillingPreferences("00000000-0000-0000-0000-000000000099")).toBeNull();
  });

  // ── Customer reverse lookup ─────────────────────────────────────────────

  it("customer reverse lookup by user id", async () => {
    const { bs, bm } = await makePgComponents(pool);
    await bs.upsertBillingCustomer(PROVIDER, CUSTOMER_ID, USER_ID, "test@example.com");

    const found = await bs.getBillingCustomerByUserId(USER_ID);
    expect(found).not.toBeNull();
    expect(found!.provider).toBe(PROVIDER);
    expect(found!.providerCustomerId).toBe(CUSTOMER_ID);

    const foundScoped = await bs.getBillingCustomerByUserId(USER_ID, PROVIDER);
    expect(foundScoped).not.toBeNull();
    expect(foundScoped!.providerCustomerId).toBe(CUSTOMER_ID);

    const viaManager = await bm.getCustomerByUserId(USER_ID);
    expect(viaManager).not.toBeNull();
    expect(viaManager!.provider).toBe(PROVIDER);
  });

  it("customer reverse lookup not found", async () => {
    const { bs } = await makePgComponents(pool);
    expect(await bs.getBillingCustomerByUserId("00000000-0000-0000-0000-000000000099")).toBeNull();
  });

  // ── Active pricing config ───────────────────────────────────────────────

  it("get active pricing config", async () => {
    const { bs } = await makePgComponents(pool);
    const config = await bs.getActiveCatalogDocument();
    expect(config).toMatchObject({ version: 1 });
  });

  // ── Manager public API ──────────────────────────────────────────────────

  it("manager resolve offer and topup", async () => {
    const { bm } = await makePgComponents(pool);
    const offer = await bm.resolveOffer(PROVIDER, null, PRICE_ID);
    expect(offer).not.toBeNull();
    expect(offer!.offerKey).toBe("pro_monthly");

    const topup = await bm.resolveTopup(PROVIDER, null, PRICE_ID_TOPUP);
    expect(topup).not.toBeNull();
    expect(topup!.topupKey).toBe("standard_topup");
  });

  it("manager upsert customer and invalidate cache", async () => {
    const { bm } = await makePgComponents(pool);
    await bm.upsertCustomer(PROVIDER, "cus_manager", USER_ID, "test@example.com");
    const uid = await bm["store"].getBillingCustomer(PROVIDER, "cus_manager");
    expect(uid).toBe(USER_ID);
    bm.invalidateOfferCache();
  });

  // ── Customer deleted ────────────────────────────────────────────────────

  it("customer deleted revokes plan", async () => {
    const { cm, bm, bs } = await makePgComponents(pool);
    await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_cus_del_1",
      eventType: "customer.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      customer: { providerCustomerId: "cus_del_test" },
    });
    await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_sub_del_1",
      eventType: "subscription.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      customer: { providerCustomerId: "cus_del_test" },
      subscription: {
        providerSubscriptionId: "sub_del_test",
        status: "active",
        refs: { productId: PRODUCT_ID, priceId: PRICE_ID },
      },
    });
    expect((await cm.getUserPlan(USER_ID)).planId).not.toBeNull();
    expect(await bm.listCancellableProviderSubscriptionIds(USER_ID)).toEqual(["sub_del_test"]);
    expect(await bm.listCancellableSubscriptions(USER_ID)).toHaveLength(1);
    await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_cus_del_2",
      eventType: "customer.deleted",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      customer: { providerCustomerId: "cus_del_test" },
    });
    expect((await cm.getUserPlan(USER_ID)).planId).toBeNull();

    await cm.setUserPlan(USER_ID, "pro");
    const terminalPlanBilling = new BillingService(bs, {
      provisioning: cm,
      terminalPlanKey: "free",
    });
    await terminalPlanBilling.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_cus_del_terminal",
      eventType: "customer.deleted",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      customer: { providerCustomerId: "cus_del_test" },
    });
    expect((await cm.getUserPlan(USER_ID)).planKey).toBe("free");
  });

  // ── Checkout completed ──────────────────────────────────────────────────

  it("checkout completed creates subscription", async () => {
    const { cm, bm } = await makePgComponents(pool);
    const result = await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_chk_1",
      eventType: "checkout.completed",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      customer: { providerCustomerId: "cus_chk_test" },
      subscription: {
        providerSubscriptionId: "sub_chk_test",
        status: "active",
        refs: { productId: PRODUCT_ID, priceId: PRICE_ID },
      },
    });
    expect(result.handled).toBe(true);
    const plan = await cm.getUserPlan(USER_ID);
    expect(plan.planId).not.toBeNull();
  });

  it("reconciles a subscription payment, cycle grant, and invoice together", async () => {
    const { cm, bm } = await makePgComponents(pool);
    const subscriptionId = "sub_payment_invoice_history";
    await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_payment_invoice_subscription",
      eventType: "subscription.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      subscription: {
        providerSubscriptionId: subscriptionId,
        status: "active",
        refs: { priceId: "price_cycle_grant_5000" },
      },
    });

    const result = await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_payment_invoice_succeeded",
      eventType: "payment.succeeded",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      payment: {
        providerPaymentId: "py_payment_invoice_history",
        amountMinor: 5000,
        taxMinor: 0,
        currency: "USD",
        purpose: "subscription",
        status: "succeeded",
      },
      subscription: {
        providerSubscriptionId: subscriptionId,
        status: "active",
        refs: { priceId: "price_cycle_grant_5000" },
      },
    });

    expect(result).toMatchObject({ handled: true, action: "payment_succeeded" });
    expect(await bm.listBillingInvoices(USER_ID)).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          providerInvoiceId: "py_payment_invoice_history",
          status: "paid",
          amountPaidMinor: 5000,
        }),
      ]),
    );
    expect((await cm.getUserPlan(USER_ID)).planKey).toBe("pro");
    expect((await cm.getBalance(USER_ID)).balance.toString()).toBe("5000");
  });

  it("checkout completed without subscription", async () => {
    const { bm } = await makePgComponents(pool);
    const result = await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_chk_2",
      eventType: "checkout.completed",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      customer: { providerCustomerId: "cus_chk2" },
    });
    expect(result.action).toBe("checkout_completed");
  });

  // ── Subscription activated ──────────────────────────────────────────────

  it("subscription activated provisions plan", async () => {
    const { cm, bm } = await makePgComponents(pool);
    await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_act_1",
      eventType: "subscription.activated",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      customer: { providerCustomerId: "cus_act" },
      subscription: {
        providerSubscriptionId: "sub_act",
        status: "active",
        refs: { productId: PRODUCT_ID, priceId: PRICE_ID },
      },
    });
    expect((await cm.getUserPlan(USER_ID)).planId).not.toBeNull();
  });

  // ── Cancellation schedule + unschedule ──────────────────────────────────

  it("persists cancellation tombstones for previously unseen subscriptions", async () => {
    const { bm, bs } = await makePgComponents(pool);
    const result = await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_cancel_unknown_with_refs",
      eventType: "subscription.canceled",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      subscription: {
        providerSubscriptionId: "sub_cancel_unknown_with_refs",
        status: "canceled",
        refs: { productId: PRODUCT_ID, priceId: PRICE_ID },
      },
    });

    expect(result).toEqual({ handled: true, action: "subscription_canceled" });
    await expect(
      bs.getBillingSubscription(PROVIDER, "sub_cancel_unknown_with_refs"),
    ).resolves.toMatchObject({
      status: "canceled",
      offerKey: "pro_monthly",
    });
  });

  it("fails an unknown cancellation that cannot be persisted safely", async () => {
    const { bm, bs } = await makePgComponents(pool);
    const result = await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_cancel_unknown_without_refs",
      eventType: "subscription.canceled",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      subscription: {
        providerSubscriptionId: "sub_cancel_unknown_without_refs",
        status: "canceled",
      },
    });

    expect(result.handled).toBe(false);
    expect(result.error).toBe("billing_event_processing_failed:STORE_ERROR");
    await expect(
      bs.getBillingSubscription(PROVIDER, "sub_cancel_unknown_without_refs"),
    ).resolves.toBeNull();
  });

  it("subscription cancellation scheduled and unscheduled", async () => {
    const { bm } = await makePgComponents(pool);
    await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_cs_1",
      eventType: "subscription.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      subscription: {
        providerSubscriptionId: "sub_cs_test",
        status: "active",
        refs: { productId: PRODUCT_ID, priceId: PRICE_ID },
      },
    });
    const schedResult = await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_cs_2",
      eventType: "subscription.cancellation_scheduled",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      subscription: { providerSubscriptionId: "sub_cs_test" },
    });
    expect(schedResult.action).toBe("cancellation_scheduled");

    const unschedResult = await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_cs_3",
      eventType: "subscription.cancellation_unscheduled",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      subscription: { providerSubscriptionId: "sub_cs_test" },
    });
    expect(unschedResult.action).toBe("cancellation_unscheduled");
  });

  // ── Subscription expired ────────────────────────────────────────────────

  it("subscription expired revokes plan", async () => {
    const { cm, bm } = await makePgComponents(pool);
    await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_exp_1",
      eventType: "subscription.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID5,
      subscription: {
        providerSubscriptionId: "sub_exp_test",
        status: "active",
        refs: { productId: PRODUCT_ID, priceId: PRICE_ID },
      },
    });
    await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_exp_2",
      eventType: "subscription.expired",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID5,
      subscription: { providerSubscriptionId: "sub_exp_test" },
    });
    expect((await cm.getUserPlan(USER_ID5)).planId).toBeNull();
  });

  // ── Payment failed ──────────────────────────────────────────────────────

  it("payment failed records and preserves grace-period access", async () => {
    const { cm, bs } = await makePgComponents(pool);
    const bm = new BillingService(bs, {
      provisioning: cm,
      pastDueGracePeriodMs: 1_000,
    });
    const now = Date.now();
    await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_pf_1",
      eventType: "subscription.created",
      occurredAt: new Date(now - 5_000).toISOString(),
      accountId: USER_ID5,
      subscription: {
        providerSubscriptionId: "sub_pf_test",
        status: "active",
        refs: { productId: PRODUCT_ID, priceId: PRICE_ID },
      },
    });
    const result = await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_pf_2",
      eventType: "payment.failed",
      occurredAt: new Date(now - 2_000).toISOString(),
      accountId: USER_ID5,
      customer: { providerCustomerId: "cus_pf" },
      payment: {
        providerPaymentId: "py_pf",
        amountMinor: 1000,
        taxMinor: 0,
        currency: "USD",
        purpose: "subscription",
        status: "failed",
      },
      subscription: { providerSubscriptionId: "sub_pf_test" },
    });
    expect(result.handled).toBe(true);
    expect((await cm.getUserPlan(USER_ID5)).planId).not.toBeNull();
    const pastDue = await bs.getBillingSubscription(PROVIDER, "sub_pf_test");
    expect(pastDue?.status).toBe("past_due");
    expect(pastDue?.graceEndsAt).not.toBeNull();

    expect(await bm.expirePastDueGracePeriods(new Date(now))).toBe(1);
    expect((await cm.getUserPlan(USER_ID5)).planId).toBeNull();
    const expiredGrace = await bs.getBillingSubscription(PROVIDER, "sub_pf_test");
    expect(expiredGrace?.status).toBe("past_due");
    expect(expiredGrace?.graceExpiredAt).not.toBeNull();
    expect(await bm.expirePastDueGracePeriods(new Date(now))).toBe(0);
  });

  it("quotes and retries durable auto-recharge attempts across provider outcomes", async () => {
    const { cm, bm, bs } = await makePgComponents(pool);
    const config: BursarConfigData = structuredClone(PRICING_DICT);
    const commerce = config.commerce;
    if (!commerce) throw new Error("expected commerce configuration");
    commerce.auto_recharge = {
      eligible_topups: ["standard_topup"],
      balance_below: { minimum: "100", maximum: "5000", default: "1000" },
      rearm_above: "6000",
      quantity: { minimum: 1, maximum: 3, default: 1 },
      limits: {
        max_purchases: 3,
        window: { type: "rolling", duration: { unit: "day", count: 30 } },
        max_charge_minor: 1000,
        cooldown: { unit: "hour", count: 1 },
      },
    };
    await cm.publishAndActivateCatalog(config);

    const outcomes = [
      {
        providerPaymentId: "auto_processing_initial",
        status: "processing" as const,
        amountMinor: 1000,
        currency: "USD",
      },
    ];
    const provider = {
      provider: PROVIDER,
      createCheckoutSession: vi.fn(),
      handleWebhook: vi.fn(),
      listPaymentMethods: vi.fn().mockResolvedValue([
        {
          id: "pm_auto",
          last4: "4242",
          brand: "visa",
          expiryMonth: 12,
          expiryYear: 2030,
          isDefault: true,
        },
      ]),
      previewSavedPaymentCharge: vi.fn().mockResolvedValue({
        amountMinor: 1000,
        currency: "USD",
      }),
      chargeSavedPaymentMethod: vi.fn().mockImplementation(async () => outcomes.shift()),
    } satisfies PaymentProvider;
    await bs.upsertBillingCustomer(PROVIDER, "cus_auto_integration", USER_ID, "auto@example.com");

    await expect(bm.autoRecharge.quote({ userId: USER_ID, provider })).resolves.toEqual({
      amountMinor: 1000,
      currency: "USD",
    });
    await expect(bm.autoRecharge.getStatus({ userId: USER_ID, provider })).resolves.toMatchObject({
      enabled: false,
      state: "disabled",
      paymentMethodLast4: null,
    });

    const enabled = await bm.autoRecharge.enable({
      userId: USER_ID,
      provider,
      balance: new Decimal(2000),
    });
    expect(enabled).toMatchObject({ enabled: true, state: "active", quoteAmountMinor: 1000 });
    await expect(
      bm.autoRecharge.processIfNeeded({ userId: USER_ID, provider, balance: new Decimal(0) }),
    ).resolves.toMatchObject({ outcome: "submitted" });
    await expect(
      bm.autoRecharge.retry({ userId: USER_ID, provider, balance: new Decimal(0) }),
    ).resolves.toMatchObject({ outcome: "already_processing" });
    await expect(
      bm.ingestBillingEvent({
        provider: PROVIDER,
        eventId: "evt_auto_recharge_succeeded",
        eventType: "payment.succeeded",
        occurredAt: new Date().toISOString(),
        accountId: USER_ID,
        payment: {
          providerPaymentId: "auto_processing_initial",
          amountMinor: 1000,
          taxMinor: 0,
          currency: "USD",
          refs: { productId: "prod_topup", priceId: PRICE_ID_TOPUP },
          purpose: "credit_topup",
          status: "succeeded",
        },
      }),
    ).resolves.toMatchObject({ handled: true, action: "payment_succeeded" });
    expect((await cm.getBalance(USER_ID)).balance.toString()).toBe("1000");
    await expect(
      bm.autoRecharge.processIfNeeded({ userId: USER_ID, provider, balance: new Decimal(2000) }),
    ).resolves.toEqual({ outcome: "above_threshold" });

    await bm.autoRecharge.disable(USER_ID);
    await expect(
      bm.autoRecharge.retry({ userId: USER_ID, provider, balance: new Decimal(0) }),
    ).rejects.toThrow("disabled");
  });

  it("retries a failed auto-recharge with a fresh attempt and settles its provider payment", async () => {
    const { cm, bm, bs } = await makePgComponents(pool);
    const config: BursarConfigData = structuredClone(PRICING_DICT);
    const commerce = config.commerce;
    if (!commerce) throw new Error("expected commerce configuration");
    commerce.auto_recharge = {
      eligible_topups: ["standard_topup"],
      balance_below: { minimum: "100", maximum: "5000", default: "1000" },
      rearm_above: "6000",
      quantity: { minimum: 1, maximum: 3, default: 1 },
      limits: {
        max_purchases: 3,
        window: { type: "rolling", duration: { unit: "day", count: 30 } },
        max_charge_minor: 1000,
        cooldown: { unit: "hour", count: 1 },
      },
    };
    await cm.publishAndActivateCatalog(config);

    const charges = [
      {
        providerPaymentId: "auto_failed_first",
        status: "failed" as const,
        amountMinor: 1000,
        currency: "USD",
      },
      {
        providerPaymentId: "auto_retry_submitted",
        status: "succeeded" as const,
        amountMinor: 1000,
        currency: "USD",
      },
    ];
    const provider = {
      provider: PROVIDER,
      createCheckoutSession: vi.fn(),
      handleWebhook: vi.fn(),
      listPaymentMethods: vi.fn().mockResolvedValue([
        {
          id: "pm_auto_retry",
          last4: "4242",
          brand: "visa",
          expiryMonth: 12,
          expiryYear: 2030,
          isDefault: true,
        },
      ]),
      chargeSavedPaymentMethod: vi.fn().mockImplementation(async () => charges.shift()),
    } satisfies PaymentProvider;
    await bs.upsertBillingCustomer(PROVIDER, "cus_auto_retry", USER_ID2, "retry@example.com");

    await expect(
      bm.autoRecharge.enable({ userId: USER_ID2, provider, balance: new Decimal(0) }),
    ).resolves.toMatchObject({ enabled: true, state: "active" });
    await expect(
      pool.query(
        `SELECT state, failure_code
         FROM bursar.billing_auto_recharge_attempts
         WHERE subject_id = $1
         ORDER BY created_at`,
        [USER_ID2],
      ),
    ).resolves.toMatchObject({
      rows: [{ state: "failed", failure_code: "payment_failed" }],
    });

    await expect(
      bm.autoRecharge.retry({ userId: USER_ID2, provider, balance: new Decimal(0) }),
    ).resolves.toMatchObject({ outcome: "submitted" });
    const attempts = await pool.query(
      `SELECT state, provider_attempt_id
       FROM bursar.billing_auto_recharge_attempts
       WHERE subject_id = $1
       ORDER BY created_at`,
      [USER_ID2],
    );
    expect(attempts.rows).toEqual([
      { state: "failed", provider_attempt_id: "auto_failed_first" },
      { state: "processing", provider_attempt_id: "auto_retry_submitted" },
    ]);

    await expect(
      bm.ingestBillingEvent({
        provider: PROVIDER,
        eventId: "evt_auto_retry_settled",
        eventType: "payment.succeeded",
        occurredAt: new Date().toISOString(),
        accountId: USER_ID2,
        payment: {
          providerPaymentId: "auto_retry_submitted",
          amountMinor: 1000,
          taxMinor: 0,
          currency: "USD",
          refs: { productId: "prod_topup", priceId: PRICE_ID_TOPUP },
          purpose: "credit_topup",
          status: "succeeded",
        },
      }),
    ).resolves.toMatchObject({ handled: true, action: "payment_succeeded" });
    expect((await cm.getBalance(USER_ID2)).balance.toString()).toBe("1000");
    await expect(
      pool.query(
        `SELECT state FROM bursar.billing_auto_recharge_attempts
         WHERE subject_id = $1 AND provider_attempt_id = $2`,
        [USER_ID2, "auto_retry_submitted"],
      ),
    ).resolves.toMatchObject({ rows: [{ state: "succeeded" }] });
  });

  it("records a standalone invoice while rejecting a subscription-linked invoice before persistence", async () => {
    const { bm, bs } = await makePgComponents(pool);
    await expect(
      bs.upsertBillingInvoice({
        provider: PROVIDER,
        providerInvoiceId: "in_missing_subscription",
        providerSubscriptionId: "sub_missing_invoice",
        userId: USER_ID4,
        status: "open",
        amountDueMinor: 1000,
        amountPaidMinor: 0,
        currency: "USD",
        providerUpdatedAt: TEST_INSTANT,
      }),
    ).rejects.toMatchObject({ code: "STORE_ERROR", retryable: true });

    await bs.upsertBillingInvoice({
      provider: PROVIDER,
      providerInvoiceId: "in_standalone",
      userId: USER_ID4,
      status: "open",
      amountDueMinor: 1000,
      amountPaidMinor: 0,
      currency: "USD",
      metadata: { source: "manual_reconciliation" },
      providerUpdatedAt: TEST_INSTANT,
    });
    await expect(bm.listBillingInvoices(USER_ID4)).resolves.toMatchObject([
      expect.objectContaining({
        providerInvoiceId: "in_standalone",
        status: "open",
      }),
    ]);
  });

  it("pseudonymizes mutable financial identity data without deleting records", async () => {
    const { bm, bs } = await makePgComponents(pool);
    await bs.upsertBillingCustomer(PROVIDER, "cus_pseudonymize", USER_ID4, "pii@example.com");
    await bs.upsertBillingPayment({
      provider: PROVIDER,
      providerPaymentId: "py_pseudonymize",
      userId: USER_ID4,
      amountMinor: 1000,
      taxMinor: 0,
      currency: "USD",
      purpose: "subscription",
      status: "succeeded",
      providerUpdatedAt: TEST_INSTANT,
      metadata: { email: "pii@example.com" },
    });
    const tenantClient = await pool.connect();
    try {
      await tenantClient.query("BEGIN");
      await tenantClient.query("SELECT set_config('bursar.tenant_id', $1, true)", [TEST_TENANT_ID]);
      await tenantClient.query("SELECT set_config('bursar.provider_environment', 'test', true)");
      await tenantClient.query(
        `INSERT INTO bursar.external_identities(
           subject_id, provider, external_subject
         ) VALUES ($1, 'host', 'external-user-4')`,
        [USER_ID4],
      );
      await tenantClient.query("COMMIT");
    } catch (error) {
      await tenantClient.query("ROLLBACK");
      throw error;
    } finally {
      tenantClient.release();
    }

    await expect(bm.pseudonymizeFinancialSubject(USER_ID4)).resolves.toBeUndefined();
    await bs.upsertBillingCustomer(
      PROVIDER,
      "cus_pseudonymize",
      USER_ID4,
      "reintroduced@example.com",
    );
    await bs.upsertBillingPayment({
      provider: PROVIDER,
      providerPaymentId: "py_pseudonymize",
      userId: USER_ID4,
      amountMinor: 1000,
      taxMinor: 0,
      currency: "USD",
      purpose: "subscription",
      status: "succeeded",
      metadata: { email: "reintroduced@example.com" },
      providerUpdatedAt: new Date(Date.now() + 1_000).toISOString(),
    });
    const state = await pool.query(
      `SELECT
         subject.pseudonymized_at IS NOT NULL AS pseudonymized,
         customer.email,
         customer.metadata AS customer_metadata,
         payment.metadata AS payment_metadata,
         EXISTS (
           SELECT 1 FROM bursar.external_identities AS identity
           WHERE identity.subject_id = subject.id
         ) AS has_external_identity
       FROM bursar.subjects AS subject
       JOIN bursar.billing_customers AS customer ON customer.subject_id = subject.id
       JOIN bursar.billing_payments AS payment ON payment.subject_id = subject.id
       WHERE subject.id = $1`,
      [USER_ID4],
    );
    expect(state.rows[0]).toEqual({
      pseudonymized: true,
      email: null,
      customer_metadata: {},
      payment_metadata: {},
      has_external_identity: false,
    });
  });

  // ── Dispute lifecycle ───────────────────────────────────────────────────

  it("dispute created and closed", async () => {
    const { bm, bs } = await makePgComponents(pool);
    await bs.upsertBillingPayment({
      provider: PROVIDER,
      providerPaymentId: "py_disp",
      userId: USER_ID,
      amountMinor: 1000,
      taxMinor: 0,
      currency: "USD",
      purpose: "subscription",
      status: "succeeded",
      providerUpdatedAt: TEST_INSTANT,
    });
    const created = await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_disp_1",
      eventType: "dispute.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      customer: { providerCustomerId: "cus_disp" },
      dispute: {
        providerDisputeId: "dp_cycle",
        providerPaymentId: "py_disp",
        status: "needs_response",
        reason: "fraudulent",
      },
    });
    expect(created.action).toBe("dispute_recorded");
    const closed = await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_disp_2",
      eventType: "dispute.closed",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      customer: { providerCustomerId: "cus_disp" },
      dispute: {
        providerDisputeId: "dp_cycle",
        providerPaymentId: "py_disp",
        status: "won",
        reason: "won",
      },
    });
    expect(closed.action).toBe("dispute_closed");
  });

  it("fails closed when a dispute references an unknown payment", async () => {
    const { bm } = await makePgComponents(pool);
    await expect(
      bm.ingestBillingEvent({
        provider: PROVIDER,
        eventId: "evt_dispute_unknown_payment",
        eventType: "dispute.created",
        occurredAt: new Date().toISOString(),
        accountId: USER_ID,
        dispute: {
          providerDisputeId: "dp_unknown_payment",
          providerPaymentId: "py_unknown_payment",
          status: "needs_response",
          reason: "fraudulent",
        },
      }),
    ).resolves.toMatchObject({
      handled: false,
      error: "billing_event_processing_failed:STORE_ERROR",
    });
  });

  // ── Invoice.paid ────────────────────────────────────────────────────────

  it("invoice paid records invoice and renews subscription", async () => {
    const { cm, bm } = await makePgComponents(pool);
    await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_ip_1",
      eventType: "customer.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      customer: { providerCustomerId: "cus_ip" },
    });
    await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_ip_2",
      eventType: "subscription.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      customer: { providerCustomerId: "cus_ip" },
      subscription: {
        providerSubscriptionId: "sub_ip",
        status: "active",
        periodStart: "2025-06-01T00:00:00Z",
        periodEnd: "2025-07-01T00:00:00Z",
        refs: { productId: PRODUCT_ID, priceId: PRICE_ID },
      },
    });
    const result = await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_ip_3",
      eventType: "invoice.paid",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      customer: { providerCustomerId: "cus_ip" },
      subscription: {
        providerSubscriptionId: "sub_ip",
        status: "active",
        periodStart: "2025-07-01T00:00:00Z",
        periodEnd: "2025-08-01T00:00:00Z",
        refs: { productId: PRODUCT_ID, priceId: PRICE_ID },
      },
      invoice: {
        providerInvoiceId: "in_ip",
        status: "paid",
        amountPaidMinor: 1000,
        amountDueMinor: 1000,
        currency: "USD",
        periodStart: "2025-07-01T00:00:00Z",
        periodEnd: "2025-08-01T00:00:00Z",
      },
    });
    expect(result.handled).toBe(true);
    const plan = await cm.getUserPlan(USER_ID);
    expect(plan.planId).not.toBeNull();
  });

  // ── Subscription trial_will_end ─────────────────────────────────────────

  it("subscription trial will end callback", async () => {
    let called = false;
    const pool2 = new pg.Pool({ connectionString: DATABASE_URL!, max: 1 });
    const cs2 = new PostgresStore({
      postgres: DATABASE_URL!,
      tenantId: TEST_TENANT_ID,
      providerEnvironment: "test",
    });
    try {
      const cm2 = new CreditsService(cs2);
      await cm2.publishAndActivateCatalog(PRICING_DICT);
      const bs2 = new PostgresBillingStore({
        postgres: pool2,
        tenantId: TEST_TENANT_ID,
        providerEnvironment: "test",
      });
      const bm2 = new BillingService(bs2, {
        provisioning: cm2,
        eventHandlers: {
          [BillingEventType.SUBSCRIPTION_TRIAL_WILL_END]: async (_event, _userId) => {
            called = true;
          },
        },
      });
      const result = await bm2.ingestBillingEvent({
        provider: PROVIDER,
        eventId: "evt_twe_1",
        eventType: "subscription.trial_will_end",
        occurredAt: new Date().toISOString(),
        accountId: USER_ID,
        subscription: { providerSubscriptionId: "sub_trial_will_end" },
      });
      expect(result.handled).toBe(true);
      expect(called).toBe(true);
    } finally {
      await cs2.close().catch(() => {});
      await pool2.end().catch(() => {});
    }
  });

  // ── Subscription updated ────────────────────────────────────────────────

  it("subscription updated upserts state", async () => {
    const { cm, bm, bs } = await makePgComponents(pool);
    await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_up_1",
      eventType: "subscription.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      subscription: {
        providerSubscriptionId: "sub_up",
        status: "active",
        refs: { productId: PRODUCT_ID, priceId: PRICE_ID },
      },
    });
    await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_up_2",
      eventType: "subscription.updated",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      subscription: {
        providerSubscriptionId: "sub_up",
        status: "active",
        refs: { productId: PRODUCT_ID, priceId: PRICE_ID },
      },
    });
    const sub = await bs.getBillingSubscription(PROVIDER, "sub_up");
    expect(sub!.status).toBe("active");

    const unmapped = await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_up_unmapped_lookup",
      eventType: "subscription.updated",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      subscription: {
        providerSubscriptionId: "sub_up",
        status: "active",
        refs: { lookupKey: "provider-plan-slug-not-yet-catalogued" },
      },
    });
    expect(unmapped).toEqual({ handled: true, action: "subscription_updated" });
    expect((await cm.getUserPlan(USER_ID)).planKey).toBe("pro");
  });

  // ── Subscription plan changed ───────────────────────────────────────────

  it("subscription plan changed", async () => {
    const { cm, bm, bs } = await makePgComponents(pool);
    await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_pc_1",
      eventType: "subscription.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      subscription: {
        providerSubscriptionId: "sub_pc",
        status: "active",
        periodStart: TEST_INSTANT,
        refs: { productId: PRODUCT_ID, priceId: PRICE_ID },
      },
    });
    const originalPlan = await cm.getUserPlan(USER_ID);
    const target = await bs.resolveBillingOffer(PROVIDER, null, "price_yearly_10000");
    expect(target).not.toBeNull();
    await bm.createBillingSubscriptionChange({
      provider: PROVIDER,
      providerSubscriptionId: "sub_pc",
      toOfferId: target!.offerId,
      effectiveAt: "2030-01-01T00:00:00.000Z",
      effective: "immediate",
      idempotencyKey: "change:sub_pc:enterprise",
    });
    const result = await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_pc_2",
      eventType: "subscription.plan_changed",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      subscription: {
        providerSubscriptionId: "sub_pc",
        status: "active",
        refs: { priceId: "price_yearly_10000" },
      },
    });
    expect(result.action).toBe("subscription_plan_changed");
    const plan = await cm.getUserPlan(USER_ID);
    expect(plan.planKey).toBe("enterprise");
    expect(plan.planAssignedAt?.toISOString()).toBe(originalPlan.planAssignedAt?.toISOString());
    expect(await bm.getOpenBillingSubscriptionChange(PROVIDER, "sub_pc")).toBeNull();
    expect((await bm.getActiveSubscription(USER_ID))?.offerKey).toBe("enterprise_yearly");
  });

  // ── Ignored event types ─────────────────────────────────────────────────

  it("checkout.expired is ignored", async () => {
    const { bm } = await makePgComponents(pool);
    const result = await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_ign_1",
      eventType: "checkout.expired",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
    });
    expect(result.handled).toBe(true);
    expect(result.action).toBe("ignored");
  });

  // ── Payment edge cases ──────────────────────────────────────────────────

  it("payment succeeded without refs", async () => {
    const { bm } = await makePgComponents(pool);
    const result = await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_pay_norefs",
      eventType: "payment.succeeded",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      payment: {
        providerPaymentId: "py_norefs",
        amountMinor: 500,
        taxMinor: 0,
        currency: "USD",
        purpose: "subscription",
        status: "succeeded",
      },
    });
    expect(result.handled).toBe(true);
    expect(result.action).toBe("payment_succeeded");
  });

  it("payment succeeds without removed topup caps", async () => {
    const { bm } = await makePgComponents(pool);
    const result = await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_pay_cap",
      eventType: "payment.succeeded",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      customer: { providerCustomerId: "cus_paycap" },
      payment: {
        providerPaymentId: "py_cap",
        amountMinor: 99999,
        taxMinor: 0,
        currency: "USD",
        refs: { productId: "prod_topup", priceId: PRICE_ID_TOPUP },
        purpose: "credit_topup",
        status: "succeeded",
      },
    });
    expect(result.action).toBe("payment_succeeded");
  });

  it("rejects a top-up payment above the configured quantity ceiling", async () => {
    const { cm, bm } = await makePgComponents(pool);
    const result = await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_pay_topup_over_max",
      eventType: "payment.succeeded",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      payment: {
        providerPaymentId: "py_topup_over_max",
        amountMinor: 101_000,
        taxMinor: 0,
        currency: "USD",
        refs: { productId: "prod_topup", priceId: PRICE_ID_TOPUP },
        purpose: "credit_topup",
        status: "succeeded",
      },
    });

    expect(result).toMatchObject({
      handled: true,
      action: "payment_succeeded_out_of_bounds",
    });
    expect((await cm.getBalance(USER_ID)).balance.toString()).toBe("0");
  });

  // ── Manager no-creditManager edge cases ─────────────────────────────────

  it("subscription created without credit manager", async () => {
    const pool3 = new pg.Pool({ connectionString: DATABASE_URL!, max: 1 });
    try {
      const bs3 = new PostgresBillingStore({
        postgres: pool3,
        tenantId: TEST_TENANT_ID,
        providerEnvironment: "test",
      });
      const bm3 = new BillingService(bs3);
      const cs3 = new PostgresStore({
        postgres: pool3,
        tenantId: TEST_TENANT_ID,
        providerEnvironment: "test",
      });
      const cm3 = new CreditsService(cs3);
      await cm3.publishAndActivateCatalog(PRICING_DICT);
      const result = await bm3.ingestBillingEvent({
        provider: PROVIDER,
        eventId: "evt_nocm_1",
        eventType: "subscription.created",
        occurredAt: new Date().toISOString(),
        accountId: "00000000-0000-0000-0000-000000000010",
        subscription: {
          providerSubscriptionId: "sub_nocm",
          status: "active",
          refs: { productId: PRODUCT_ID, priceId: PRICE_ID },
        },
      });
      expect(result.handled).toBe(true);
    } finally {
      await pool3.end().catch(() => {});
    }
  });

  it("rejects dispute created without dispute data", async () => {
    const { bm } = await makePgComponents(pool);
    await expect(
      bm.ingestBillingEvent({
        provider: PROVIDER,
        eventId: "evt_disp_noop",
        eventType: "dispute.created",
        occurredAt: new Date().toISOString(),
        accountId: USER_ID,
      }),
    ).rejects.toThrow("dispute.created requires dispute data");
  });

  // ── Resolve offer by lookup key ─────────────────────────────────────────

  it("noncanonical lookup keys are not accepted by the typed catalog", async () => {
    const { bs } = await makePgComponents(pool);
    const offer = await bs.resolveBillingOfferByLookup(PROVIDER, "pro_monthly_lookup");
    expect(offer).toBeNull();
  });
  it("resolve billing offer by lookup key not found", async () => {
    const { bs } = await makePgComponents(pool);
    expect(await bs.resolveBillingOfferByLookup(PROVIDER, "nonexistent")).toBeNull();
  });

  // ── User subscriptions ──────────────────────────────────────────────────

  it("get user subscriptions lists all", async () => {
    const { bs } = await makePgComponents(pool);
    const listUid = "00000000-0000-0000-0000-000000000011";
    await bs.upsertBillingSubscription({
      userId: listUid,
      provider: PROVIDER,
      providerSubscriptionId: "sub_list_1",
      offerKey: "pro_monthly",
      status: "active",
      providerUpdatedAt: "2025-01-01T00:00:00Z",
      cancelAtPeriodEnd: false,
    });
    await bs.upsertBillingSubscription({
      userId: listUid,
      provider: "dodo",
      providerSubscriptionId: "sub_list_2",
      offerKey: "pro_monthly",
      status: "active",
      providerUpdatedAt: "2025-01-01T00:00:00Z",
      cancelAtPeriodEnd: false,
    });
    const subs = await bs.getUserSubscriptions(listUid);
    expect(subs.length).toBeGreaterThanOrEqual(2);
  });

  it("get user subscription filters by status", async () => {
    const { bs } = await makePgComponents(pool);
    const statusUid = "00000000-0000-0000-0000-000000000012";
    await bs.upsertBillingSubscription({
      userId: statusUid,
      provider: PROVIDER,
      providerSubscriptionId: "sub_status_1",
      offerKey: "pro_monthly",
      status: "canceled",
      providerUpdatedAt: "2025-01-01T00:00:00Z",
      cancelAtPeriodEnd: false,
    });
    const activeSub = await bs.getUserSubscription(statusUid, ["active"]);
    expect(activeSub).toBeNull();
    const canceledSub = await bs.getUserSubscription(statusUid, ["canceled"]);
    expect(canceledSub).not.toBeNull();
    expect(canceledSub!.status).toBe("canceled");
  });

  // ── Customer updated event ──────────────────────────────────────────────

  it("customer updated upserts customer", async () => {
    const { bm, bs } = await makePgComponents(pool);
    const result = await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_cus_upd_1",
      eventType: "customer.updated",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      customer: { providerCustomerId: "cus_upd", email: "updated@test.com" },
    });
    expect(result.handled).toBe(true);
    const uid = await bs.getBillingCustomer(PROVIDER, "cus_upd");
    expect(uid).toBe(USER_ID);
  });

  it("resolves webhook identity from durable references and preserves subscription status transitions", async () => {
    const { bm, bs } = await makePgComponents(pool);
    await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_identity_customer",
      eventType: "customer.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      customer: { providerCustomerId: "cus_identity" },
    });

    const created = await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_identity_subscription",
      eventType: "subscription.created",
      occurredAt: new Date().toISOString(),
      customer: { providerCustomerId: "cus_identity" },
      subscription: {
        providerSubscriptionId: "sub_identity",
        status: "active",
        refs: { priceId: PRICE_ID },
      },
    });
    expect(created).toMatchObject({ handled: true, action: "subscription_created" });

    const paused = await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_identity_paused",
      eventType: "subscription.paused",
      occurredAt: new Date().toISOString(),
      subscription: { providerSubscriptionId: "sub_identity" },
    });
    expect(paused).toMatchObject({ handled: true, action: "subscription_paused" });
    expect((await bs.getBillingSubscription(PROVIDER, "sub_identity"))?.status).toBe("paused");

    const resumed = await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_identity_resumed",
      eventType: "subscription.resumed",
      occurredAt: new Date().toISOString(),
      subscription: {
        providerSubscriptionId: "sub_identity",
        refs: { priceId: PRICE_ID },
      },
    });
    expect(resumed).toMatchObject({ handled: true, action: "subscription_resumed" });
    expect((await bs.getBillingSubscription(PROVIDER, "sub_identity"))?.status).toBe("active");

    await bs.upsertBillingPayment({
      provider: PROVIDER,
      providerPaymentId: "py_identity",
      userId: USER_ID,
      amountMinor: 1000,
      taxMinor: 0,
      currency: "USD",
      purpose: "subscription",
      status: "failed",
      providerUpdatedAt: TEST_INSTANT,
    });
    const failed = await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_identity_payment_failed",
      eventType: "payment.failed",
      occurredAt: new Date().toISOString(),
      payment: {
        providerPaymentId: "py_identity",
        amountMinor: 1000,
        taxMinor: 0,
        currency: "USD",
        purpose: "subscription",
        status: "failed",
      },
    });
    expect(failed).toMatchObject({ handled: true, action: "payment_failed_recorded" });
  });

  it("rejects subscription events without a resolvable account", async () => {
    const { bm } = await makePgComponents(pool);
    const eventTypes = [
      "subscription.created",
      "subscription.updated",
      "subscription.canceled",
    ] as const;

    for (const [index, eventType] of eventTypes.entries()) {
      const withoutAccount = await bm.ingestBillingEvent({
        provider: PROVIDER,
        eventId: `evt_missing_account_${index}`,
        eventType,
        occurredAt: new Date().toISOString(),
        subscription: { providerSubscriptionId: "sub-missing-account" },
      });
      expect(withoutAccount).toMatchObject({ handled: false, error: "account_not_found" });
    }
  });

  it("converts a concurrent subscription uniqueness race into a durable conflict", async () => {
    const { cm, bs: firstStore, bm: firstManager } = await makePgComponents(pool);
    const originalFirstUpsert = firstStore.upsertBillingSubscription.bind(firstStore);
    const secondManager = new BillingService(firstStore, { provisioning: cm });
    let arrivals = 0;
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const pauseBeforeUpsert = async (state: BillingSubscriptionState): Promise<void> => {
      arrivals += 1;
      if (arrivals === 2) release();
      await gate;
      if (state.providerSubscriptionId === "sub_race_1") {
        await originalFirstUpsert(state);
        return;
      }
      for (let attempt = 0; attempt < 20; attempt += 1) {
        const persisted = await pool.query(
          `SELECT 1
           FROM bursar.billing_subscriptions
           WHERE provider = $1 AND provider_subscription_id = $2
           LIMIT 1`,
          [PROVIDER, "sub_race_1"],
        );
        if (persisted.rowCount) break;
        await new Promise((resolve) => setTimeout(resolve, 5));
      }
      throw Object.assign(new Error("simulated unique constraint race"), { code: "23505" });
    };
    vi.spyOn(firstStore, "upsertBillingSubscription").mockImplementation((state) =>
      pauseBeforeUpsert(state),
    );

    try {
      const [first, second] = await Promise.all([
        firstManager.ingestBillingEvent({
          provider: PROVIDER,
          eventId: "evt_race_subscription_1",
          eventType: "subscription.created",
          occurredAt: new Date().toISOString(),
          accountId: USER_ID,
          subscription: {
            providerSubscriptionId: "sub_race_1",
            status: "active",
            refs: { priceId: PRICE_ID },
          },
        }),
        secondManager.ingestBillingEvent({
          provider: PROVIDER,
          eventId: "evt_race_subscription_2",
          eventType: "subscription.created",
          occurredAt: new Date().toISOString(),
          accountId: USER_ID,
          subscription: {
            providerSubscriptionId: "sub_race_2",
            status: "active",
            refs: { priceId: PRICE_ID },
          },
        }),
      ]);
      expect([first.handled, second.handled].sort()).toEqual([false, true]);
      expect([first.error, second.error].filter(Boolean)).toEqual(["subscription_conflict"]);
      const subscriptions = await firstStore.getUserSubscriptions(USER_ID);
      expect(subscriptions.filter((subscription) => subscription.status === "active")).toHaveLength(
        1,
      );
      const conflicts = await pool.query(
        `SELECT duplicate_provider_subscription_id
         FROM bursar.billing_subscription_conflicts`,
      );
      expect(conflicts.rows).toHaveLength(1);
    } finally {
      release();
    }
  });

  it("re-evaluates access across provider subscription state transitions", async () => {
    const { cm, bm, bs } = await makePgComponents(pool);
    const subscriptionId = "sub_access_transitions";
    const created = await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_access_created",
      eventType: "subscription.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      subscription: {
        providerSubscriptionId: subscriptionId,
        status: "active",
        refs: { priceId: PRICE_ID },
      },
    });
    expect(created).toMatchObject({ handled: true, action: "subscription_created" });
    expect((await cm.getUserPlan(USER_ID)).planKey).toBe("pro");

    const refreshed = await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_access_updated_without_refs",
      eventType: "subscription.updated",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      subscription: { providerSubscriptionId: subscriptionId, status: "active" },
    });
    expect(refreshed).toMatchObject({ handled: true, action: "subscription_updated" });
    expect((await cm.getUserPlan(USER_ID)).planKey).toBe("pro");

    const grace = await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_access_past_due",
      eventType: "subscription.updated",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      subscription: {
        providerSubscriptionId: subscriptionId,
        status: "past_due",
        refs: { priceId: PRICE_ID },
      },
    });
    expect(grace).toMatchObject({ handled: true, action: "subscription_updated" });
    expect((await cm.getUserPlan(USER_ID)).planKey).toBe("pro");

    const unmappedGrace = await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_access_past_due_unmapped",
      eventType: "subscription.updated",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      subscription: {
        providerSubscriptionId: subscriptionId,
        status: "past_due",
        refs: { lookupKey: "provider-plan-slug-not-yet-catalogued" },
      },
    });
    expect(unmappedGrace).toMatchObject({ handled: true, action: "subscription_updated" });
    expect((await cm.getUserPlan(USER_ID)).planKey).toBe("pro");

    const storedGrace = await bs.getBillingSubscription(PROVIDER, subscriptionId);
    if (!storedGrace) throw new Error("expected persisted past-due subscription");
    await bs.upsertBillingSubscription({
      ...storedGrace,
      graceEndsAt: new Date(Date.now() - 1_000).toISOString(),
      graceExpiredAt: null,
      providerUpdatedAt: new Date().toISOString(),
    });
    const expiredGrace = await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_access_past_due_expired",
      eventType: "subscription.updated",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      subscription: {
        providerSubscriptionId: subscriptionId,
        status: "past_due",
      },
    });
    expect(expiredGrace).toMatchObject({ handled: true, action: "subscription_updated" });
    expect((await cm.getUserPlan(USER_ID)).planKey).toBeNull();

    const canceled = await bm.ingestBillingEvent({
      provider: PROVIDER,
      eventId: "evt_access_canceled",
      eventType: "subscription.updated",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      subscription: { providerSubscriptionId: subscriptionId, status: "canceled" },
    });
    expect(canceled).toMatchObject({ handled: true, action: "subscription_updated" });
    expect((await cm.getUserPlan(USER_ID)).planKey).toBeNull();
    await expect(bs.getBillingSubscription(PROVIDER, subscriptionId)).resolves.toMatchObject({
      status: "canceled",
    });
  });
});
