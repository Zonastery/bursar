/**
 * DB-backed commerce integration tests for the JavaScript SDK.
 */

import { afterAll, beforeAll, beforeEach, describe, expect, inject, it, vi } from "vitest";
import pg from "pg";
import { Bursar } from "../src/bursar.js";
import { PostgresBillingStore } from "../src/billing/index.js";
import { CheckoutConflictError } from "../src/commerce/index.js";
import type { CreateCheckoutInput } from "../src/commerce/index.js";
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

const DATABASE_URL = inject("DATABASE_URL");

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
    { providerPaymentId: "auto_pay_failed", status: "failed", amountMinor: 500, currency: "USD" },
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

class ConcurrentCheckoutProvider extends IntegrationProvider {
  private arrivals = 0;
  private release!: () => void;
  private readonly gate = new Promise<void>((resolve) => {
    this.release = resolve;
  });

  override async createCheckoutSession(params: CheckoutParams): Promise<CheckoutSessionResult> {
    this.checkoutParams.push(params);
    this.arrivals += 1;
    if (this.arrivals === 2) this.release();
    await this.gate;
    return {
      url: params.returnUrl,
      providerSessionId: `session_${params.idempotencyKey ?? "checkout"}`,
      customerId: CUSTOMER_ID,
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

  it("binds checkout replays to one operation key before another provider call", async () => {
    const { bursar, provider } = await makeBursar(pool);
    expect(bursar.commerce).not.toBeNull();
    const checkout = (overrides: Partial<CreateCheckoutInput> = {}) =>
      bursar.commerce!.createCheckout({
        subjectId: USER_ID,
        accountId: USER_ID,
        offerKey: "standard_topup",
        quantity: 1,
        returnUrl: "https://app.example/return?intent={intentId}",
        cancelUrl: "https://app.example/cancel?intent={intentId}",
        operationKey: "checkout-operation-replay",
        ...overrides,
      });

    const first = await checkout();
    await expect(checkout()).resolves.toEqual(first);
    expect(provider.checkoutParams).toHaveLength(1);

    await expect(checkout({ accountId: USER_ID2 })).rejects.toBeInstanceOf(CheckoutConflictError);
    await expect(checkout({ quantity: 2 })).rejects.toBeInstanceOf(CheckoutConflictError);
    await expect(checkout({ offerKey: "pro_month", quantity: 1 })).rejects.toBeInstanceOf(
      CheckoutConflictError,
    );
    expect(provider.checkoutParams).toHaveLength(1);

    const independent = await checkout({ operationKey: "checkout-operation-independent" });
    expect(independent.intentId).not.toBe(first.intentId);
    expect(provider.checkoutParams).toHaveLength(2);

    const persisted = await pool.query<{
      operation_key: string;
      request_digest: string;
      count: string;
    }>(
      `SELECT operation_key,
              encode(request_digest, 'hex') AS request_digest,
              count(*)::text AS count
       FROM bursar.billing_checkout_intents
       WHERE subject_id = $1::uuid
       GROUP BY operation_key, request_digest
       ORDER BY operation_key`,
      [USER_ID],
    );
    expect(persisted.rows).toEqual([
      {
        operation_key: "checkout-operation-independent",
        request_digest: "685d7d08850ce95a0ce0a59dacba601f7b1dcaea192d215aa147319a74c628f3",
        count: "1",
      },
      {
        operation_key: "checkout-operation-replay",
        request_digest: "685d7d08850ce95a0ce0a59dacba601f7b1dcaea192d215aa147319a74c628f3",
        count: "1",
      },
    ]);
  });

  it.each([
    {
      failurePoint: "intent" as const,
      operationKey: "checkout-intent-persistence-recovery",
      failureMessage: "injected checkout persistence failure",
    },
    {
      failurePoint: "customer" as const,
      operationKey: "checkout-customer-persistence-recovery",
      failureMessage: "injected customer persistence failure",
    },
  ])(
    "recovers the same open intent after provider success and a transient $failurePoint persistence failure",
    async ({ failurePoint, operationKey, failureMessage }) => {
      const { bursar, billingStore, provider } = await makeBursar(pool);
      expect(bursar.commerce).not.toBeNull();
      const failingWrite =
        failurePoint === "intent"
          ? vi.spyOn(billingStore, "updateCheckoutIntent")
          : vi.spyOn(billingStore, "upsertBillingCustomer");
      failingWrite.mockRejectedValueOnce(new Error(failureMessage));
      const input = {
        subjectId: USER_ID,
        accountId: USER_ID,
        offerKey: "standard_topup",
        returnUrl: "https://app.example/return?intent={intentId}",
        cancelUrl: "https://app.example/cancel?intent={intentId}",
        operationKey,
      } satisfies CreateCheckoutInput;

      await expect(bursar.commerce!.createCheckout(input)).rejects.toThrow(failureMessage);
      expect(provider.checkoutParams).toHaveLength(1);

      const afterFailure = await pool.query<{
        id: string;
        status: string;
        provider_session_id: string | null;
        checkout_url: string | null;
      }>(
        `SELECT id, status, provider_session_id, checkout_url
       FROM bursar.billing_checkout_intents
       WHERE subject_id = $1::uuid
         AND operation_key = $2`,
        [USER_ID, input.operationKey],
      );
      expect(afterFailure.rows).toEqual([
        {
          id: expect.any(String),
          status: "open",
          provider_session_id: null,
          checkout_url: null,
        },
      ]);
      const customerAfterFailure = await pool.query<{ count: string }>(
        `SELECT count(*)::text AS count
       FROM bursar.billing_customers
       WHERE subject_id = $1::uuid`,
        [USER_ID],
      );
      expect(customerAfterFailure.rows).toEqual([{ count: failurePoint === "intent" ? "1" : "0" }]);

      const recovered = await bursar.commerce!.createCheckout(input);
      expect(recovered.intentId).toBe(afterFailure.rows[0]!.id);
      expect(provider.checkoutParams).toHaveLength(2);
      expect(provider.checkoutParams.map((params) => params.idempotencyKey)).toEqual([
        input.operationKey,
        input.operationKey,
      ]);
      expect(new Set(provider.checkoutParams.map((params) => params.returnUrl))).toEqual(
        new Set([recovered.url]),
      );

      await expect(
        billingStore.updateCheckoutIntent(recovered.intentId, { status: "completed" }),
      ).resolves.toBeUndefined();
      const afterRecovery = await pool.query<{
        status: string;
        provider_session_id: string | null;
        checkout_url: string | null;
      }>(
        `SELECT status, provider_session_id, checkout_url
       FROM bursar.billing_checkout_intents
       WHERE id = $1::uuid`,
        [recovered.intentId],
      );
      expect(afterRecovery.rows).toEqual([
        {
          status: "completed",
          provider_session_id: `session_${input.operationKey}`,
          checkout_url: recovered.url,
        },
      ]);
      const customerAfterRecovery = await pool.query<{
        provider_customer_id: string;
        count: string;
      }>(
        `SELECT min(provider_customer_id) AS provider_customer_id,
              count(*)::text AS count
       FROM bursar.billing_customers
       WHERE subject_id = $1::uuid`,
        [USER_ID],
      );
      expect(customerAfterRecovery.rows).toEqual([
        { provider_customer_id: CUSTOMER_ID, count: "1" },
      ]);
    },
  );

  it("converges concurrent same-key checkouts on one persisted provider session", async () => {
    const provider = new ConcurrentCheckoutProvider();
    const { bursar } = await makeBursar(pool, provider);
    expect(bursar.commerce).not.toBeNull();
    const input = {
      subjectId: USER_ID,
      accountId: USER_ID,
      offerKey: "standard_topup",
      returnUrl: "https://app.example/return?intent={intentId}",
      cancelUrl: "https://app.example/cancel?intent={intentId}",
      operationKey: "checkout-concurrent-replay",
    } satisfies CreateCheckoutInput;

    const [first, second] = await Promise.all([
      bursar.commerce!.createCheckout(input),
      bursar.commerce!.createCheckout(input),
    ]);

    expect(second).toEqual(first);
    expect(provider.checkoutParams).toHaveLength(2);
    expect(new Set(provider.checkoutParams.map((params) => params.idempotencyKey))).toEqual(
      new Set([input.operationKey]),
    );
    expect(new Set(provider.checkoutParams.map((params) => params.returnUrl))).toEqual(
      new Set([first.url]),
    );
    const persisted = await pool.query<{
      id: string;
      status: string;
      provider_session_id: string;
      checkout_url: string;
      count: string;
    }>(
      `SELECT min(id::text) AS id,
              min(status) AS status,
              min(provider_session_id) AS provider_session_id,
              min(checkout_url) AS checkout_url,
              count(*)::text AS count
       FROM bursar.billing_checkout_intents
       WHERE subject_id = $1::uuid
         AND operation_key = $2`,
      [USER_ID, input.operationKey],
    );
    expect(persisted.rows).toEqual([
      {
        id: first.intentId,
        status: "open",
        provider_session_id: `session_${input.operationKey}`,
        checkout_url: first.url,
        count: "1",
      },
    ]);
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
