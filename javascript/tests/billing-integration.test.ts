/**
 * Integration tests for billing stores — MemoryBillingStore (always) and
 * PostgresBillingStore (when a real Postgres is available).
 *
 * Mirrors Python test_billing_integration.py.
 */

import { describe, it, expect, beforeAll, afterAll, inject } from "vitest";
import pg from "pg";
import { PostgresStore } from "../src/stores/postgres-store.js";
import { MemoryStore } from "../src/stores/memory-store.js";
import { CreditManager } from "../src/manager.js";
import { MemoryBillingStore, PostgresBillingStore, BillingManager } from "../src/billing/index.js";
import type { BillingConfig, BillingSubscriptionState } from "../src/billing/index.js";

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

async function makeComponents() {
  const cs = new MemoryStore();
  const cm = new CreditManager(cs);
  cm.publishPricingFromDict(PRICING_DICT);
  const bs = new MemoryBillingStore();
  const bm = new BillingManager(bs, { creditManager: cm });
  await bs.syncBillingFromConfig(BILLING_CONFIG);
  return { cs, cm, bs, bm };
}

async function makePgComponents(pool: pg.Pool) {
  const cs = new PostgresStore(DATABASE_URL!, pool);
  const cm = new CreditManager(cs);
  await cm.publishPricingFromDict(PRICING_DICT);
  const bs = new PostgresBillingStore(pool);
  const bm = new BillingManager(bs, { creditManager: cm });
  return { cs, cm, bs, bm };
}

// ── MemoryBillingStore (always runs) ─────────────────────────────────────

describe("MemoryBillingStore integration", () => {
  it("sync_billing_config_roundtrip", async () => {
    const { bs } = await makeComponents();
    const offer = await bs.resolveBillingOffer(PROVIDER, null, PRICE_ID);
    expect(offer).not.toBeNull();
    expect(offer!.offerKey).toBe("pro_monthly");
    expect(offer!.plan).toBe("pro");
  });

  it("sync_billing_config_resolve_by_product_id", async () => {
    const { bs } = await makeComponents();
    const offer = await bs.resolveBillingOffer(PROVIDER, "prod_monthly");
    expect(offer).not.toBeNull();
    expect(offer!.offerKey).toBe("pro_monthly");
  });

  it("sync_topup_config_roundtrip", async () => {
    const { bs } = await makeComponents();
    const topup = await bs.resolveCreditTopup(PROVIDER, null, PRICE_ID_TOPUP);
    expect(topup).not.toBeNull();
    expect(topup!.topupKey).toBe("standard_topup");
    expect(topup!.creditsPerUnit).toBe(1000);
  });

  it("unresolved_offer_returns_null", async () => {
    const { bs } = await makeComponents();
    expect(await bs.resolveBillingOffer(PROVIDER, null, "nonexistent")).toBeNull();
  });

  it("customer_created_roundtrip", async () => {
    const { bs } = await makeComponents();
    await bs.upsertBillingCustomer(PROVIDER, CUSTOMER_ID, USER_ID, "test@example.com");
    const uid = await bs.getBillingCustomer(PROVIDER, CUSTOMER_ID);
    expect(uid).toBe(USER_ID);
  });

  it("customer_updated_replaces_user_id", async () => {
    const { bs } = await makeComponents();
    await bs.upsertBillingCustomer(PROVIDER, CUSTOMER_ID, USER_ID);
    await bs.upsertBillingCustomer(PROVIDER, CUSTOMER_ID, USER_ID2);
    expect(await bs.getBillingCustomer(PROVIDER, CUSTOMER_ID)).toBe(USER_ID2);
  });

  it("event_idempotency", async () => {
    const { bs, bm } = await makeComponents();
    const event = {
      provider: PROVIDER,
      eventId: EVENT_ID,
      eventType: "customer.created",
      occurredAt: new Date().toISOString(),
      userId: USER_ID,
    };
    await bs.syncBillingFromConfig(BILLING_CONFIG);
    const r1 = await bm.handleEvent(event);
    expect(r1.handled).toBe(true);
    const r2 = await bm.handleEvent(event);
    expect(r2.action).toBe("duplicate");
  });

  it("event_claim_complete_fail_cycle", async () => {
    const { bs } = await makeComponents();
    expect((await bs.claimBillingEvent(PROVIDER, EVENT_ID, "test.event")).status).toBe("claimed");
    await bs.completeBillingEvent(PROVIDER, EVENT_ID);
    expect((await bs.claimBillingEvent(PROVIDER, EVENT_ID, "test.event")).status).toBe("duplicate");
  });

  it("event_fail_then_retry", async () => {
    const { bs } = await makeComponents();
    expect((await bs.claimBillingEvent(PROVIDER, EVENT_ID, "test.event")).status).toBe("claimed");
    await bs.failBillingEvent(PROVIDER, EVENT_ID);
    expect((await bs.claimBillingEvent(PROVIDER, EVENT_ID, "test.event")).status).toBe("retry");
  });

  it("multiple_providers_same_customer_id", async () => {
    const { bs } = await makeComponents();
    await bs.upsertBillingCustomer("stripe", CUSTOMER_ID, USER_ID);
    await bs.upsertBillingCustomer("dodo", CUSTOMER_ID, USER_ID2);
    expect(await bs.getBillingCustomer("stripe", CUSTOMER_ID)).toBe(USER_ID);
    expect(await bs.getBillingCustomer("dodo", CUSTOMER_ID)).toBe(USER_ID2);
  });

  it("subscription_upsert_and_read", async () => {
    const { bs } = await makeComponents();
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

  it("subscription_update", async () => {
    const { bs } = await makeComponents();
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
    const result = await bs.getBillingSubscription(PROVIDER, SUB_ID);
    expect(result!.status).toBe("canceled");
  });

  it("subscription_not_found", async () => {
    const { bs } = await makeComponents();
    expect(await bs.getBillingSubscription(PROVIDER, "nonexistent_sub")).toBeNull();
  });

  it("customer_not_found", async () => {
    const { bs } = await makeComponents();
    expect(await bs.getBillingCustomer(PROVIDER, "nonexistent_cus")).toBeNull();
  });

  it("compute_topup_credits", async () => {
    const { bs } = await makeComponents();
    expect(await bs.computeTopupCredits(2000, { creditsPerUnit: 1000 })).toBe(20000);
  });

  it("compute_topup_credits_odd_amount", async () => {
    const { bs } = await makeComponents();
    expect(await bs.computeTopupCredits(1999, { creditsPerUnit: 1000 })).toBe(19990);
  });

  it("resolve_billing_offer_no_match", async () => {
    const { bs } = await makeComponents();
    expect(await bs.resolveBillingOffer("nonexistent_provider", null, PRICE_ID)).toBeNull();
  });

  it("provider_scoped_event_id", async () => {
    const { bs } = await makeComponents();
    expect((await bs.claimBillingEvent("stripe", EVENT_ID, "test.event")).status).toBe("claimed");
    expect((await bs.claimBillingEvent("dodo", EVENT_ID, "test.event")).status).toBe("claimed");
  });

  it("duplicate_event_skips_side_effects", async () => {
    const { bs, bm } = await makeComponents();
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
    // Duplicate should NOT update the customer mapping
    expect(await bs.getBillingCustomer(PROVIDER, "cus_dup_test")).toBe(USER_ID);
  });

  it("unknown_event_type_is_ignored", async () => {
    const { bm } = await makeComponents();
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

  it("sync_offers_replaces_all", async () => {
    const { bs } = await makeComponents();
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
    expect(await bs.resolveBillingOffer(PROVIDER, null, PRICE_ID)).toBeNull();
    expect(await bs.resolveBillingOffer("stripe", null, "price_new_offer")).not.toBeNull();
  });

  it("cycle_grant_credits_granted", async () => {
    const { cm, bm, bs } = await makeComponents();
    await bs.syncBillingFromConfig(BILLING_CONFIG);
    await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_cus_cg1",
      eventType: "customer.created",
      occurredAt: new Date().toISOString(),
      userId: USER_ID2,
      customer: { providerCustomerId: CUSTOMER_ID2 },
    });
    await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_sub_cg1",
      eventType: "subscription.created",
      occurredAt: new Date().toISOString(),
      userId: USER_ID2,
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
    const balance = await cm.getBalance(USER_ID2);
    expect(balance.balance.toString()).toBe("5000");
  });

  it("cycle_grant_replace_prior", async () => {
    const { cm, bm, bs } = await makeComponents();
    await bs.syncBillingFromConfig(BILLING_CONFIG);
    await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_cus_cg2",
      eventType: "customer.created",
      occurredAt: new Date().toISOString(),
      userId: USER_ID,
      customer: { providerCustomerId: "cus_cg_replace" },
    });
    await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_sub_cg2a",
      eventType: "subscription.created",
      occurredAt: new Date().toISOString(),
      userId: USER_ID,
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
    const balance1 = await cm.getBalance(USER_ID);
    expect(balance1.balance.toString()).toBe("5000");

    // Renew — should revoke prior cycle_grant and grant new 5000
    await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_sub_cg2b",
      eventType: "subscription.renewed",
      occurredAt: new Date().toISOString(),
      userId: USER_ID,
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
    const balance2 = await cm.getBalance(USER_ID);
    // Old 5000 revoked, new 5000 granted = 5000
    expect(balance2.balance.toString()).toBe("5000");
  });

  it("payment_persisted", async () => {
    const { bs, bm } = await makeComponents();
    await bs.syncBillingFromConfig(BILLING_CONFIG);
    await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_pay_persist",
      eventType: "payment.succeeded",
      occurredAt: new Date().toISOString(),
      userId: USER_ID,
      payment: {
        providerPaymentId: "py_persist_001",
        amountMinor: 2000,
        taxMinor: 200,
        currency: "USD",
        purpose: "credit_topup",
      },
    });
    const payment = await bs.getBillingPayment(PROVIDER, "py_persist_001");
    expect(payment).not.toBeNull();
    expect(payment!.purpose).toBe("credit_topup");
  });

  it("refund_persisted_and_claws_back", async () => {
    const { cm, bm, bs } = await makeComponents();
    await bs.syncBillingFromConfig(BILLING_CONFIG);
    await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_cus_refund",
      eventType: "customer.created",
      occurredAt: new Date().toISOString(),
      userId: USER_ID,
      customer: { providerCustomerId: "cus_refund_test" },
    });
    // Grant topup credits via payment
    await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_pay_refund",
      eventType: "payment.succeeded",
      occurredAt: new Date().toISOString(),
      userId: USER_ID,
      customer: { providerCustomerId: "cus_refund_test" },
      payment: {
        providerPaymentId: "py_refund_001",
        amountMinor: 1000,
        currency: "USD",
        refs: { productId: "prod_topup", priceId: PRICE_ID_TOPUP },
        purpose: "credit_topup",
      },
    });
    const balanceBefore = await cm.getBalance(USER_ID);
    expect(balanceBefore.balance.toString()).toBe("10000");

    // Issue refund
    await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_refund_001",
      eventType: "refund.created",
      occurredAt: new Date().toISOString(),
      userId: USER_ID,
      customer: { providerCustomerId: "cus_refund_test" },
      refund: {
        providerRefundId: "ref_001",
        providerPaymentId: "py_refund_001",
        amountMinor: 1000,
        currency: "USD",
        reason: "customer_request",
      },
    });

    const refund = await bs.getBillingPayment(PROVIDER, "py_refund_001");
    expect(refund).not.toBeNull();

    const balanceAfter = await cm.getBalance(USER_ID);
    // 10000 credits granted (1000 minor * 1000 credits/major / 100),
    // full refund claws back all 10000 credits
    expect(balanceAfter.balance.toString()).toBe("0");
  });

  it("payment_failed_persisted_and_revokes", async () => {
    const { cm, bm, bs } = await makeComponents();
    await bs.syncBillingFromConfig(BILLING_CONFIG);
    await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_cus_pf",
      eventType: "customer.created",
      occurredAt: new Date().toISOString(),
      userId: USER_ID,
      customer: { providerCustomerId: "cus_pf_test" },
    });
    await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_sub_pf",
      eventType: "subscription.created",
      occurredAt: new Date().toISOString(),
      userId: USER_ID,
      customer: { providerCustomerId: "cus_pf_test" },
      subscription: {
        providerSubscriptionId: "sub_pf_test",
        status: "active",
        refs: { productId: PRODUCT_ID, priceId: PRICE_ID },
      },
    });
    expect((await cm.getUserPlan(USER_ID)).planId).not.toBeNull();

    await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_pay_fail",
      eventType: "payment.failed",
      occurredAt: new Date().toISOString(),
      userId: USER_ID,
      customer: { providerCustomerId: "cus_pf_test" },
      payment: {
        providerPaymentId: "py_fail_001",
        amountMinor: 1000,
        currency: "USD",
        purpose: "subscription",
      },
      subscription: {
        providerSubscriptionId: "sub_pf_test",
        status: "past_due",
        refs: { productId: PRODUCT_ID, priceId: PRICE_ID },
      },
    });

    const payment = await bs.getBillingPayment(PROVIDER, "py_fail_001");
    expect(payment).not.toBeNull();

    const plan = await cm.getUserPlan(USER_ID);
    expect(plan.planId).toBeNull();
  });

  it("dispute_persisted", async () => {
    const { bs, bm } = await makeComponents();
    await bs.syncBillingFromConfig(BILLING_CONFIG);
    await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_dispute_001",
      eventType: "dispute.created",
      occurredAt: new Date().toISOString(),
      userId: USER_ID,
    });
    // Dispute created — no providerPaymentId, just persisted
    const result = await bm.handleEvent({
      provider: PROVIDER,
      eventId: "evt_dispute_002",
      eventType: "dispute.created",
      occurredAt: new Date().toISOString(),
      userId: USER_ID,
    });
    expect(result.handled).toBe(true);
  });
});

// ── PostgresBillingStore (requires real Postgres) ────────────────────────

const describePg = DATABASE_URL ? describe : describe.skip;

describePg("PostgresBillingStore integration (real Postgres 16)", () => {
  let pool: pg.Pool;

  beforeAll(async () => {
    pool = new pg.Pool({ connectionString: DATABASE_URL!, max: 1 });
    // Bootstrap roles + auth schema + stubs + seed test users
    await pool.query(
      `DO $$ BEGIN CREATE ROLE anon NOLOGIN; EXCEPTION WHEN duplicate_object THEN NULL; END $$`,
    );
    await pool.query(
      `DO $$ BEGIN CREATE ROLE authenticated NOLOGIN; EXCEPTION WHEN duplicate_object THEN NULL; END $$`,
    );
    await pool.query(
      `DO $$ BEGIN CREATE ROLE service_role NOLOGIN; EXCEPTION WHEN duplicate_object THEN NULL; END $$`,
    );
    await pool.query("CREATE SCHEMA IF NOT EXISTS auth");
    await pool.query("CREATE TABLE IF NOT EXISTS auth.users (id uuid PRIMARY KEY)");
    await pool.query(
      `CREATE OR REPLACE FUNCTION auth.role() RETURNS text LANGUAGE SQL IMMUTABLE AS $$ SELECT 'service_role'::text $$`,
    );
    await pool.query(
      `CREATE OR REPLACE FUNCTION auth.uid() RETURNS uuid LANGUAGE SQL IMMUTABLE AS $$ SELECT '00000000-0000-0000-0000-000000000000'::uuid $$`,
    );
    await pool.query(
      "INSERT INTO auth.users (id) VALUES ($1), ($2), ($3), ($4) ON CONFLICT DO NOTHING",
      [USER_ID, USER_ID2, USER_ID3, USER_ID4],
    );
    // Apply all bundled SQL migrations (assumes python/src/bursar/sql/ is
    // accessible via relative path — same as store-integration.test.ts).
    const { readdirSync, readFileSync } = await import("fs");
    const { join, dirname } = await import("path");
    const { fileURLToPath } = await import("url");
    const __dirname = dirname(fileURLToPath(import.meta.url));
    const sqlDir = join(__dirname, "../../python/src/bursar/sql");
    const files = readdirSync(sqlDir)
      .filter((f: string) => f.endsWith(".sql"))
      .sort();
    for (const file of files) {
      const sql = readFileSync(join(sqlDir, file), "utf8");
      await pool.query(sql);
    }
    // Clean up any billing/credit data from previous runs (now that tables exist)
    // Order matters: child tables (FKs) before parent tables
    await pool.query("DELETE FROM public.billing_disputes");
    await pool.query("DELETE FROM public.billing_refunds");
    await pool.query("DELETE FROM public.billing_payments");
    await pool.query("DELETE FROM public.billing_invoices");
    await pool.query("DELETE FROM public.billing_events");
    await pool.query("DELETE FROM public.billing_subscriptions");
    await pool.query("DELETE FROM public.billing_provider_refs");
    await pool.query("DELETE FROM public.billing_credit_topups");
    await pool.query("DELETE FROM public.billing_offers");
    await pool.query("DELETE FROM public.billing_customers");
    await pool.query("DELETE FROM public.credit_transactions");
    await pool.query("DELETE FROM public.user_credits");
  }, 60000);

  afterAll(async () => {
    if (pool) await pool.end();
  });

  // ── Sync + Resolve ───────────────────────────────────────────────────

  it("sync_billing_config_roundtrip", async () => {
    const { bs } = await makePgComponents(pool);
    await bs.syncBillingFromConfig(BILLING_CONFIG);
    const offer = await bs.resolveBillingOffer(PROVIDER, null, PRICE_ID);
    expect(offer).not.toBeNull();
    expect(offer!.offerKey).toBe("pro_monthly");
    expect(offer!.plan).toBe("pro");
  });

  it("sync_billing_config_resolve_by_product_id", async () => {
    const { bs } = await makePgComponents(pool);
    await bs.syncBillingFromConfig(BILLING_CONFIG);
    const offer = await bs.resolveBillingOffer(PROVIDER, "prod_monthly");
    expect(offer).not.toBeNull();
    expect(offer!.offerKey).toBe("pro_monthly");
  });

  it("sync_topup_config_roundtrip", async () => {
    const { bs } = await makePgComponents(pool);
    await bs.syncBillingFromConfig(BILLING_CONFIG);
    const topup = await bs.resolveCreditTopup(PROVIDER, null, PRICE_ID_TOPUP);
    expect(topup).not.toBeNull();
    expect(topup!.topupKey).toBe("standard_topup");
    expect(topup!.creditsPerUnit).toBe(1000);
  });

  it("unresolved_offer_returns_null", async () => {
    const { bs } = await makePgComponents(pool);
    await bs.syncBillingFromConfig(BILLING_CONFIG);
    expect(await bs.resolveBillingOffer(PROVIDER, null, "nonexistent")).toBeNull();
  });

  it("resolve_billing_offer_no_match", async () => {
    const { bs } = await makePgComponents(pool);
    await bs.syncBillingFromConfig(BILLING_CONFIG);
    expect(await bs.resolveBillingOffer("nonexistent_provider", null, PRICE_ID)).toBeNull();
  });

  // ── Customer CRUD ────────────────────────────────────────────────────

  it("customer_created_roundtrip", async () => {
    const { bs } = await makePgComponents(pool);
    await bs.upsertBillingCustomer(PROVIDER, CUSTOMER_ID, USER_ID, "test@example.com");
    const uid = await bs.getBillingCustomer(PROVIDER, CUSTOMER_ID);
    expect(uid).toBe(USER_ID);
  });

  it("customer_not_found", async () => {
    const { bs } = await makePgComponents(pool);
    expect(await bs.getBillingCustomer(PROVIDER, "nonexistent_cus")).toBeNull();
  });

  it("customer_updated_replaces_user_id", async () => {
    const { bs } = await makePgComponents(pool);
    await bs.upsertBillingCustomer(PROVIDER, CUSTOMER_ID, USER_ID);
    await bs.upsertBillingCustomer(PROVIDER, CUSTOMER_ID, USER_ID2);
    expect(await bs.getBillingCustomer(PROVIDER, CUSTOMER_ID)).toBe(USER_ID2);
  });

  it("multiple_providers_same_customer_id", async () => {
    const { bs } = await makePgComponents(pool);
    await bs.upsertBillingCustomer("stripe", CUSTOMER_ID, USER_ID);
    await bs.upsertBillingCustomer("dodo", CUSTOMER_ID, USER_ID2);
    expect(await bs.getBillingCustomer("stripe", CUSTOMER_ID)).toBe(USER_ID);
    expect(await bs.getBillingCustomer("dodo", CUSTOMER_ID)).toBe(USER_ID2);
  });

  // ── Subscription CRUD ────────────────────────────────────────────────

  it("subscription_upsert_and_read", async () => {
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

  it("subscription_not_found", async () => {
    const { bs } = await makePgComponents(pool);
    expect(await bs.getBillingSubscription(PROVIDER, "nonexistent_sub")).toBeNull();
  });

  it("subscription_update", async () => {
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

  it("event_idempotency", async () => {
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

  it("event_claim_complete_fail_cycle", async () => {
    const { bs } = await makePgComponents(pool);
    await bs.syncBillingFromConfig(BILLING_CONFIG);
    const c1 = await bs.claimBillingEvent(PROVIDER, "evt_claim_cycle", "test.event");
    expect(c1.status).toBe("claimed");
    await bs.completeBillingEvent(PROVIDER, "evt_claim_cycle");
    const c2 = await bs.claimBillingEvent(PROVIDER, "evt_claim_cycle", "test.event");
    expect(c2.status).toBe("duplicate");
  });

  it("event_fail_then_reclaim", async () => {
    const { bs } = await makePgComponents(pool);
    await bs.syncBillingFromConfig(BILLING_CONFIG);
    const c1 = await bs.claimBillingEvent(PROVIDER, "evt_fail_retry", "test.event");
    expect(c1.status).toBe("claimed");
    await bs.failBillingEvent(PROVIDER, "evt_fail_retry");
    const c2 = await bs.claimBillingEvent(PROVIDER, "evt_fail_retry", "test.event");
    expect(c2.status).toBe("claimed");
  });

  // ── Topup credits ────────────────────────────────────────────────────

  it("compute_topup_credits", async () => {
    const { bs } = await makePgComponents(pool);
    expect(await bs.computeTopupCredits(2000, { creditsPerUnit: 1000 })).toBe(20000);
  });

  it("compute_topup_credits_odd_amount", async () => {
    const { bs } = await makePgComponents(pool);
    expect(await bs.computeTopupCredits(1999, { creditsPerUnit: 1000 })).toBe(19990);
  });

  // ── BillingManager lifecycle ─────────────────────────────────────────

  it("subscription_lifecycle_full", async () => {
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

  it("topup_credit_grant", async () => {
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

  it("subscription_pause_resume", async () => {
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

  it("unknown_event_type_is_ignored", async () => {
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

  it("duplicate_event_skips_side_effects", async () => {
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

  it("provider_scoped_event_id", async () => {
    const { bs } = await makePgComponents(pool);
    await bs.syncBillingFromConfig(BILLING_CONFIG);
    expect((await bs.claimBillingEvent("stripe", "evt_prov_scope", "test.event")).status).toBe(
      "claimed",
    );
    expect((await bs.claimBillingEvent("dodo", "evt_prov_scope", "test.event")).status).toBe(
      "claimed",
    );
  });

  it("sync_offers_adds_new", async () => {
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

  it("cycle_grant_credits_granted", async () => {
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

  it("cycle_grant_replace_prior", async () => {
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
});
