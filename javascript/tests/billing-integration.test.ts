/**
 * Integration tests for PostgresBillingStore against a real Postgres.
 *
 * Mirrors Python test_billing_integration.py.
 */

import { describe, it, expect, beforeAll, afterAll, inject } from "vitest";
import pg from "pg";
import { PostgresStore } from "../src/stores/postgres-store.js";
import { CreditManager } from "../src/manager.js";
import { PostgresBillingStore, BillingManager, BillingEventType } from "../src/billing/index.js";
import type {
  BillingConfig,
  BillingPreferences,
  BillingSubscriptionState,
} from "../src/billing/index.js";
import { BOOTSTRAP_SQL, applyMigrations, truncateBursarTables } from "./helpers/bootstrap.js";

const DATABASE_URL = process.env.DATABASE_URL ?? inject("DATABASE_URL");

const USER_ID = "00000000-0000-0000-0000-000000000001";
const USER_ID2 = "00000000-0000-0000-0000-000000000002";
const USER_ID3 = "00000000-0000-0000-0000-000000000003";
const USER_ID4 = "00000000-0000-0000-0000-000000000004";
const PROVIDER = "stripe";
const CUSTOMER_ID = "cus_test123";
const CUSTOMER_ID2 = "cus_test456";
const SUB_ID = "sub_test789";
const SUB_ID2 = "sub_test012";
const PRODUCT_ID = "prod_monthly";
const PRICE_ID = "price_monthly_1000";
const PRICE_ID_TOPUP = "price_topup_credits";
const EVENT_ID = "evt_test_001";

const PRICING_DICT = {
  version: 1,
  metering: {
    models: { "*": "input_tokens * 1" },
  },
  ledger: {
    min_balance: 0,
    buckets: {
      purchased: {
        label: "Purchased",
        priority: 1,
        default: true,
        allow_overdraft: false,
      },
    },
  },
  plans: {
    free: { label: "Free", allowance: { amount: 1000 } },
    pro: { label: "Pro", allowance: { amount: 100000 } },
    enterprise: {
      label: "Enterprise",
      allowance: { amount: 1000000 },
    },
  },
};

const BILLING_CONFIG: BillingConfig = {
  subscriptions: {
    pro_monthly: {
      plan: "pro",
      interval: "month",
      intervalCount: 1,
      grant: { mode: "allowance" },
      validFrom: "2025-01-01",
      validTo: "2026-12-31",
      providers: {
        stripe: {
          productId: "prod_monthly",
          priceId: "price_monthly_1000",
        },
      },
    },
    enterprise_yearly: {
      plan: "enterprise",
      interval: "year",
      intervalCount: 1,
      grant: { mode: "allowance" },
      validFrom: "2025-06-01",
      providers: {
        stripe: {
          productId: "prod_yearly",
          priceId: "price_yearly_10000",
        },
      },
    },
    cycle_grant_monthly: {
      plan: "pro",
      interval: "month",
      intervalCount: 1,
      grant: {
        mode: "cycle_grant",
        credits: 5000,
        bucket: "purchased",
        replacePrior: true,
      },
      validTo: null,
      providers: {
        stripe: {
          productId: "prod_cycle_grant",
          priceId: "price_cycle_grant_5000",
        },
      },
    },
  },
  topups: {
    standard_topup: {
      creditsPerUnit: 1000,
      depositTo: "purchased",
      minAmountMinor: 500,
      maxAmountMinor: 50000,
      providers: {
        stripe: {
          productId: "prod_topup",
          priceId: "price_topup_credits",
        },
      },
    },
  },
};

async function makePgComponents(pool: pg.Pool) {
  const cs = new PostgresStore(DATABASE_URL!, pool);
  const cm = new CreditManager(cs);
  await cm.publishPricingFromDict(PRICING_DICT);
  const bs = new PostgresBillingStore(pool);
  const bm = new BillingManager(bs, { creditManager: cm });
  return { cs, cm, bs, bm };
}

// ── PostgresBillingStore (requires real Postgres) ────────────────────────

describe.runIf(DATABASE_URL)("PostgresBillingStore integration (real Postgres 16)", () => {
  let pool: pg.Pool;

  beforeAll(async () => {
    pool = new pg.Pool({ connectionString: DATABASE_URL!, max: 1 });
    await pool.query(BOOTSTRAP_SQL);
    await pool.query(
      "INSERT INTO auth.users (id) VALUES ($1), ($2), ($3), ($4) ON CONFLICT DO NOTHING",
      [USER_ID, USER_ID2, USER_ID3, USER_ID4],
    );
    await applyMigrations(pool);
    // Seed public.user for migration 021 FK from user_credits — range covers
    // all fixed IDs (1–4) plus inline IDs (5, 10–13, 99) used in this suite.
    await pool.query(
      `INSERT INTO public."user" (id)
       SELECT ('00000000-0000-0000-0000-' || LPAD(s::text, 12, '0'))::uuid
       FROM generate_series(1, 100) AS s
       ON CONFLICT (id) DO NOTHING`,
    );
    await truncateBursarTables(pool);
  }, 60000);

  afterAll(async () => {
    if (pool) await pool.end();
  });

  // ── Sync + Resolve ───────────────────────────────────────────────────

  it("sync billing config round-trip", async () => {
    const { bs } = await makePgComponents(pool);
    await bs.syncBillingFromConfig(BILLING_CONFIG);
    const offer = await bs.resolveBillingOffer(PROVIDER, null, PRICE_ID);
    expect(offer).not.toBeNull();
    expect(offer!.offerKey).toBe("pro_monthly");
    expect(offer!.plan).toBe("pro");
  });

  it("sync billing config resolve by product id", async () => {
    const { bs } = await makePgComponents(pool);
    await bs.syncBillingFromConfig(BILLING_CONFIG);
    const offer = await bs.resolveBillingOffer(PROVIDER, "prod_monthly");
    expect(offer).not.toBeNull();
    expect(offer!.offerKey).toBe("pro_monthly");
  });

  it("sync topup config round-trip", async () => {
    const { bs } = await makePgComponents(pool);
    await bs.syncBillingFromConfig(BILLING_CONFIG);
    const topup = await bs.resolveCreditTopup(PROVIDER, null, PRICE_ID_TOPUP);
    expect(topup).not.toBeNull();
    expect(topup!.topupKey).toBe("standard_topup");
    expect(topup!.creditsPerUnit).toBe(1000);
  });

  it("unresolved offer returns null", async () => {
    const { bs } = await makePgComponents(pool);
    await bs.syncBillingFromConfig(BILLING_CONFIG);
    expect(await bs.resolveBillingOffer(PROVIDER, null, "nonexistent")).toBeNull();
  });

  it("resolve billing offer no match", async () => {
    const { bs } = await makePgComponents(pool);
    await bs.syncBillingFromConfig(BILLING_CONFIG);
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

  it("customer updated replaces user id", async () => {
    const { bs } = await makePgComponents(pool);
    await bs.upsertBillingCustomer(PROVIDER, CUSTOMER_ID, USER_ID);
    await bs.upsertBillingCustomer(PROVIDER, CUSTOMER_ID, USER_ID2);
    expect(await bs.getBillingCustomer(PROVIDER, CUSTOMER_ID)).toBe(USER_ID2);
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
      status: "active",
    });
    await bs.upsertBillingSubscription({
      userId: USER_ID,
      provider: PROVIDER,
      providerSubscriptionId: SUB_ID,
      status: "canceled",
    });
    const sub = await bs.getBillingSubscription(PROVIDER, SUB_ID);
    expect(sub!.status).toBe("canceled");
  });

  // ── Event idempotency ────────────────────────────────────────────────

  it("event idempotency", async () => {
    const { bs, bm } = await makePgComponents(pool);
    await bs.syncBillingFromConfig(BILLING_CONFIG);
    const event = {
      provider: PROVIDER,
      eventId: EVENT_ID,
      eventType: "customer.created",
      occurredAt: new Date().toISOString(),
      userId: USER_ID,
    };
    const r1 = await bm.handleEvent(event);
    expect(r1.handled).toBe(true);
    const r2 = await bm.handleEvent(event);
    expect(r2.action).toBe("duplicate");
  });

  it("event claim complete fail cycle", async () => {
    const { bs } = await makePgComponents(pool);
    await bs.syncBillingFromConfig(BILLING_CONFIG);
    const c1 = await bs.claimBillingEvent(PROVIDER, "evt_claim_cycle", "test.event");
    expect(c1.status).toBe("claimed");
    await bs.completeBillingEvent(PROVIDER, "evt_claim_cycle");
    const c2 = await bs.claimBillingEvent(PROVIDER, "evt_claim_cycle", "test.event");
    expect(c2.status).toBe("duplicate");
  });

  it("event fail then reclaim", async () => {
    const { bs } = await makePgComponents(pool);
    await bs.syncBillingFromConfig(BILLING_CONFIG);
    const c1 = await bs.claimBillingEvent(PROVIDER, "evt_fail_retry", "test.event");
    expect(c1.status).toBe("claimed");
    await bs.failBillingEvent(PROVIDER, "evt_fail_retry");
    const c2 = await bs.claimBillingEvent(PROVIDER, "evt_fail_retry", "test.event");
    expect(c2.status).toBe("claimed");
  });

  // ── Topup credits ────────────────────────────────────────────────────

  it("compute topup credits", async () => {
    const { bs } = await makePgComponents(pool);
    expect(await bs.computeTopupCredits(2000, { creditsPerUnit: 1000 })).toBe(20000);
  });

  it("compute topup credits odd amount", async () => {
    const { bs } = await makePgComponents(pool);
    expect(await bs.computeTopupCredits(1999, { creditsPerUnit: 1000 })).toBe(19990);
  });

  // ── BillingManager lifecycle ─────────────────────────────────────────

  it("subscription lifecycle full", async () => {
    const { cm, bm, bs } = await makePgComponents(pool);
    await bs.syncBillingFromConfig(BILLING_CONFIG);
    await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_customer_1",
      eventType: "customer.created",
      occurredAt: new Date().toISOString(),
      userId: USER_ID,
      customer: { providerCustomerId: CUSTOMER_ID },
    });
    await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_sub_create_1",
      eventType: "subscription.created",
      occurredAt: new Date().toISOString(),
      userId: USER_ID,
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

    const cancelResult = await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_sub_cancel_1",
      eventType: "subscription.canceled",
      occurredAt: new Date().toISOString(),
      userId: USER_ID,
      customer: { providerCustomerId: CUSTOMER_ID },
      subscription: {
        providerSubscriptionId: SUB_ID,
        status: "canceled",
        refs: { productId: PRODUCT_ID, priceId: PRICE_ID },
      },
    });
    expect(cancelResult).toEqual({ handled: true, action: "subscription_canceled" });
    const plan2 = await cm.getUserPlan(USER_ID);
    expect(plan2.planId).toBeNull();
  });

  it("topup credit grant", async () => {
    const { cm, bm, bs } = await makePgComponents(pool);
    await bs.syncBillingFromConfig(BILLING_CONFIG);
    await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_customer_2",
      eventType: "customer.created",
      occurredAt: new Date().toISOString(),
      userId: USER_ID2,
      customer: { providerCustomerId: CUSTOMER_ID2 },
    });
    await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_payment_2",
      eventType: "payment.succeeded",
      occurredAt: new Date().toISOString(),
      userId: USER_ID2,
      customer: { providerCustomerId: CUSTOMER_ID2 },
      payment: {
        providerPaymentId: "py_test456",
        amountMinor: 2000,
        currency: "USD",
        refs: { productId: "prod_topup", priceId: PRICE_ID_TOPUP },
        purpose: "credit_topup",
      },
    });
    const balance = await cm.getBalance(USER_ID2);
    expect(balance.balance.toString()).toBe("20000");
  });

  it("subscription pause resume", async () => {
    const { cm, bm, bs } = await makePgComponents(pool);
    await bs.syncBillingFromConfig(BILLING_CONFIG);
    await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_cus_pause",
      eventType: "customer.created",
      occurredAt: new Date().toISOString(),
      userId: USER_ID2,
      customer: { providerCustomerId: CUSTOMER_ID2 },
    });
    await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_sub_pause_1",
      eventType: "subscription.created",
      occurredAt: new Date().toISOString(),
      userId: USER_ID2,
      customer: { providerCustomerId: CUSTOMER_ID2 },
      subscription: {
        providerSubscriptionId: SUB_ID2,
        status: "active",
        refs: { productId: PRODUCT_ID, priceId: PRICE_ID },
      },
    });
    expect((await cm.getUserPlan(USER_ID2)).planId).not.toBeNull();

    await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_sub_pause_2",
      eventType: "subscription.paused",
      occurredAt: new Date().toISOString(),
      userId: USER_ID2,
      customer: { providerCustomerId: CUSTOMER_ID2 },
      subscription: { providerSubscriptionId: SUB_ID2 },
    });
    expect((await cm.getUserPlan(USER_ID2)).planId).toBeNull();

    await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_sub_pause_3",
      eventType: "subscription.resumed",
      occurredAt: new Date().toISOString(),
      userId: USER_ID2,
      customer: { providerCustomerId: CUSTOMER_ID2 },
      subscription: {
        providerSubscriptionId: SUB_ID2,
        status: "active",
        refs: { productId: PRODUCT_ID, priceId: PRICE_ID },
      },
    });
    expect((await cm.getUserPlan(USER_ID2)).planId).not.toBeNull();
  });

  it("unknown event type is ignored", async () => {
    const { bm, bs } = await makePgComponents(pool);
    await bs.syncBillingFromConfig(BILLING_CONFIG);
    const result = await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_unknown",
      eventType: "some.unknown.event",
      occurredAt: new Date().toISOString(),
      userId: USER_ID,
    });
    expect(result.handled).toBe(false);
    expect(result.error).toBe("unhandled_event_type");
  });

  it("duplicate event skips side effects", async () => {
    const { bs, bm } = await makePgComponents(pool);
    await bs.syncBillingFromConfig(BILLING_CONFIG);
    await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_cus_dup",
      eventType: "customer.created",
      occurredAt: new Date().toISOString(),
      userId: USER_ID,
      customer: { providerCustomerId: "cus_dup_test" },
    });
    expect(await bs.getBillingCustomer(PROVIDER, "cus_dup_test")).toBe(USER_ID);
    await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_cus_dup",
      eventType: "customer.created",
      occurredAt: new Date().toISOString(),
      userId: USER_ID2,
      customer: { providerCustomerId: "cus_dup_test" },
    });
    expect(await bs.getBillingCustomer(PROVIDER, "cus_dup_test")).toBe(USER_ID);
  });

  it("provider scoped event id", async () => {
    const { bs } = await makePgComponents(pool);
    await bs.syncBillingFromConfig(BILLING_CONFIG);
    expect((await bs.claimBillingEvent("stripe", "evt_prov_scope", "test.event")).status).toBe(
      "claimed",
    );
    expect((await bs.claimBillingEvent("dodo", "evt_prov_scope", "test.event")).status).toBe(
      "claimed",
    );
  });

  it("sync offers adds new", async () => {
    const { bs } = await makePgComponents(pool);
    await bs.syncBillingFromConfig(BILLING_CONFIG);
    await bs.syncBillingFromConfig({
      subscriptions: {
        new_offer: {
          plan: "free",
          interval: "month",
          providers: {
            stripe: { priceId: "price_new_offer" },
          },
        },
      },
    });
    const newOffer = await bs.resolveBillingOffer("stripe", null, "price_new_offer");
    expect(newOffer).not.toBeNull();
    expect(newOffer!.offerKey).toBe("new_offer");
  });

  it("cycle grant credits granted", async () => {
    const { cm, bm, bs } = await makePgComponents(pool);
    await bs.syncBillingFromConfig(BILLING_CONFIG);
    await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_cus_cg1",
      eventType: "customer.created",
      occurredAt: new Date().toISOString(),
      userId: USER_ID3,
      customer: { providerCustomerId: CUSTOMER_ID2 },
    });
    await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_sub_cg1",
      eventType: "subscription.created",
      occurredAt: new Date().toISOString(),
      userId: USER_ID3,
      customer: { providerCustomerId: CUSTOMER_ID2 },
      subscription: {
        providerSubscriptionId: "sub_cg_test",
        status: "active",
        periodStart: "2025-06-01T00:00:00Z",
        periodEnd: "2025-07-01T00:00:00Z",
        refs: { productId: "prod_cycle_grant", priceId: "price_cycle_grant_5000" },
        interval: "month",
        intervalCount: 1,
      },
    });
    const balance = await cm.getBalance(USER_ID3);
    expect(balance.balance.toString()).toBe("5000");
  });

  it("refund clawback deducts credits", async () => {
    const { cm, bm, bs } = await makePgComponents(pool);
    await bs.syncBillingFromConfig(BILLING_CONFIG);
    const uid = "00000000-0000-0000-0000-000000000005";
    const paymentId = "py_refund_clawback";
    await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_cus_refund",
      eventType: "customer.created",
      occurredAt: new Date().toISOString(),
      userId: uid,
      customer: { providerCustomerId: "cus_refund_test" },
    });
    await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_pay_refund",
      eventType: "payment.succeeded",
      occurredAt: new Date().toISOString(),
      userId: uid,
      customer: { providerCustomerId: "cus_refund_test" },
      payment: {
        providerPaymentId: paymentId,
        amountMinor: 2000,
        currency: "USD",
        refs: { productId: "prod_topup", priceId: PRICE_ID_TOPUP },
        purpose: "credit_topup",
      },
    });
    const balanceAfterGrant = await cm.getBalance(uid);
    expect(balanceAfterGrant.balance.toString()).toBe("20000");

    const result = await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_refund_1",
      eventType: "refund.created",
      occurredAt: new Date().toISOString(),
      userId: uid,
      customer: { providerCustomerId: "cus_refund_test" },
      refund: {
        providerRefundId: "refund_1",
        providerPaymentId: paymentId,
        amountMinor: 2000,
        currency: "USD",
      },
    });
    expect(result.handled).toBe(true);
    const balanceAfterRefund = await cm.getBalance(uid);
    expect(balanceAfterRefund.balance.toString()).toBe("0");
  });

  it("cycle grant replace prior", async () => {
    const { cm, bm, bs } = await makePgComponents(pool);
    await bs.syncBillingFromConfig(BILLING_CONFIG);
    await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_cus_cg2",
      eventType: "customer.created",
      occurredAt: new Date().toISOString(),
      userId: USER_ID4,
      customer: { providerCustomerId: "cus_cg_replace" },
    });
    await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_sub_cg2a",
      eventType: "subscription.created",
      occurredAt: new Date().toISOString(),
      userId: USER_ID4,
      customer: { providerCustomerId: "cus_cg_replace" },
      subscription: {
        providerSubscriptionId: "sub_cg_replace",
        status: "active",
        periodStart: "2025-06-01T00:00:00Z",
        periodEnd: "2025-07-01T00:00:00Z",
        refs: { productId: "prod_cycle_grant", priceId: "price_cycle_grant_5000" },
        interval: "month",
        intervalCount: 1,
      },
    });
    const balance1 = await cm.getBalance(USER_ID4);
    expect(balance1.balance.toString()).toBe("5000");

    // Renew — should revoke prior cycle_grant and grant new 5000
    await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_sub_cg2b",
      eventType: "subscription.renewed",
      occurredAt: new Date().toISOString(),
      userId: USER_ID4,
      customer: { providerCustomerId: "cus_cg_replace" },
      subscription: {
        providerSubscriptionId: "sub_cg_replace",
        status: "active",
        periodStart: "2025-07-01T00:00:00Z",
        periodEnd: "2025-08-01T00:00:00Z",
        refs: { productId: "prod_cycle_grant", priceId: "price_cycle_grant_5000" },
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
    });
    await bs.upsertBillingDispute({
      provider: PROVIDER,
      providerDisputeId: "dp_001",
      providerPaymentId: "py_001",
      userId: USER_ID,
      status: "needs_response",
      reason: "fraudulent",
    });
    const payResult = await bs.getBillingPayment(PROVIDER, "py_001");
    expect(payResult).toBeNull();
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
      usageLimitAlerts: true,
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
    const config = await bs.getActivePricingConfig();
    expect(config).not.toBeNull();
    expect((config as Record<string, unknown>)?.version).toBe(1);
  });

  // ── Manager public API ──────────────────────────────────────────────────

  it("manager resolve offer and topup", async () => {
    const { bs, bm } = await makePgComponents(pool);
    await bs.syncBillingFromConfig(BILLING_CONFIG);
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
    await bs.syncBillingFromConfig(BILLING_CONFIG);
    await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_cus_del_1",
      eventType: "customer.created",
      occurredAt: new Date().toISOString(),
      userId: USER_ID,
      customer: { providerCustomerId: "cus_del_test" },
    });
    await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_sub_del_1",
      eventType: "subscription.created",
      occurredAt: new Date().toISOString(),
      userId: USER_ID,
      customer: { providerCustomerId: "cus_del_test" },
      subscription: {
        providerSubscriptionId: "sub_del_test",
        status: "active",
        refs: { productId: PRODUCT_ID, priceId: PRICE_ID },
      },
    });
    expect((await cm.getUserPlan(USER_ID)).planId).not.toBeNull();
    await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_cus_del_2",
      eventType: "customer.deleted",
      occurredAt: new Date().toISOString(),
      userId: USER_ID,
      customer: { providerCustomerId: "cus_del_test" },
    });
    expect((await cm.getUserPlan(USER_ID)).planId).toBeNull();
  });

  // ── Checkout completed ──────────────────────────────────────────────────

  it("checkout completed creates subscription", async () => {
    const { cm, bm, bs } = await makePgComponents(pool);
    await bs.syncBillingFromConfig(BILLING_CONFIG);
    const result = await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_chk_1",
      eventType: "checkout.completed",
      occurredAt: new Date().toISOString(),
      userId: USER_ID,
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

  it("checkout completed without subscription", async () => {
    const { bm, bs } = await makePgComponents(pool);
    await bs.syncBillingFromConfig(BILLING_CONFIG);
    const result = await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_chk_2",
      eventType: "checkout.completed",
      occurredAt: new Date().toISOString(),
      userId: USER_ID,
      customer: { providerCustomerId: "cus_chk2" },
    });
    expect(result.action).toBe("checkout_completed");
  });

  // ── Subscription activated ──────────────────────────────────────────────

  it("subscription activated provisions plan", async () => {
    const { cm, bm, bs } = await makePgComponents(pool);
    await bs.syncBillingFromConfig(BILLING_CONFIG);
    await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_act_1",
      eventType: "subscription.activated",
      occurredAt: new Date().toISOString(),
      userId: USER_ID,
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

  it("subscription cancellation scheduled and unscheduled", async () => {
    const { bm, bs } = await makePgComponents(pool);
    await bs.syncBillingFromConfig(BILLING_CONFIG);
    await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_cs_1",
      eventType: "subscription.created",
      occurredAt: new Date().toISOString(),
      userId: USER_ID,
      subscription: {
        providerSubscriptionId: "sub_cs_test",
        status: "active",
        refs: { productId: PRODUCT_ID, priceId: PRICE_ID },
      },
    });
    const schedResult = await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_cs_2",
      eventType: "subscription.cancellation_scheduled",
      occurredAt: new Date().toISOString(),
      userId: USER_ID,
      subscription: { providerSubscriptionId: "sub_cs_test" },
    });
    expect(schedResult.action).toBe("cancellation_scheduled");

    const unschedResult = await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_cs_3",
      eventType: "subscription.cancellation_unscheduled",
      occurredAt: new Date().toISOString(),
      userId: USER_ID,
      subscription: { providerSubscriptionId: "sub_cs_test" },
    });
    expect(unschedResult.action).toBe("cancellation_unscheduled");
  });

  // ── Subscription expired ────────────────────────────────────────────────

  it("subscription expired revokes plan", async () => {
    const { cm, bm, bs } = await makePgComponents(pool);
    await bs.syncBillingFromConfig(BILLING_CONFIG);
    await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_exp_1",
      eventType: "subscription.created",
      occurredAt: new Date().toISOString(),
      userId: USER_ID,
      subscription: {
        providerSubscriptionId: "sub_exp_test",
        status: "active",
        refs: { productId: PRODUCT_ID, priceId: PRICE_ID },
      },
    });
    await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_exp_2",
      eventType: "subscription.expired",
      occurredAt: new Date().toISOString(),
      userId: USER_ID,
      subscription: { providerSubscriptionId: "sub_exp_test" },
    });
    expect((await cm.getUserPlan(USER_ID)).planId).toBeNull();
  });

  // ── Payment failed ──────────────────────────────────────────────────────

  it("payment failed records and revokes", async () => {
    const { cm, bm, bs } = await makePgComponents(pool);
    await bs.syncBillingFromConfig(BILLING_CONFIG);
    await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_pf_1",
      eventType: "subscription.created",
      occurredAt: new Date().toISOString(),
      userId: USER_ID,
      subscription: {
        providerSubscriptionId: "sub_pf_test",
        status: "active",
        refs: { productId: PRODUCT_ID, priceId: PRICE_ID },
      },
    });
    const result = await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_pf_2",
      eventType: "payment.failed",
      occurredAt: new Date().toISOString(),
      userId: USER_ID,
      customer: { providerCustomerId: "cus_pf" },
      payment: {
        providerPaymentId: "py_pf",
        amountMinor: 1000,
        currency: "USD",
        purpose: "subscription",
      },
      subscription: { providerSubscriptionId: "sub_pf_test" },
    });
    expect(result.handled).toBe(true);
    expect((await cm.getUserPlan(USER_ID)).planId).toBeNull();
  });

  // ── Dispute lifecycle ───────────────────────────────────────────────────

  it("dispute created and closed", async () => {
    const { bm, bs } = await makePgComponents(pool);
    await bs.syncBillingFromConfig(BILLING_CONFIG);
    const created = await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_disp_1",
      eventType: "dispute.created",
      occurredAt: new Date().toISOString(),
      userId: USER_ID,
      customer: { providerCustomerId: "cus_disp" },
      dispute: {
        providerDisputeId: "dp_cycle",
        providerPaymentId: "py_disp",
        reason: "fraudulent",
      },
    });
    expect(created.action).toBe("dispute_recorded");
    const closed = await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_disp_2",
      eventType: "dispute.closed",
      occurredAt: new Date().toISOString(),
      userId: USER_ID,
      customer: { providerCustomerId: "cus_disp" },
      dispute: {
        providerDisputeId: "dp_cycle",
        providerPaymentId: "py_disp",
        reason: "won",
      },
    });
    expect(closed.action).toBe("dispute_closed");
  });

  // ── Invoice.paid ────────────────────────────────────────────────────────

  it("invoice paid records invoice and renews subscription", async () => {
    const { cm, bm, bs } = await makePgComponents(pool);
    await bs.syncBillingFromConfig(BILLING_CONFIG);
    await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_ip_1",
      eventType: "customer.created",
      occurredAt: new Date().toISOString(),
      userId: USER_ID,
      customer: { providerCustomerId: "cus_ip" },
    });
    await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_ip_2",
      eventType: "subscription.created",
      occurredAt: new Date().toISOString(),
      userId: USER_ID,
      customer: { providerCustomerId: "cus_ip" },
      subscription: {
        providerSubscriptionId: "sub_ip",
        status: "active",
        periodStart: "2025-06-01T00:00:00Z",
        periodEnd: "2025-07-01T00:00:00Z",
        refs: { productId: PRODUCT_ID, priceId: PRICE_ID },
      },
    });
    const result = await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_ip_3",
      eventType: "invoice.paid",
      occurredAt: new Date().toISOString(),
      userId: USER_ID,
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
    const cs2 = new PostgresStore(DATABASE_URL!);
    try {
      const cm2 = new CreditManager(cs2);
      await cm2.publishPricingFromDict(PRICING_DICT);
      const bs2 = new PostgresBillingStore(pool2);
      const bm2 = new BillingManager(bs2, {
        creditManager: cm2,
        eventHandlers: {
          [BillingEventType.SUBSCRIPTION_TRIAL_WILL_END]: async (_event, _userId) => {
            called = true;
          },
        },
      });
      await bs2.syncBillingFromConfig(BILLING_CONFIG);
      const result = await bm2.handleEvent({
        provider: PROVIDER,
        eventId: "evt_twe_1",
        eventType: "subscription.trial_will_end",
        occurredAt: new Date().toISOString(),
        userId: USER_ID,
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
    const { bm, bs } = await makePgComponents(pool);
    await bs.syncBillingFromConfig(BILLING_CONFIG);
    await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_up_1",
      eventType: "subscription.created",
      occurredAt: new Date().toISOString(),
      userId: USER_ID,
      subscription: {
        providerSubscriptionId: "sub_up",
        status: "active",
        refs: { productId: PRODUCT_ID, priceId: PRICE_ID },
      },
    });
    await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_up_2",
      eventType: "subscription.updated",
      occurredAt: new Date().toISOString(),
      userId: USER_ID,
      subscription: {
        providerSubscriptionId: "sub_up",
        status: "active",
        refs: { productId: PRODUCT_ID, priceId: PRICE_ID },
      },
    });
    const sub = await bs.getBillingSubscription(PROVIDER, "sub_up");
    expect(sub!.status).toBe("active");
  });

  // ── Subscription plan changed ───────────────────────────────────────────

  it("subscription plan changed", async () => {
    const { cm, bm, bs } = await makePgComponents(pool);
    await bs.syncBillingFromConfig(BILLING_CONFIG);
    await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_pc_1",
      eventType: "subscription.created",
      occurredAt: new Date().toISOString(),
      userId: USER_ID,
      subscription: {
        providerSubscriptionId: "sub_pc",
        status: "active",
        refs: { productId: PRODUCT_ID, priceId: PRICE_ID },
      },
    });
    const result = await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_pc_2",
      eventType: "subscription.plan_changed",
      occurredAt: new Date().toISOString(),
      userId: USER_ID,
      subscription: {
        providerSubscriptionId: "sub_pc",
        status: "active",
        refs: { productId: PRODUCT_ID, priceId: PRICE_ID },
      },
    });
    expect(result.action).toBe("subscription_plan_changed");
    const plan = await cm.getUserPlan(USER_ID);
    expect(plan.planId).not.toBeNull();
  });

  // ── Ignored event types ─────────────────────────────────────────────────

  it("checkout.expired is ignored", async () => {
    const { bm, bs } = await makePgComponents(pool);
    await bs.syncBillingFromConfig(BILLING_CONFIG);
    const result = await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_ign_1",
      eventType: "checkout.expired",
      occurredAt: new Date().toISOString(),
      userId: USER_ID,
    });
    expect(result.handled).toBe(true);
    expect(result.action).toBe("ignored");
  });

  // ── Payment edge cases ──────────────────────────────────────────────────

  it("payment succeeded without refs", async () => {
    const { bm, bs } = await makePgComponents(pool);
    await bs.syncBillingFromConfig(BILLING_CONFIG);
    const result = await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_pay_norefs",
      eventType: "payment.succeeded",
      occurredAt: new Date().toISOString(),
      userId: USER_ID,
      payment: {
        providerPaymentId: "py_norefs",
        amountMinor: 500,
        currency: "USD",
        purpose: "subscription",
      },
    });
    expect(result.handled).toBe(true);
    expect(result.action).toBe("payment_succeeded");
  });

  it("payment succeeded amount exceeds topup cap", async () => {
    const { bm, bs } = await makePgComponents(pool);
    await bs.syncBillingFromConfig(BILLING_CONFIG);
    const result = await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_pay_cap",
      eventType: "payment.succeeded",
      occurredAt: new Date().toISOString(),
      userId: USER_ID,
      customer: { providerCustomerId: "cus_paycap" },
      payment: {
        providerPaymentId: "py_cap",
        amountMinor: 99999,
        currency: "USD",
        refs: { productId: "prod_topup", priceId: PRICE_ID_TOPUP },
        purpose: "credit_topup",
      },
    });
    expect(result.action).toBe("payment_succeeded_out_of_bounds");
  });

  // ── Manager no-creditManager edge cases ─────────────────────────────────

  it("subscription created without credit manager", async () => {
    const pool3 = new pg.Pool({ connectionString: DATABASE_URL!, max: 1 });
    try {
      const bs3 = new PostgresBillingStore(pool3);
      const bm3 = new BillingManager(bs3);
      await bs3.syncBillingFromConfig(BILLING_CONFIG);
      const result = await bm3.handleEvent({
        provider: PROVIDER,
        eventId: "evt_nocm_1",
        eventType: "subscription.created",
        occurredAt: new Date().toISOString(),
        userId: "00000000-0000-0000-0000-000000000010",
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

  it("dispute created without dispute data", async () => {
    const { bm, bs } = await makePgComponents(pool);
    await bs.syncBillingFromConfig(BILLING_CONFIG);
    const result = await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_disp_noop",
      eventType: "dispute.created",
      occurredAt: new Date().toISOString(),
      userId: USER_ID,
    });
    expect(result.action).toBe("dispute_recorded");
  });

  // ── Resolve offer by lookup key ─────────────────────────────────────────

  it("resolve billing offer by lookup key", async () => {
    const { bs } = await makePgComponents(pool);
    const lookupCfg: BillingConfig = {
      subscriptions: {
        lookup_offer: {
          plan: "pro",
          interval: "month",
          grant: { mode: "allowance" },
          providers: {
            stripe: {
              priceId: "price_lookup",
              lookupKey: "pro_monthly_lookup",
            },
          },
        },
      },
    };
    await bs.syncBillingFromConfig(lookupCfg);
    const offer = await bs.resolveBillingOfferByLookup(PROVIDER, "pro_monthly_lookup");
    expect(offer).not.toBeNull();
    expect(offer!.offerKey).toBe("lookup_offer");
  });

  it("resolve billing offer by lookup key not found", async () => {
    const { bs } = await makePgComponents(pool);
    await bs.syncBillingFromConfig(BILLING_CONFIG);
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
      status: "active",
    });
    await bs.upsertBillingSubscription({
      userId: listUid,
      provider: "dodo",
      providerSubscriptionId: "sub_list_2",
      status: "active",
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
      status: "canceled",
    });
    const activeSub = await bs.getUserSubscription(statusUid, ["active"]);
    expect(activeSub).toBeNull();
    const canceledSub = await bs.getUserSubscription(statusUid, ["canceled"]);
    expect(canceledSub).not.toBeNull();
    expect(canceledSub!.status).toBe("canceled");
  });

  // ── Deactivate other provider subscriptions ─────────────────────────────

  it("deactivate other provider subscriptions", async () => {
    const { bs } = await makePgComponents(pool);
    const daUid = "00000000-0000-0000-0000-000000000013";
    await bs.upsertBillingSubscription({
      userId: daUid,
      provider: "stripe",
      providerSubscriptionId: "sub_da_1",
      status: "active",
    });
    await bs.upsertBillingSubscription({
      userId: daUid,
      provider: "dodo",
      providerSubscriptionId: "sub_da_2",
      status: "active",
    });
    const result = await bs.deactivateOtherProviderSubscriptions(daUid, "dodo");
    expect(result.deactivatedCount).toBeGreaterThanOrEqual(1);
    const stripeSub = await bs.getBillingSubscription("stripe", "sub_da_1");
    expect(stripeSub!.status).toBe("canceled");
  });

  // ── Customer updated event ──────────────────────────────────────────────

  it("customer updated upserts customer", async () => {
    const { bm, bs } = await makePgComponents(pool);
    await bs.syncBillingFromConfig(BILLING_CONFIG);
    const result = await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_cus_upd_1",
      eventType: "customer.updated",
      occurredAt: new Date().toISOString(),
      userId: USER_ID,
      customer: { providerCustomerId: "cus_upd", email: "updated@test.com" },
    });
    expect(result.handled).toBe(true);
    const uid = await bs.getBillingCustomer(PROVIDER, "cus_upd");
    expect(uid).toBe(USER_ID);
  });
});
