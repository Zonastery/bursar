import { afterAll, beforeAll, beforeEach, describe, expect, inject, it, vi } from "vitest";
import pg from "pg";
import { Bursar } from "../src/bursar.js";
import { PostgresBillingStore } from "../src/billing/index.js";
import { PostgresStore } from "../src/credits/postgres/store.js";
import type { BursarConfigData } from "../src/config.js";
import type {
  CheckoutParams,
  CheckoutSessionResult,
  PaymentProvider,
  PortalParams,
  ProviderUrlResult,
  SavedPaymentChargeQuote,
  SavedPaymentChargeResult,
  WebhookResult,
} from "../src/providers/types.js";
import { TEST_TENANT_ID, applyMigrations, truncateBursarTables } from "./helpers/bootstrap.js";
const DATABASE_URL = inject("DATABASE_URL");
const USER_ID = "00000000-0000-0000-0000-000000000001";
const CUSTOMER_ID = "cus_auto";
const CONFIG = {
  version: 1,
  catalog: { default_plan: "starter" },
  pricing: {
    operations: {
      completion: {
        measures: { tokens: { unit: "token" } },
        dimensions: {},
      },
    },
    rate_cards: {
      standard: {
        operations: {
          completion: {
            rules: [],
            unmatched: {
              action: "charge",
              charge: { type: "per_unit", measure: "tokens", rate: "1" },
            },
          },
        },
      },
    },
  },
  credits: {
    buckets: {
      general: { priority: 10 },
    },
    default_bucket: "general",
    policies: {
      prepaid: { type: "prepaid" },
    },
  },
  entitlements: { features: {} },
  admission: { policies: {} },
  plans: {
    starter: {
      display_name: "Starter",
      rank: 0,
      rate_card: "standard",
      allowed_operations: ["completion"],
      features: {},
      quotas: {},
      credit_policy: "prepaid",
    },
  },
  commerce: {
    providers: { stripe: { type: "stripe" } },
    offers: {
      standard_topup: {
        type: "topup",
        display_name: "Standard top-up",
        credits_per_unit: "100",
        quantity: { minimum: 1, maximum: 5, default: 1 },
        bucket: "general",
        price: { amount_minor: 500, currency: "USD" },
        providers: {
          stripe: { type: "stripe_price", price_id: "price_topup_500" },
        },
      },
    },
    auto_recharge: {
      eligible_topups: ["standard_topup"],
      balance_below: { minimum: "100", maximum: "5000", default: "1000" },
      rearm_above: "6000",
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
} satisfies BursarConfigData;

class IntegrationProvider implements PaymentProvider {
  readonly provider = "stripe";
  readonly checkoutParams: CheckoutParams[] = [];
  readonly charges: SavedPaymentChargeResult[] = [
    {
      providerPaymentId: "auto_pay_processing",
      status: "processing",
      amountMinor: 500,
      currency: "USD",
    },
    {
      providerPaymentId: "auto_pay_action",
      status: "requires_customer_action",
      amountMinor: 500,
      currency: "USD",
      actionUrl: "https://app.example/confirm",
    },
    {
      providerPaymentId: "auto_pay_failed",
      status: "failed",
      amountMinor: 500,
      currency: "USD",
    },
  ];

  async createCheckoutSession(params: CheckoutParams): Promise<CheckoutSessionResult> {
    this.checkoutParams.push(params);
    return {
      url: params.returnUrl,
      providerSessionId: `session_${params.idempotencyKey ?? "checkout"}`,
      customerId: CUSTOMER_ID,
    };
  }

  async createCustomerPortalSession(params: PortalParams): Promise<ProviderUrlResult> {
    return { url: params.returnUrl };
  }

  async listPaymentMethods() {
    return [
      {
        id: "pm_card_visa",
        last4: "4242",
        brand: "visa",
        expiryMonth: 12,
        expiryYear: 2030,
        isDefault: true,
      },
    ];
  }

  async previewSavedPaymentCharge(): Promise<SavedPaymentChargeQuote> {
    return { amountMinor: 500, currency: "USD" };
  }

  async chargeSavedPaymentMethod(): Promise<SavedPaymentChargeResult> {
    return (
      this.charges.shift() ?? {
        providerPaymentId: "auto_pay_success",
        status: "succeeded",
        amountMinor: 500,
        currency: "USD",
      }
    );
  }

  async handleWebhook(): Promise<WebhookResult> {
    return {
      received: true,
      retryable: false,
      provider: this.provider,
      eventId: "unused",
      eventType: "payment.succeeded",
    };
  }
}

async function makeBursar(
  pool: pg.Pool,
  provider = new IntegrationProvider(),
  config: BursarConfigData = CONFIG,
): Promise<{
  bursar: Bursar;
  billingStore: PostgresBillingStore;
  provider: IntegrationProvider;
}> {
  const creditStore = new PostgresStore({
    postgres: pool,
    tenantId: TEST_TENANT_ID,
    providerEnvironment: "test",
  });
  const billingStore = new PostgresBillingStore({
    postgres: pool,
    tenantId: TEST_TENANT_ID,
    providerEnvironment: "test",
  });
  const bursar = new Bursar({
    creditStore,
    billingStore,
    commerceOptions: {
      providerEnvironment: "test",
      providers: {
        stripe: () => provider,
      },
    },
  });
  await bursar.catalog.publishAndActivate(config);
  return { bursar, billingStore, provider };
}

describe.runIf(DATABASE_URL)("auto-recharge catalog transitions", () => {
  let pool: pg.Pool;
  beforeAll(async () => {
    pool = new pg.Pool({ connectionString: DATABASE_URL!, max: 1 });
    await applyMigrations(pool);
  }, 60000);
  beforeEach(async () => {
    await truncateBursarTables(pool);
  });
  afterAll(async () => {
    await pool.end();
  });
  it("reports unavailable for an offer bound only in another environment", async () => {
    const config: BursarConfigData = structuredClone(CONFIG);
    config.commerce!.offers!.standard_topup!.providers!.stripe = {
      type: "stripe_price",
      price_id: { live: "price_live" },
    };
    const { bursar } = await makeBursar(pool, undefined, config);
    expect(await bursar.commerce!.autoRecharge.getStatus({ accountId: USER_ID })).toBeNull();
    await expect(
      bursar.commerce!.autoRecharge.enable({ accountId: USER_ID }),
    ).rejects.toMatchObject({ code: "AUTO_RECHARGE_NOT_CONFIGURED" });
  });
  it("preserves enrollment and failure history when retrying after publication", async () => {
    const provider = new IntegrationProvider();
    provider.charges.splice(0, provider.charges.length, {
      providerPaymentId: "failed",
      status: "failed",
      amountMinor: 500,
      currency: "USD",
    });
    const { bursar, billingStore } = await makeBursar(pool, provider);
    await bursar.ingestBillingEvent({
      provider: "stripe",
      eventId: "customer",
      eventType: "customer.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      customer: { providerCustomerId: CUSTOMER_ID },
    });
    await bursar.commerce!.autoRecharge.enable({ accountId: USER_ID });
    const before = await billingStore.getAutoRechargeProfile(USER_ID);
    const updated = structuredClone(CONFIG);
    updated.plans.starter.display_name = "Starter revised";
    await bursar.catalog.publishAndActivate(updated);
    provider.charges.push({
      providerPaymentId: "retried",
      status: "processing",
      amountMinor: 500,
      currency: "USD",
    });
    await bursar.commerce!.autoRecharge.retry({ accountId: USER_ID });
    const after = await billingStore.getAutoRechargeProfile(USER_ID);
    expect(after?.topupId).toBe(before?.topupId);
    const attempts = await pool.query(
      "SELECT state FROM bursar.billing_auto_recharge_attempts WHERE subject_id=$1 ORDER BY created_at",
      [USER_ID],
    );
    expect(attempts.rows.map((row) => row.state)).toEqual(["failed", "processing"]);
    await bursar.commerce!.autoRecharge.disable({ accountId: USER_ID });
    expect((await billingStore.getAutoRechargeProfile(USER_ID))?.enabled).toBe(false);
  });
  it("reports unavailable when the database declines resume after the precheck", async () => {
    const { bursar, billingStore } = await makeBursar(pool);
    await bursar.ingestBillingEvent({
      provider: "stripe",
      eventId: "customer",
      eventType: "customer.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      customer: { providerCustomerId: CUSTOMER_ID },
    });
    await bursar.commerce!.autoRecharge.enable({ accountId: USER_ID });
    const resume = vi.spyOn(billingStore, "resumeAutoRechargeProfile").mockResolvedValueOnce(false);
    expect(await bursar.commerce!.autoRecharge.retry({ accountId: USER_ID })).toBeNull();
    expect(resume).toHaveBeenCalledOnce();
  });
  it("keeps an in-flight attempt when resuming across publication", async () => {
    const provider = new IntegrationProvider();
    provider.charges.splice(0, provider.charges.length, {
      providerPaymentId: "action",
      status: "requires_customer_action",
      amountMinor: 500,
      currency: "USD",
    });
    const { bursar } = await makeBursar(pool, provider);
    await bursar.ingestBillingEvent({
      provider: "stripe",
      eventId: "customer",
      eventType: "customer.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      customer: { providerCustomerId: CUSTOMER_ID },
    });
    await bursar.commerce!.autoRecharge.enable({ accountId: USER_ID });
    const updated = structuredClone(CONFIG);
    updated.plans.starter.display_name = "Revised";
    await bursar.catalog.publishAndActivate(updated);
    await bursar.commerce!.autoRecharge.retry({ accountId: USER_ID });
    const attempts = await pool.query(
      "SELECT state FROM bursar.billing_auto_recharge_attempts WHERE subject_id=$1",
      [USER_ID],
    );
    expect(attempts.rows).toEqual([{ state: "action_required" }]);
  });
  it("does not resume a paused profile after its offer becomes ineligible", async () => {
    const provider = new IntegrationProvider();
    provider.charges.splice(0, provider.charges.length, {
      providerPaymentId: "action",
      status: "requires_customer_action",
      amountMinor: 500,
      currency: "USD",
    });
    const { bursar, billingStore } = await makeBursar(pool, provider);
    await bursar.ingestBillingEvent({
      provider: "stripe",
      eventId: "customer",
      eventType: "customer.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      customer: { providerCustomerId: CUSTOMER_ID },
    });
    await bursar.commerce!.autoRecharge.enable({ accountId: USER_ID });
    const updated: BursarConfigData = structuredClone(CONFIG);
    updated.commerce!.offers!.standard_topup!.availability = {
      regions: ["IN"],
    };
    await bursar.catalog.publishAndActivate(updated);
    expect(await bursar.commerce!.autoRecharge.retry({ accountId: USER_ID })).toBeNull();
    expect((await billingStore.getAutoRechargeProfile(USER_ID))?.state).toBe("paused");
    const attempts = await pool.query(
      "SELECT count(*)::int AS count FROM bursar.billing_auto_recharge_attempts WHERE subject_id=$1",
      [USER_ID],
    );
    expect(attempts.rows[0].count).toBe(1);
  });
  it("uses the published cooldown and window for fresh claims", async () => {
    const provider = new IntegrationProvider();
    provider.charges.splice(0, provider.charges.length, {
      providerPaymentId: "failed",
      status: "failed",
      amountMinor: 500,
      currency: "USD",
    });
    const initial = structuredClone(CONFIG);
    initial.commerce.auto_recharge.limits.cooldown.count = 3;
    const { bursar } = await makeBursar(pool, provider, initial);
    await bursar.ingestBillingEvent({
      provider: "stripe",
      eventId: "customer",
      eventType: "customer.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      customer: { providerCustomerId: CUSTOMER_ID },
    });
    await bursar.commerce!.autoRecharge.enable({ accountId: USER_ID });
    await pool.query(
      "UPDATE bursar.billing_auto_recharge_profiles SET last_attempt_at=now()-interval '2 hours' WHERE subject_id=$1",
      [USER_ID],
    );
    expect(
      await bursar.commerce!.autoRecharge.processIfNeeded({
        accountId: USER_ID,
      }),
    ).toMatchObject({ outcome: "limit_reached" });
    const updated = structuredClone(initial);
    updated.commerce.auto_recharge.limits.cooldown.count = 1;
    updated.commerce.auto_recharge.limits.window.duration.count = 1;
    await bursar.catalog.publishAndActivate(updated);
    const status = await bursar.commerce!.autoRecharge.getStatus({
      accountId: USER_ID,
    });
    expect(new Date(status!.windowEnd).getTime() - new Date(status!.windowStart).getTime()).toBe(
      86400000,
    );
    provider.charges.push({
      providerPaymentId: "after_cooldown",
      status: "processing",
      amountMinor: 500,
      currency: "USD",
    });
    expect(
      await bursar.commerce!.autoRecharge.processIfNeeded({
        accountId: USER_ID,
      }),
    ).toMatchObject({ outcome: "submitted" });
  });
  it("enforces the newly published limit shown in status", async () => {
    const { bursar, billingStore } = await makeBursar(pool);
    await bursar.ingestBillingEvent({
      provider: "stripe",
      eventId: "customer",
      eventType: "customer.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      customer: { providerCustomerId: CUSTOMER_ID },
    });
    await bursar.commerce!.autoRecharge.enable({ accountId: USER_ID });
    await billingStore.updateAutoRechargeAttemptByProviderPayment({
      provider: "stripe",
      providerPaymentId: "auto_pay_processing",
      state: "succeeded",
    });
    const updated = structuredClone(CONFIG);
    updated.commerce.auto_recharge.limits.max_purchases = 1;
    await bursar.catalog.publishAndActivate(updated);
    expect(
      (await bursar.commerce!.autoRecharge.getStatus({ accountId: USER_ID }))?.maxRecharges,
    ).toBe(1);
    await pool.query(
      "UPDATE bursar.billing_auto_recharge_profiles SET armed=true, last_attempt_at=NULL WHERE subject_id=$1",
      [USER_ID],
    );
    expect(
      await bursar.commerce!.autoRecharge.processIfNeeded({
        accountId: USER_ID,
      }),
    ).toMatchObject({ outcome: "limit_reached" });
    const attempts = await pool.query(
      "SELECT count(*)::int AS count FROM bursar.billing_auto_recharge_attempts WHERE subject_id=$1",
      [USER_ID],
    );
    expect(attempts.rows[0].count).toBe(1);
  });
});
