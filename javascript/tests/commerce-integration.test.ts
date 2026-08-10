/**
 * DB-backed commerce integration tests for the JavaScript SDK.
 */

import { afterAll, beforeAll, beforeEach, describe, expect, inject, it } from "vitest";
import pg from "pg";
import { Bursar } from "../src/bursar.js";
import { PostgresBillingStore } from "../src/billing/index.js";
import type { BursarConfigData } from "../src/config.js";
import { PostgresStore } from "../src/credits/postgres/store.js";
import type {
  ChangePlanParams,
  ChangePlanPreview,
  CheckoutParams,
  CheckoutSessionResult,
  PaymentProvider,
  PortalParams,
  ProviderUrlResult,
  SavedPaymentChargeQuote,
  SavedPaymentChargeResult,
  WebhookRequest,
  WebhookResult,
} from "../src/providers/types.js";
import { TEST_TENANT_ID, applyMigrations, truncateBursarTables } from "./helpers/bootstrap.js";

const DATABASE_URL = process.env.DATABASE_URL ?? inject("DATABASE_URL");

const USER_ID = "00000000-0000-0000-0000-000000000001";
const USER_ID2 = "00000000-0000-0000-0000-000000000002";
const USER_ID3 = "00000000-0000-0000-0000-000000000003";
const CUSTOMER_ID = "cus_commerce_1";
const CUSTOMER_ID2 = "cus_commerce_2";
const CUSTOMER_ID3 = "cus_commerce_3";
const SUBSCRIPTION_ID = "sub_commerce_1";

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
    pro: {
      display_name: "Pro",
      rank: 1,
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
      starter_month: {
        type: "subscription",
        display_name: "Starter monthly",
        plan: "starter",
        billing_interval: { unit: "month", count: 1 },
        price: { amount_minor: 1000, currency: "USD" },
        providers: {
          stripe: { type: "stripe_price", price_id: "price_starter_month" },
        },
      },
      pro_month: {
        type: "subscription",
        display_name: "Pro monthly",
        plan: "pro",
        billing_interval: { unit: "month", count: 1 },
        price: { amount_minor: 2000, currency: "USD" },
        providers: {
          stripe: { type: "stripe_price", price_id: "price_pro_month" },
        },
      },
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
    subscription_changes: {
      upgrade: { effective: "immediate", proration: "prorated", payment_failure: "prevent_change" },
      downgrade: { effective: "renewal", proration: "none", payment_failure: "prevent_change" },
      lateral: { effective: "immediate", proration: "prorated", payment_failure: "prevent_change" },
      cadence_change: {
        effective: "renewal",
        proration: "none",
        payment_failure: "prevent_change",
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
    { providerPaymentId: "auto_pay_failed", status: "failed", amountMinor: 500, currency: "USD" },
  ];

  async createCheckoutSession(params: CheckoutParams): Promise<CheckoutSessionResult> {
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

  async cancelSubscription(): Promise<void> {}

  async reactivateSubscription(): Promise<void> {}

  async cancelScheduledPlanChange(): Promise<void> {}

  async previewChangePlan(): Promise<ChangePlanPreview> {
    return {
      totalAmount: 0,
      settlementAmount: 0,
      currency: "USD",
      lineItems: [],
      effectiveAt: "2026-08-01T00:00:00.000Z",
      nextBillingDate: "2026-09-01T00:00:00.000Z",
    };
  }

  async changePlan(_params: ChangePlanParams): Promise<{ providerOperationId: string }> {
    return { providerOperationId: "change_commerce_1" };
  }

  async handleWebhook(_req: WebhookRequest): Promise<WebhookResult> {
    return {
      received: true,
      retryable: false,
      provider: this.provider,
      eventId: "evt_webhook_1",
      eventType: "payment.succeeded",
    };
  }
}

async function makeBursar(
  pool: pg.Pool,
  provider = new IntegrationProvider(),
): Promise<{ bursar: Bursar; billingStore: PostgresBillingStore; provider: IntegrationProvider }> {
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
  await bursar.catalog.publishAndActivate(CONFIG);
  return { bursar, billingStore, provider };
}

describe.runIf(DATABASE_URL)("Commerce integration", () => {
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
    await pool?.end();
  });

  it("persists checkout intent and grants top-up credits from a paid provider event", async () => {
    const { bursar } = await makeBursar(pool);
    expect(bursar.commerce).not.toBeNull();

    const checkout = await bursar.commerce!.createCheckout({
      subjectId: USER_ID,
      accountId: USER_ID,
      offerKey: "standard_topup",
      type: "credit_pack",
      returnUrl: "https://app.example/return?intent={intentId}",
      cancelUrl: "https://app.example/cancel?intent={intentId}",
      operationKey: "checkout-topup-1",
    });

    expect(checkout.provider).toBe("stripe");
    expect(checkout.url).toContain(checkout.intentId);
    await expect(
      bursar.commerce!.getCheckoutStatus({ intentId: checkout.intentId, subjectId: USER_ID }),
    ).resolves.toMatchObject({ status: "pending" });

    const result = await bursar.ingestBillingEvent({
      provider: "stripe",
      eventId: "evt_commerce_topup_paid",
      eventType: "payment.succeeded",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      payment: {
        providerPaymentId: "pay_commerce_topup_1",
        amountMinor: 500,
        taxMinor: 0,
        currency: "USD",
        refs: { priceId: "price_topup_500" },
        purpose: "credit_topup",
        status: "succeeded",
      },
    });

    expect(result.handled).toBe(true);
    expect((await bursar.credits.getBalance(USER_ID)).balance.toString()).toBe("100");
    const overview = await bursar.commerce!.getAccountOverview(USER_ID);
    expect(overview.credits.ledgerBalance.toString()).toBe("100");
    expect(overview.transactions[0]?.entryType).toBe("purchase");
  });

  it("manages active subscription portal, scheduled plan change, and cancellation", async () => {
    const { bursar } = await makeBursar(pool);
    expect(bursar.commerce).not.toBeNull();

    await bursar.ingestBillingEvent({
      provider: "stripe",
      eventId: "evt_commerce_customer_created",
      eventType: "customer.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      customer: { providerCustomerId: CUSTOMER_ID, email: "buyer@example.com" },
    });
    await bursar.ingestBillingEvent({
      provider: "stripe",
      eventId: "evt_commerce_subscription_active",
      eventType: "subscription.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      customer: { providerCustomerId: CUSTOMER_ID },
      subscription: {
        providerSubscriptionId: SUBSCRIPTION_ID,
        status: "active",
        refs: { priceId: "price_pro_month" },
        interval: "month",
        intervalCount: 1,
      },
    });

    expect((await bursar.credits.getUserPlan(USER_ID)).planKey).toBe("pro");
    await expect(
      bursar.commerce!.createPortalSession({
        accountId: USER_ID,
        returnUrl: "https://app.example/billing",
      }),
    ).resolves.toEqual({ url: "https://app.example/billing" });

    const preview = await bursar.commerce!.previewPlanChange({
      accountId: USER_ID,
      offerKey: "starter_month",
    });
    if (preview.unchanged) throw new Error("Expected a plan-change quote");
    expect(preview.scheduled).toBe(true);
    expect(preview.quoteFingerprint).toBeTruthy();

    const confirmed = await bursar.commerce!.confirmPlanChange({
      accountId: USER_ID,
      operationKey: "downgrade-1",
      offerKey: "starter_month",
      quoteFingerprint: preview.quoteFingerprint,
    });
    expect(confirmed.scheduled).toBe(true);
    expect(confirmed.effectiveAt).toBe("2026-09-01T00:00:00.000Z");

    await expect(
      bursar.commerce!.cancelScheduledPlanChange({
        accountId: USER_ID,
        operationKey: "cancel-downgrade",
      }),
    ).resolves.toEqual({ success: true });

    const canceled = await bursar.commerce!.cancelSubscription({
      accountId: USER_ID,
      operationKey: "cancel-subscription",
    });
    expect(canceled.pending).toBe(true);
  });

  it("processes auto-recharge attempts through saved payment methods", async () => {
    const provider = new IntegrationProvider();
    const { bursar, billingStore } = await makeBursar(pool, provider);
    expect(bursar.commerce).not.toBeNull();

    await bursar.ingestBillingEvent({
      provider: "stripe",
      eventId: "evt_auto_customer",
      eventType: "customer.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      customer: { providerCustomerId: CUSTOMER_ID, email: "buyer@example.com" },
    });

    const enabled = await bursar.commerce!.autoRecharge.enable({
      accountId: USER_ID,
      returnUrl: "https://app.example/auto-recharge",
    });
    expect(enabled?.enabled).toBe(true);
    expect(enabled?.paymentMethodLast4).toBe("4242");
    expect(enabled?.quoteAmountMinor).toBe(500);
    expect(await billingStore.countAutoRechargeAttempts(USER_ID, "2000-01-01T00:00:00.000Z")).toBe(
      1,
    );

    const blockedByCooldown = await bursar.commerce!.autoRecharge.processIfNeeded({
      accountId: USER_ID,
      returnUrl: "https://app.example/auto-recharge/action",
    });
    expect(blockedByCooldown.outcome).toBe("limit_reached");

    await bursar.ingestBillingEvent({
      provider: "stripe",
      eventId: "evt_auto_customer_2",
      eventType: "customer.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID2,
      customer: { providerCustomerId: CUSTOMER_ID2, email: "buyer2@example.com" },
    });
    provider.charges.splice(0, provider.charges.length, {
      providerPaymentId: "auto_pay_action",
      status: "requires_customer_action",
      amountMinor: 500,
      currency: "USD",
      actionUrl: "https://app.example/confirm",
    });
    const actionRequired = await bursar.commerce!.autoRecharge.enable({
      accountId: USER_ID2,
      returnUrl: "https://app.example/auto-recharge/action",
    });
    expect(actionRequired?.state).toBe("paused");
    expect(actionRequired?.suspendedReason).toBe("auto_recharge_paused");
    expect((await billingStore.getAutoRechargeProfile(USER_ID2))?.state).toBe("paused");

    await billingStore.updateAutoRechargeAttemptByProviderPayment({
      provider: "stripe",
      providerPaymentId: "auto_pay_action",
      state: "succeeded",
    });
    const completedAction = await pool.query(
      "SELECT state FROM bursar.billing_auto_recharge_attempts WHERE subject_id = $1",
      [USER_ID2],
    );
    expect(completedAction.rows[0]?.state).toBe("succeeded");
    expect((await billingStore.getAutoRechargeProfile(USER_ID2))?.state).toBe("active");

    await bursar.ingestBillingEvent({
      provider: "stripe",
      eventId: "evt_auto_customer_3",
      eventType: "customer.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID3,
      customer: { providerCustomerId: CUSTOMER_ID3, email: "buyer3@example.com" },
    });
    provider.charges.splice(0, provider.charges.length, {
      providerPaymentId: "auto_pay_failed",
      status: "failed",
      amountMinor: 500,
      currency: "USD",
    });
    const failed = await bursar.commerce!.autoRecharge.enable({
      accountId: USER_ID3,
      returnUrl: "https://app.example/auto-recharge/failed",
    });
    expect(failed?.state).toBe("active");
    expect(await billingStore.getAutoRechargeProfile(USER_ID3)).toMatchObject({
      state: "active",
      armed: true,
    });
    const failedAttempt = await pool.query(
      "SELECT state, failure_code FROM bursar.billing_auto_recharge_attempts WHERE subject_id = $1",
      [USER_ID3],
    );
    expect(failedAttempt.rows[0]).toMatchObject({
      state: "failed",
      failure_code: "payment_failed",
    });

    provider.charges.splice(0, provider.charges.length, {
      providerPaymentId: "auto_pay_retry",
      status: "processing",
      amountMinor: 500,
      currency: "USD",
    });
    await bursar.commerce!.autoRecharge.enable({
      accountId: USER_ID3,
      returnUrl: "https://app.example/auto-recharge/enable-again",
    });
    const cooldownAttempts = await pool.query(
      "SELECT state FROM bursar.billing_auto_recharge_attempts WHERE subject_id = $1",
      [USER_ID3],
    );
    expect(cooldownAttempts.rows).toEqual([{ state: "failed" }]);

    const retried = await bursar.commerce!.autoRecharge.retry({
      accountId: USER_ID3,
      returnUrl: "https://app.example/auto-recharge/retry",
    });
    expect(retried?.state).toBe("active");
    const retryAttempts = await pool.query(
      "SELECT state FROM bursar.billing_auto_recharge_attempts WHERE subject_id = $1",
      [USER_ID3],
    );
    expect(retryAttempts.rows).toHaveLength(2);
    expect(retryAttempts.rows).toEqual(
      expect.arrayContaining([{ state: "failed" }, { state: "processing" }]),
    );

    await bursar.commerce!.autoRecharge.disable({ accountId: USER_ID });
    expect((await billingStore.getAutoRechargeProfile(USER_ID))?.enabled).toBe(false);
    await expect(
      bursar.commerce!.autoRecharge.processIfNeeded({
        accountId: USER_ID,
        returnUrl: "https://app.example/auto-recharge",
      }),
    ).resolves.toMatchObject({ outcome: "disabled" });
  });
});
