/**
 * DB-backed commerce integration tests for the JavaScript SDK.
 */

import { afterAll, beforeAll, beforeEach, describe, expect, inject, it, vi } from "vitest";
import { Decimal } from "decimal.js";
import pg from "pg";
import { Bursar } from "../src/bursar.js";
import { PostgresBillingStore } from "../src/billing/index.js";
import {
  CheckoutCompletedError,
  CheckoutConflictError,
  CommerceResourceNotFoundError,
} from "../src/commerce/index.js";
import { ProviderResponseError } from "../src/errors.js";
import type { CreateCheckoutInput } from "../src/commerce/index.js";
import type { BursarConfigData } from "../src/config.js";
import { PostgresStore } from "../src/credits/postgres/store.js";
import type {
  ChangePlanParams,
  ChangePlanPreview,
  CheckoutParams,
  CheckoutSessionResult,
  CheckoutSessionStatus,
  PaymentProvider,
  PreviewChangePlanParams,
  PortalParams,
  ProviderUrlResult,
  SavedPaymentChargeQuote,
  SavedPaymentChargeResult,
  WebhookRequest,
  WebhookResult,
} from "../src/providers/types.js";
import { TEST_TENANT_ID, applyMigrations, truncateBursarTables } from "./helpers/bootstrap.js";
import { mapDodoEvent } from "./helpers/dodo-fixtures.js";

const DATABASE_URL = inject("DATABASE_URL");

const USER_ID = "00000000-0000-0000-0000-000000000001";
const USER_ID2 = "00000000-0000-0000-0000-000000000002";
const USER_ID3 = "00000000-0000-0000-0000-000000000003";
const CUSTOMER_ID = "cus_commerce_1";
const CUSTOMER_ID2 = "cus_commerce_2";
const CUSTOMER_ID3 = "cus_commerce_3";
const SUBSCRIPTION_ID = "sub_commerce_1";
const SUBSCRIPTION_ID2 = "sub_commerce_2";

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
      tier: 1,
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
      upgrade: {
        effective: "immediate",
        proration: "prorated",
        payment_failure: "prevent_change",
      },
      downgrade: {
        effective: "renewal",
        proration: "none",
        payment_failure: "prevent_change",
      },
      lateral: {
        effective: "immediate",
        proration: "prorated",
        payment_failure: "prevent_change",
      },
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
  readonly provider: string = "stripe";
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

  async cancelSubscription(): Promise<void> {}

  async reactivateSubscription(): Promise<void> {}

  async cancelScheduledPlanChange(): Promise<void> {}

  async previewChangePlan(_params?: PreviewChangePlanParams): Promise<ChangePlanPreview> {
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

class CommerceLifecycleProvider extends IntegrationProvider {
  checkoutStatus: CheckoutSessionStatus | null = null;
  previewMode: "valid" | "malformed" | "missing_next_billing_date" = "valid";
  failListPaymentMethods = false;
  failCancelSubscription = false;
  failChangePlan = false;
  returnNullInvoiceUrl = false;
  readonly canceled: string[] = [];
  readonly reactivated: string[] = [];

  async getCheckoutSessionStatus(): Promise<CheckoutSessionStatus | null> {
    return this.checkoutStatus;
  }

  override async previewChangePlan(params?: PreviewChangePlanParams): Promise<ChangePlanPreview> {
    const preview = await super.previewChangePlan(params);
    if (this.previewMode === "malformed") {
      return { ...preview, totalAmount: Number.NaN };
    }
    if (this.previewMode === "missing_next_billing_date") {
      const { nextBillingDate: _nextBillingDate, ...withoutNextBillingDate } = preview;
      return withoutNextBillingDate;
    }
    return preview;
  }

  async createUpdatePaymentMethodSession(params: {
    customerId: string;
    subscriptionId: string;
    returnUrl: string;
  }): Promise<ProviderUrlResult> {
    return { url: `${params.returnUrl}?customer=${params.customerId}` };
  }

  async createPaymentMethodSetupSession(params: {
    customerId: string;
    returnUrl: string;
    cancelUrl?: string;
  }): Promise<ProviderUrlResult> {
    return { url: `${params.returnUrl}?customer=${params.customerId}` };
  }

  async getInvoiceUrl(providerPaymentId: string): Promise<ProviderUrlResult | null> {
    if (this.returnNullInvoiceUrl) return null;
    return { url: `https://app.example/invoices/${providerPaymentId}` };
  }

  async listPaymentMethods(_customerId?: string) {
    if (this.failListPaymentMethods) throw new Error("provider payment methods unavailable");
    return super.listPaymentMethods();
  }

  async cancelSubscription(subscriptionId?: string): Promise<void> {
    if (this.failCancelSubscription) throw new Error("provider cancellation unavailable");
    if (subscriptionId) this.canceled.push(subscriptionId);
  }

  async reactivateSubscription(subscriptionId?: string): Promise<void> {
    if (subscriptionId) this.reactivated.push(subscriptionId);
  }

  override async changePlan(params: ChangePlanParams): Promise<{ providerOperationId: string }> {
    if (this.failChangePlan) throw new Error("provider plan change failed");
    return super.changePlan(params);
  }
}

async function makeBursar(
  pool: pg.Pool,
  provider = new IntegrationProvider(),
  config: typeof CONFIG = CONFIG,
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

async function seedSubscription(
  bursar: Bursar,
  options: {
    accountId?: string;
    customerId?: string;
    subscriptionId?: string;
    priceId?: string;
    cancelAtPeriodEnd?: boolean;
  } = {},
): Promise<void> {
  const accountId = options.accountId ?? USER_ID;
  const customerId = options.customerId ?? CUSTOMER_ID;
  const subscriptionId = options.subscriptionId ?? SUBSCRIPTION_ID;
  await bursar.ingestBillingEvent({
    provider: "stripe",
    eventId: `evt_seed_customer_${accountId}`,
    eventType: "customer.created",
    occurredAt: new Date().toISOString(),
    accountId,
    customer: { providerCustomerId: customerId },
  });
  await bursar.ingestBillingEvent({
    provider: "stripe",
    eventId: `evt_seed_subscription_${subscriptionId}`,
    eventType: "subscription.created",
    occurredAt: new Date().toISOString(),
    accountId,
    customer: { providerCustomerId: customerId },
    subscription: {
      providerSubscriptionId: subscriptionId,
      status: "active",
      refs: { priceId: options.priceId ?? "price_pro_month" },
      interval: "month",
      intervalCount: 1,
      cancelAtPeriodEnd: options.cancelAtPeriodEnd ?? false,
    },
  });
}

function removeProviderCapability(provider: IntegrationProvider, capability: string): void {
  Object.defineProperty(provider, capability, {
    configurable: true,
    value: undefined,
    writable: true,
  });
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

  it("enforces region in the database and replays a matching checkout", async () => {
    const config = structuredClone(CONFIG);
    Object.assign(config.commerce.offers.standard_topup, {
      availability: { regions: ["US-CA"] },
    });
    const { bursar, provider } = await makeBursar(pool, undefined, config);
    const input = {
      subjectId: USER_ID,
      accountId: USER_ID,
      offerKey: "standard_topup",
      returnUrl: "https://return.test",
      cancelUrl: "https://cancel.test",
      operationKey: "restricted-region",
      region: "US-CA",
    };
    await expect(bursar.commerce!.createCheckout({ ...input, region: "US-NY" })).rejects.toThrow();
    const first = await bursar.commerce!.createCheckout(input);
    await expect(bursar.commerce!.createCheckout(input)).resolves.toEqual(first);
    expect(provider.checkoutParams).toHaveLength(1);
  });

  it("applies the catalog terminal plan on subscription cancellation", async () => {
    const config = structuredClone(CONFIG);
    Object.assign(config.catalog, { subscription_end_plan: "starter" });
    const { bursar } = await makeBursar(pool, undefined, config);
    await seedSubscription(bursar);
    expect((await bursar.credits.getUserPlan(USER_ID)).planKey).toBe("pro");
    const event = {
      provider: "stripe",
      eventId: "evt_terminal_catalog",
      eventType: "subscription.canceled" as const,
      occurredAt: new Date(Date.now() + 1000).toISOString(),
      accountId: USER_ID,
      subscription: {
        providerSubscriptionId: SUBSCRIPTION_ID,
        status: "canceled" as const,
      },
    };
    await bursar.ingestBillingEvent(event);
    expect((await bursar.credits.getUserPlan(USER_ID)).planKey).toBe("starter");
    await bursar.ingestBillingEvent(event);
    expect((await bursar.credits.getUserPlan(USER_ID)).planKey).toBe("starter");
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
      bursar.commerce!.getCheckoutStatus({
        intentId: checkout.intentId,
        subjectId: USER_ID,
      }),
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

  it("completes provider-confirmed checkout and preserves subscription checkout metadata", async () => {
    const provider = new CommerceLifecycleProvider();
    const { bursar, billingStore } = await makeBursar(pool, provider);
    const input = {
      subjectId: USER_ID,
      accountId: USER_ID,
      offerKey: "standard_topup",
      returnUrl: "https://app.example/return?intent={intentId}",
      cancelUrl: "https://app.example/cancel?intent={intentId}",
      operationKey: "checkout-provider-succeeded",
    } satisfies CreateCheckoutInput;
    const checkout = await bursar.commerce!.createCheckout(input);

    provider.checkoutStatus = { paymentStatus: "succeeded" };
    await expect(bursar.commerce!.createCheckout(input)).rejects.toBeInstanceOf(
      CheckoutCompletedError,
    );
    await expect(billingStore.getCheckoutIntent(checkout.intentId, USER_ID)).resolves.toMatchObject(
      { status: "completed" },
    );

    const subscriptionCheckout = await bursar.commerce!.createCheckout({
      subjectId: USER_ID,
      accountId: USER_ID,
      offerKey: "pro_month",
      type: "subscription",
      email: "buyer@example.com",
      returnUrl: "https://app.example/return?intent={intentId}",
      cancelUrl: "https://app.example/cancel?intent={intentId}",
      operationKey: "checkout-subscription-email",
    });
    expect(subscriptionCheckout.provider).toBe("stripe");
    expect(provider.checkoutParams.at(-1)).toMatchObject({
      type: "subscription",
      email: "buyer@example.com",
      metadata: expect.objectContaining({
        plan_slug: "pro",
        billing_interval: "month",
      }),
    });
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

    const independent = await checkout({
      operationKey: "checkout-operation-independent",
    });
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
        `bursar:checkout:${recovered.intentId}`,
        `bursar:checkout:${recovered.intentId}`,
      ]);
      expect(new Set(provider.checkoutParams.map((params) => params.returnUrl))).toEqual(
        new Set([recovered.url]),
      );

      await expect(
        billingStore.updateCheckoutIntent(recovered.intentId, {
          status: "completed",
        }),
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
          provider_session_id: `session_bursar:checkout:${recovered.intentId}`,
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
      new Set([`bursar:checkout:${first.intentId}`]),
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
        provider_session_id: `session_bursar:checkout:${first.intentId}`,
        checkout_url: first.url,
        count: "1",
      },
    ]);
  });

  it("keeps persisted commerce state safe across terminal checkout and account-document flows", async () => {
    const provider = new CommerceLifecycleProvider();
    const { bursar, billingStore } = await makeBursar(pool, provider);
    expect(bursar.commerce).not.toBeNull();

    const checkoutInput = (operationKey: string) => ({
      subjectId: USER_ID,
      accountId: USER_ID,
      offerKey: "standard_topup",
      returnUrl: "https://app.example/return?intent={intentId}",
      cancelUrl: "https://app.example/cancel?intent={intentId}",
      operationKey,
    });
    const completed = await bursar.commerce!.createCheckout(checkoutInput("checkout-completed"));
    await billingStore.updateCheckoutIntent(completed.intentId, {
      status: "completed",
    });
    await expect(
      bursar.commerce!.createCheckout(checkoutInput("checkout-completed")),
    ).rejects.toBeInstanceOf(CheckoutCompletedError);

    const terminal = await bursar.commerce!.createCheckout(checkoutInput("checkout-terminal"));
    await billingStore.updateCheckoutIntent(terminal.intentId, {
      status: "failed",
    });
    await expect(
      bursar.commerce!.createCheckout(checkoutInput("checkout-terminal")),
    ).rejects.toBeInstanceOf(CheckoutConflictError);

    const expired = await bursar.commerce!.createCheckout(checkoutInput("checkout-expired"));
    await pool.query(
      `UPDATE bursar.billing_checkout_intents
          SET created_at = now() - interval '2 minutes',
              expires_at = now() - interval '1 second'
        WHERE id = $1::uuid`,
      [expired.intentId],
    );
    await expect(
      bursar.commerce!.createCheckout(checkoutInput("checkout-expired")),
    ).rejects.toBeInstanceOf(CheckoutConflictError);
    await expect(
      bursar.commerce!.getCheckoutStatus({
        intentId: "00000000-0000-0000-0000-000000000099",
        subjectId: USER_ID,
      }),
    ).rejects.toBeInstanceOf(CommerceResourceNotFoundError);

    await bursar.ingestBillingEvent({
      provider: "stripe",
      eventId: "evt_lifecycle_customer",
      eventType: "customer.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      customer: { providerCustomerId: CUSTOMER_ID },
    });
    await bursar.ingestBillingEvent({
      provider: "stripe",
      eventId: "evt_lifecycle_subscription",
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

    await expect(
      bursar.commerce!.createPortalSession({
        accountId: USER_ID,
        purpose: "payment-method",
        returnUrl: "https://app.example/payment-method",
      }),
    ).resolves.toEqual({
      url: `https://app.example/payment-method?customer=${CUSTOMER_ID}`,
    });
    await bursar.ingestBillingEvent({
      provider: "stripe",
      eventId: "evt_lifecycle_cancel",
      eventType: "subscription.cancellation_scheduled",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      subscription: { providerSubscriptionId: SUBSCRIPTION_ID },
    });
    await expect(
      bursar.commerce!.reactivateSubscription({
        accountId: USER_ID,
        operationKey: "reactivate-1",
      }),
    ).resolves.toMatchObject({ ok: true, pending: true });
    expect(provider.reactivated).toEqual([SUBSCRIPTION_ID]);

    await billingStore.upsertBillingInvoice({
      provider: "stripe",
      providerInvoiceId: "in_lifecycle",
      providerSubscriptionId: SUBSCRIPTION_ID,
      userId: USER_ID,
      status: "paid",
      amountPaidMinor: 2_000,
      amountDueMinor: 2_000,
      currency: "USD",
      periodStart: "2026-08-01T00:00:00Z",
      periodEnd: "2026-09-01T00:00:00Z",
      providerUpdatedAt: new Date().toISOString(),
    });
    await expect(
      bursar.commerce!.getInvoiceLink({
        accountId: USER_ID,
        document: {
          kind: "provider_invoice",
          provider: "stripe",
          providerDocumentId: "in_lifecycle",
        },
      }),
    ).resolves.toEqual({ url: "https://app.example/invoices/in_lifecycle" });
    await expect(
      bursar.commerce!.handleWebhook({
        provider: "stripe",
        rawBody: "{}",
        headers: {},
      }),
    ).resolves.toMatchObject({
      received: true,
      eventType: "payment.succeeded",
    });
  });

  it("keeps plan changes and account overview resilient across provider edge states", async () => {
    const provider = new CommerceLifecycleProvider();
    const { bursar } = await makeBursar(pool, provider);
    expect(bursar.commerce).not.toBeNull();

    await bursar.ingestBillingEvent({
      provider: "stripe",
      eventId: "evt_edge_customer_1",
      eventType: "customer.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      customer: { providerCustomerId: CUSTOMER_ID },
    });
    await bursar.ingestBillingEvent({
      provider: "stripe",
      eventId: "evt_edge_subscription_1",
      eventType: "subscription.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      customer: { providerCustomerId: CUSTOMER_ID },
      subscription: {
        providerSubscriptionId: SUBSCRIPTION_ID,
        status: "active",
        refs: { priceId: "price_pro_month" },
      },
    });
    const preview = await bursar.commerce!.previewPlanChange({
      accountId: USER_ID,
      offerKey: "starter_month",
    });
    if (preview.unchanged) throw new Error("Expected a scheduled plan-change quote");
    await expect(
      bursar.commerce!.confirmPlanChange({
        accountId: USER_ID,
        operationKey: "edge-downgrade",
        offerKey: "starter_month",
        quoteFingerprint: "stale-quote",
      }),
    ).rejects.toThrow("financial preview changed");

    await expect(
      bursar.commerce!.cancelAllSubscriptions({
        accountId: USER_ID,
        operationKey: "edge-cancel-all",
      }),
    ).resolves.toMatchObject({ accountId: USER_ID, canceledCount: 1 });
    expect(provider.canceled).toEqual([SUBSCRIPTION_ID]);

    await bursar.credits.addCredits(USER_ID, new Decimal(1), {
      type: "purchase",
      metadata: { provider: "stripe", provider_document_id: "in_ledger" },
      idempotencyKey: "edge-ledger-document",
    });
    const ledger = (await bursar.credits.listLedgerEntries(USER_ID, { limit: 10 })).items.find(
      (entry) => entry.metadata?.provider_document_id === "in_ledger",
    );
    expect(ledger).toBeDefined();
    await expect(
      bursar.commerce!.getInvoiceLink({
        accountId: USER_ID,
        document: { kind: "ledger_entry", ledgerEntryId: ledger!.entryId },
      }),
    ).resolves.toEqual({ url: "https://app.example/invoices/in_ledger" });

    provider.failListPaymentMethods = true;
    vi.spyOn(bursar.billing!.autoRecharge, "getStatus").mockRejectedValue(
      new Error("auto-recharge unavailable"),
    );
    const overview = await bursar.commerce!.getAccountOverview(USER_ID);
    expect(overview.availability).toMatchObject({
      paymentMethods: false,
      autoRecharge: false,
    });
  });

  it("surfaces provider preview and lifecycle capability failures without mutating state", async () => {
    const provider = new CommerceLifecycleProvider();
    const { bursar } = await makeBursar(pool, provider);
    await seedSubscription(bursar);

    provider.previewMode = "malformed";
    await expect(
      bursar.commerce!.previewPlanChange({
        accountId: USER_ID,
        offerKey: "starter_month",
      }),
    ).rejects.toBeInstanceOf(ProviderResponseError);

    provider.previewMode = "missing_next_billing_date";
    await expect(
      bursar.commerce!.previewPlanChange({
        accountId: USER_ID,
        offerKey: "starter_month",
      }),
    ).rejects.toMatchObject({
      name: "ProviderResponseError",
      operation: "previewChangePlan",
      details: { field: "nextBillingDate" },
    });

    const originalPreviewChangePlan = provider.previewChangePlan;
    const originalChangePlan = provider.changePlan;
    const originalReactivateSubscription = provider.reactivateSubscription;
    const originalCancelSubscription = provider.cancelSubscription;
    provider.previewMode = "valid";
    removeProviderCapability(provider, "previewChangePlan");
    await expect(
      bursar.commerce!.previewPlanChange({
        accountId: USER_ID,
        offerKey: "starter_month",
      }),
    ).rejects.toMatchObject({ name: "ProviderCapabilityNotSupportedError" });
    provider.previewChangePlan = originalPreviewChangePlan;

    const missingChangePlanPreview = await bursar.commerce!.previewPlanChange({
      accountId: USER_ID,
      offerKey: "starter_month",
    });
    if (missingChangePlanPreview.unchanged) throw new Error("Expected a plan-change quote");
    removeProviderCapability(provider, "changePlan");
    await expect(
      bursar.commerce!.confirmPlanChange({
        accountId: USER_ID,
        operationKey: "missing-change-plan",
        offerKey: "starter_month",
        quoteFingerprint: missingChangePlanPreview.quoteFingerprint,
      }),
    ).rejects.toMatchObject({ name: "ProviderCapabilityNotSupportedError" });
    provider.changePlan = originalChangePlan;

    removeProviderCapability(provider, "cancelSubscription");
    await expect(
      bursar.commerce!.cancelSubscription({
        accountId: USER_ID,
        operationKey: "missing-cancel",
      }),
    ).rejects.toMatchObject({ name: "ProviderCapabilityNotSupportedError" });
    await bursar.ingestBillingEvent({
      provider: "stripe",
      eventId: "evt_seed_subscription_cancelable",
      eventType: "subscription.updated",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      customer: { providerCustomerId: CUSTOMER_ID },
      subscription: {
        providerSubscriptionId: SUBSCRIPTION_ID,
        status: "active",
        refs: { priceId: "price_pro_month" },
        cancelAtPeriodEnd: true,
      },
    });
    removeProviderCapability(provider, "reactivateSubscription");
    const missingReactivatePreview = await bursar.commerce!.previewPlanChange({
      accountId: USER_ID,
      offerKey: "starter_month",
    });
    if (missingReactivatePreview.unchanged) throw new Error("Expected a plan-change quote");
    await expect(
      bursar.commerce!.confirmPlanChange({
        accountId: USER_ID,
        operationKey: "missing-reactivate-plan-change",
        offerKey: "starter_month",
        quoteFingerprint: missingReactivatePreview.quoteFingerprint,
      }),
    ).rejects.toMatchObject({ name: "ProviderCapabilityNotSupportedError" });
    provider.reactivateSubscription = originalReactivateSubscription;
    const missingCancelPreview = await bursar.commerce!.previewPlanChange({
      accountId: USER_ID,
      offerKey: "starter_month",
    });
    if (missingCancelPreview.unchanged) throw new Error("Expected a plan-change quote");
    removeProviderCapability(provider, "cancelSubscription");
    await expect(
      bursar.commerce!.confirmPlanChange({
        accountId: USER_ID,
        operationKey: "missing-cancel-plan-change",
        offerKey: "starter_month",
        quoteFingerprint: missingCancelPreview.quoteFingerprint,
      }),
    ).rejects.toMatchObject({ name: "ProviderCapabilityNotSupportedError" });
    provider.cancelSubscription = originalCancelSubscription;
    removeProviderCapability(provider, "reactivateSubscription");
    await expect(
      bursar.commerce!.reactivateSubscription({
        accountId: USER_ID,
        operationKey: "missing-reactivate",
      }),
    ).rejects.toMatchObject({ name: "ProviderCapabilityNotSupportedError" });
    await bursar.ingestBillingEvent({
      provider: "stripe",
      eventId: "evt_seed_subscription_canceled",
      eventType: "subscription.updated",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      customer: { providerCustomerId: CUSTOMER_ID },
      subscription: {
        providerSubscriptionId: SUBSCRIPTION_ID,
        status: "canceled",
      },
    });
    await expect(
      bursar.commerce!.cancelSubscription({
        accountId: USER_ID,
        operationKey: "cancel-terminal",
      }),
    ).resolves.toEqual({ ok: true });

    await bursar.ingestBillingEvent({
      provider: "stripe",
      eventId: "evt_incomplete_customer",
      eventType: "customer.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID3,
      customer: { providerCustomerId: CUSTOMER_ID3 },
    });
    await bursar.ingestBillingEvent({
      provider: "stripe",
      eventId: "evt_incomplete_subscription",
      eventType: "subscription.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID3,
      customer: { providerCustomerId: CUSTOMER_ID3 },
      subscription: {
        providerSubscriptionId: "sub_incomplete",
        status: "active",
        interval: "month",
        intervalCount: 1,
      },
    });
    await expect(
      bursar.commerce!.previewPlanChange({
        accountId: USER_ID3,
        offerKey: "starter_month",
      }),
    ).rejects.toMatchObject({ name: "CommerceResourceNotFoundError" });
    const catalogDocument = vi.spyOn(bursar.billing!, "getActiveCatalogDocument");
    catalogDocument.mockRejectedValueOnce(new Error("catalog database unavailable"));
    await expect(bursar.commerce!.getAccountOverview(USER_ID)).rejects.toMatchObject({
      name: "CoreBillingDataUnavailableError",
    });
    catalogDocument.mockResolvedValueOnce(null);
    await expect(bursar.commerce!.getAccountOverview(USER_ID)).rejects.toMatchObject({
      name: "CoreBillingDataUnavailableError",
    });
    catalogDocument.mockResolvedValueOnce(JSON.parse("{}"));
    await expect(bursar.commerce!.getAccountOverview(USER_ID)).rejects.toMatchObject({
      name: "CoreBillingDataUnavailableError",
    });
  });

  it("keeps pending account documents and commerce command failures explicit", async () => {
    const provider = new CommerceLifecycleProvider();
    removeProviderCapability(provider, "listPaymentMethods");
    const { bursar, billingStore } = await makeBursar(pool, provider);
    await seedSubscription(bursar);

    const preview = await bursar.commerce!.previewPlanChange({
      accountId: USER_ID,
      offerKey: "starter_month",
    });
    if (preview.unchanged) throw new Error("Expected a scheduled plan-change quote");
    await expect(
      bursar.commerce!.confirmPlanChange({
        accountId: USER_ID,
        operationKey: "overview-pending-change",
        offerKey: "starter_month",
        quoteFingerprint: preview.quoteFingerprint,
      }),
    ).resolves.toMatchObject({ scheduled: true, planId: "starter" });
    const conflictingPreview = await bursar.commerce!.previewPlanChange({
      accountId: USER_ID,
      offerKey: "starter_month",
    });
    if (conflictingPreview.unchanged) throw new Error("Expected a second plan-change quote");
    await expect(
      bursar.commerce!.confirmPlanChange({
        accountId: USER_ID,
        operationKey: "overview-conflicting-change",
        offerKey: "starter_month",
        quoteFingerprint: conflictingPreview.quoteFingerprint,
      }),
    ).rejects.toMatchObject({ name: "CheckoutConflictError" });

    await billingStore.upsertBillingInvoice({
      provider: "stripe",
      providerInvoiceId: "in_overview",
      providerSubscriptionId: SUBSCRIPTION_ID,
      userId: USER_ID,
      status: "paid",
      amountPaidMinor: 2_000,
      amountDueMinor: 2_000,
      currency: "USD",
      periodStart: "2026-08-01T00:00:00Z",
      periodEnd: "2026-09-01T00:00:00Z",
      providerUpdatedAt: new Date().toISOString(),
    });
    await bursar.credits.addCredits(USER_ID, new Decimal(1), {
      type: "purchase",
      metadata: { provider: "stripe", provider_document_id: "pay_overview" },
      idempotencyKey: "overview-ledger-document",
    });

    const overview = await bursar.commerce!.getAccountOverview(USER_ID);
    expect(overview.subscriptionSummary.pendingChange).toMatchObject({
      planKey: "starter",
      interval: "month",
      scheduled: true,
    });
    expect(overview.documents).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          kind: "provider_invoice",
          providerDocumentId: "in_overview",
        }),
        expect.objectContaining({
          kind: "ledger_entry",
          providerDocumentId: "pay_overview",
        }),
      ]),
    );
    expect(overview.availability.paymentMethods).toBe(false);
    vi.spyOn(bursar.billing!.autoRecharge, "getStatus").mockRejectedValueOnce(
      new Error("auto-recharge status unavailable"),
    );
    await expect(bursar.commerce!.getAccountOverview(USER_ID)).resolves.toMatchObject({
      availability: { autoRecharge: false },
    });

    await expect(
      bursar.commerce!.updatePreferences({
        accountId: USER_ID,
        patch: { emailNotifications: false },
      }),
    ).resolves.toMatchObject({ userId: USER_ID, emailNotifications: false });
    await expect(bursar.commerce!.getAccountSubscriptionSummary(USER_ID)).resolves.toMatchObject({
      pendingChange: expect.objectContaining({
        planKey: "starter",
        scheduled: true,
      }),
    });
    await expect(
      bursar.commerce!.autoRecharge.getStatus({ accountId: USER_ID }),
    ).resolves.toBeDefined();

    await seedSubscription(bursar, {
      accountId: USER_ID3,
      customerId: CUSTOMER_ID3,
      subscriptionId: "sub_overview_immediate",
      priceId: "price_starter_month",
    });
    const immediatePreview = await bursar.commerce!.previewPlanChange({
      accountId: USER_ID3,
      offerKey: "pro_month",
    });
    if (immediatePreview.unchanged) throw new Error("Expected an immediate plan-change quote");
    await expect(
      bursar.commerce!.confirmPlanChange({
        accountId: USER_ID3,
        operationKey: "overview-immediate-change",
        offerKey: "pro_month",
        quoteFingerprint: immediatePreview.quoteFingerprint,
      }),
    ).resolves.toMatchObject({ scheduled: false });
    const awaitingPaymentPreview = await bursar.commerce!.previewPlanChange({
      accountId: USER_ID3,
      offerKey: "pro_month",
    });
    if (awaitingPaymentPreview.unchanged) throw new Error("Expected a second immediate quote");
    await expect(
      bursar.commerce!.confirmPlanChange({
        accountId: USER_ID3,
        operationKey: "overview-immediate-change-again",
        offerKey: "pro_month",
        quoteFingerprint: awaitingPaymentPreview.quoteFingerprint,
      }),
    ).rejects.toMatchObject({ name: "CheckoutConflictError" });

    removeProviderCapability(provider, "createUpdatePaymentMethodSession");
    await expect(
      bursar.commerce!.createPortalSession({
        accountId: USER_ID,
        purpose: "payment-method",
        returnUrl: "https://app.example/payment-method",
      }),
    ).rejects.toMatchObject({ name: "ProviderCapabilityNotSupportedError" });
    await bursar.ingestBillingEvent({
      provider: "stripe",
      eventId: "evt_overview_setup_customer",
      eventType: "customer.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID2,
      customer: { providerCustomerId: CUSTOMER_ID2 },
    });
    await expect(
      bursar.commerce!.previewPlanChange({
        accountId: USER_ID2,
        offerKey: "starter_month",
      }),
    ).rejects.toMatchObject({ name: "CommerceResourceNotFoundError" });
    removeProviderCapability(provider, "createPaymentMethodSetupSession");
    await expect(
      bursar.commerce!.createPortalSession({
        accountId: USER_ID2,
        purpose: "payment-method",
        returnUrl: "https://app.example/setup",
      }),
    ).rejects.toMatchObject({ name: "ProviderCapabilityNotSupportedError" });
    removeProviderCapability(provider, "createCustomerPortalSession");
    await expect(
      bursar.commerce!.createPortalSession({
        accountId: USER_ID,
        returnUrl: "https://app.example/portal",
      }),
    ).rejects.toMatchObject({ name: "ProviderCapabilityNotSupportedError" });

    await bursar.credits.addCredits(USER_ID, new Decimal(1), {
      type: "purchase",
      metadata: { note: "orphan-document" },
      idempotencyKey: "overview-orphan-document",
    });
    const orphan = (await bursar.credits.listLedgerEntries(USER_ID, { limit: 20 })).items.find(
      (entry) => entry.metadata?.note === "orphan-document",
    );
    expect(orphan).toBeDefined();
    await expect(
      bursar.commerce!.getInvoiceLink({
        accountId: USER_ID,
        document: { kind: "ledger_entry", ledgerEntryId: orphan!.entryId },
      }),
    ).rejects.toMatchObject({ name: "CommerceResourceNotFoundError" });
    removeProviderCapability(provider, "getInvoiceUrl");
    await expect(
      bursar.commerce!.getInvoiceLink({
        accountId: USER_ID,
        document: {
          kind: "provider_invoice",
          provider: "stripe",
          providerDocumentId: "in_overview",
        },
      }),
    ).rejects.toMatchObject({ name: "ProviderCapabilityNotSupportedError" });

    await expect(
      bursar.commerce!.cancelScheduledPlanChange({
        accountId: USER_ID2,
        operationKey: "missing-active-subscription",
      }),
    ).rejects.toMatchObject({ name: "CommerceResourceNotFoundError" });
    await expect(
      bursar.commerce!.cancelScheduledPlanChange({
        accountId: USER_ID,
        operationKey: "cancel-overview-scheduled",
      }),
    ).resolves.toEqual({ success: true });
    await expect(
      bursar.commerce!.cancelScheduledPlanChange({
        accountId: USER_ID,
        operationKey: "cancel-overview-scheduled-again",
      }),
    ).rejects.toMatchObject({ name: "CommerceResourceNotFoundError" });
    const secondScheduledPreview = await bursar.commerce!.previewPlanChange({
      accountId: USER_ID,
      offerKey: "starter_month",
    });
    if (secondScheduledPreview.unchanged) throw new Error("Expected a replacement scheduled quote");
    await expect(
      bursar.commerce!.confirmPlanChange({
        accountId: USER_ID,
        operationKey: "overview-second-scheduled",
        offerKey: "starter_month",
        quoteFingerprint: secondScheduledPreview.quoteFingerprint,
      }),
    ).resolves.toMatchObject({ scheduled: true });
    removeProviderCapability(provider, "cancelScheduledPlanChange");
    await expect(
      bursar.commerce!.cancelScheduledPlanChange({
        accountId: USER_ID,
        operationKey: "missing-scheduled-provider-capability",
      }),
    ).rejects.toMatchObject({ name: "ProviderCapabilityNotSupportedError" });
  });

  it("keeps checkout, portal, and invoice flows explicit across provider terminal states", async () => {
    const provider = new CommerceLifecycleProvider();
    const { bursar, billingStore } = await makeBursar(pool, provider);
    expect(bursar.commerce).not.toBeNull();

    const checkoutInput = {
      subjectId: USER_ID,
      accountId: USER_ID,
      offerKey: "standard_topup",
      returnUrl: "https://app.example/return?intent={intentId}",
      cancelUrl: "https://app.example/cancel?intent={intentId}",
      operationKey: "checkout-provider-terminal",
    } satisfies CreateCheckoutInput;
    const checkout = await bursar.commerce!.createCheckout(checkoutInput);
    provider.checkoutStatus = { paymentStatus: "requires_payment_method" };
    await expect(bursar.commerce!.createCheckout(checkoutInput)).rejects.toBeInstanceOf(
      CheckoutConflictError,
    );

    await bursar.ingestBillingEvent({
      provider: "stripe",
      eventId: "evt_terminal_customer",
      eventType: "customer.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      customer: { providerCustomerId: CUSTOMER_ID },
    });
    await bursar.ingestBillingEvent({
      provider: "stripe",
      eventId: "evt_terminal_subscription",
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
    await expect(
      bursar.commerce!.createCheckout({
        ...checkoutInput,
        offerKey: "pro_month",
        operationKey: "checkout-active-subscription",
      }),
    ).rejects.toThrow("blocking subscription");

    await expect(
      bursar.commerce!.cancelSubscription({
        accountId: USER_ID2,
        operationKey: "cancel-missing-subscription",
      }),
    ).rejects.toThrow("No active subscription found");
    await expect(
      bursar.commerce!.reactivateSubscription({
        accountId: USER_ID2,
        operationKey: "reactivate-missing-subscription",
      }),
    ).rejects.toThrow("No subscription found");

    await bursar.ingestBillingEvent({
      provider: "stripe",
      eventId: "evt_terminal_setup_customer",
      eventType: "customer.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID2,
      customer: { providerCustomerId: CUSTOMER_ID2 },
    });
    await expect(
      bursar.commerce!.createPortalSession({
        accountId: USER_ID2,
        purpose: "payment-method",
        returnUrl: "https://app.example/setup",
        cancelUrl: "https://app.example/setup-cancel",
      }),
    ).resolves.toEqual({
      url: `https://app.example/setup?customer=${CUSTOMER_ID2}`,
    });
    await expect(
      bursar.commerce!.createPortalSession({
        accountId: USER_ID3,
        returnUrl: "https://app.example/portal",
      }),
    ).rejects.toThrow("No billing customer found");

    await billingStore.upsertBillingInvoice({
      provider: "stripe",
      providerInvoiceId: "in_terminal",
      providerSubscriptionId: SUBSCRIPTION_ID,
      userId: USER_ID,
      status: "paid",
      amountPaidMinor: 2_000,
      amountDueMinor: 2_000,
      currency: "USD",
      periodStart: "2026-08-01T00:00:00Z",
      periodEnd: "2026-09-01T00:00:00Z",
      providerUpdatedAt: new Date().toISOString(),
    });
    await expect(
      bursar.commerce!.getInvoiceLink({
        accountId: USER_ID,
        document: {
          kind: "provider_invoice",
          provider: "stripe",
          providerDocumentId: "in_missing",
        },
      }),
    ).rejects.toThrow("Invoice not found");
    await expect(
      bursar.commerce!.getInvoiceLink({
        accountId: USER_ID,
        document: { kind: "ledger_entry", ledgerEntryId: checkout.intentId },
      }),
    ).rejects.toThrow("Ledger entry not found");
    provider.returnNullInvoiceUrl = true;
    await expect(
      bursar.commerce!.getInvoiceLink({
        accountId: USER_ID,
        document: {
          kind: "provider_invoice",
          provider: "stripe",
          providerDocumentId: "in_terminal",
        },
      }),
    ).rejects.toThrow("No invoice URL is available");
  });

  it("supports immediate plan changes and restores cancellation after provider failure", async () => {
    const provider = new CommerceLifecycleProvider();
    const { bursar } = await makeBursar(pool, provider);
    expect(bursar.commerce).not.toBeNull();

    await bursar.ingestBillingEvent({
      provider: "stripe",
      eventId: "evt_plan_failure_customer",
      eventType: "customer.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      customer: { providerCustomerId: CUSTOMER_ID },
    });
    await bursar.ingestBillingEvent({
      provider: "stripe",
      eventId: "evt_plan_failure_subscription",
      eventType: "subscription.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID,
      customer: { providerCustomerId: CUSTOMER_ID },
      subscription: {
        providerSubscriptionId: SUBSCRIPTION_ID,
        status: "active",
        refs: { priceId: "price_starter_month" },
        interval: "month",
        intervalCount: 1,
      },
    });

    const unchanged = await bursar.commerce!.previewPlanChange({
      accountId: USER_ID,
      offerKey: "starter_month",
    });
    expect(unchanged).toMatchObject({ unchanged: true, planId: "starter" });
    await expect(
      bursar.commerce!.confirmPlanChange({
        accountId: USER_ID,
        operationKey: "unchanged-plan",
        offerKey: "starter_month",
        quoteFingerprint: "unchanged-plan",
      }),
    ).resolves.toMatchObject({ success: true, unchanged: true });

    const activePreview = await bursar.commerce!.previewPlanChange({
      accountId: USER_ID,
      offerKey: "pro_month",
    });
    if (activePreview.unchanged) throw new Error("Expected immediate plan-change quote");
    expect(activePreview.scheduled).toBe(false);
    const confirmed = await bursar.commerce!.confirmPlanChange({
      accountId: USER_ID,
      operationKey: "immediate-upgrade",
      offerKey: "pro_month",
      quoteFingerprint: activePreview.quoteFingerprint,
    });
    expect(confirmed).toMatchObject({
      success: true,
      scheduled: false,
      planId: "pro",
    });

    await bursar.ingestBillingEvent({
      provider: "stripe",
      eventId: "evt_plan_failure_customer_2",
      eventType: "customer.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID2,
      customer: { providerCustomerId: CUSTOMER_ID2 },
    });
    await bursar.ingestBillingEvent({
      provider: "stripe",
      eventId: "evt_plan_failure_subscription_2",
      eventType: "subscription.created",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID2,
      customer: { providerCustomerId: CUSTOMER_ID2 },
      subscription: {
        providerSubscriptionId: SUBSCRIPTION_ID2,
        status: "active",
        refs: { priceId: "price_pro_month" },
        interval: "month",
        intervalCount: 1,
      },
    });
    await bursar.ingestBillingEvent({
      provider: "stripe",
      eventId: "evt_plan_failure_cancel",
      eventType: "subscription.cancellation_scheduled",
      occurredAt: new Date().toISOString(),
      accountId: USER_ID2,
      customer: { providerCustomerId: CUSTOMER_ID2 },
      subscription: { providerSubscriptionId: SUBSCRIPTION_ID2 },
    });
    provider.failChangePlan = true;
    const scheduledPreview = await bursar.commerce!.previewPlanChange({
      accountId: USER_ID2,
      offerKey: "starter_month",
    });
    if (scheduledPreview.unchanged) throw new Error("Expected scheduled plan-change quote");
    await expect(
      bursar.commerce!.confirmPlanChange({
        accountId: USER_ID2,
        operationKey: "failed-plan-change",
        offerKey: "starter_month",
        quoteFingerprint: scheduledPreview.quoteFingerprint,
      }),
    ).rejects.toThrow("provider plan change failed");
    expect(provider.reactivated).toContain(SUBSCRIPTION_ID2);
    expect(provider.canceled).toContain(SUBSCRIPTION_ID2);
    const failedChange = await pool.query<{ state: string }>(
      `SELECT state
         FROM bursar.billing_subscription_changes AS change
         JOIN bursar.billing_subscriptions AS subscription
           ON subscription.id = change.subscription_id
        WHERE subscription.provider = $1
          AND subscription.provider_subscription_id = $2
        ORDER BY change.created_at DESC
        LIMIT 1`,
      ["stripe", SUBSCRIPTION_ID2],
    );
    expect(failedChange.rows).toEqual([{ state: "failed" }]);
  });

  it("durably records plan-change compensation and cancellation failures", async () => {
    const provider = new CommerceLifecycleProvider();
    provider.failChangePlan = true;
    provider.failCancelSubscription = true;
    const { bursar } = await makeBursar(pool, provider);
    await seedSubscription(bursar, {
      priceId: "price_starter_month",
      cancelAtPeriodEnd: true,
    });

    const preview = await bursar.commerce!.previewPlanChange({
      accountId: USER_ID,
      offerKey: "pro_month",
    });
    if (preview.unchanged) throw new Error("Expected an immediate plan-change quote");
    await expect(
      bursar.commerce!.confirmPlanChange({
        accountId: USER_ID,
        operationKey: "plan-change-compensation-failure",
        offerKey: "pro_month",
        quoteFingerprint: preview.quoteFingerprint,
      }),
    ).rejects.toMatchObject({ name: "AggregateError" });

    const failedChange = await pool.query<{
      state: string;
      error_message: string | null;
    }>(
      `SELECT state, error_message
         FROM bursar.billing_subscription_changes AS change
         JOIN bursar.billing_subscriptions AS subscription
           ON subscription.id = change.subscription_id
        WHERE subscription.provider_subscription_id = $1
        ORDER BY change.created_at DESC
        LIMIT 1`,
      [SUBSCRIPTION_ID],
    );
    expect(failedChange.rows[0]).toMatchObject({
      state: "failed",
      error_message: expect.stringContaining("AggregateError"),
    });

    await seedSubscription(bursar, {
      accountId: USER_ID2,
      customerId: CUSTOMER_ID2,
      subscriptionId: SUBSCRIPTION_ID2,
    });
    const cancelSubscription = provider.cancelSubscription;
    removeProviderCapability(provider, "cancelSubscription");
    await expect(
      bursar.commerce!.cancelAllSubscriptions({
        accountId: USER_ID2,
        operationKey: "cancel-all-unsupported-provider",
      }),
    ).rejects.toMatchObject({ name: "AggregateError" });
    provider.cancelSubscription = cancelSubscription;
    await expect(
      bursar.commerce!.cancelAllSubscriptions({
        accountId: USER_ID2,
        operationKey: "cancel-all-provider-failure",
      }),
    ).rejects.toMatchObject({ name: "AggregateError" });
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
      customer: {
        providerCustomerId: CUSTOMER_ID2,
        email: "buyer2@example.com",
      },
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
      customer: {
        providerCustomerId: CUSTOMER_ID3,
        email: "buyer3@example.com",
      },
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

describe.runIf(DATABASE_URL)("checkout recovery across catalog changes", () => {
  it("replays the stored session after its offer is removed", async () => {
    const pool = new pg.Pool({ connectionString: DATABASE_URL!, max: 1 });
    try {
      await applyMigrations(pool);
      await truncateBursarTables(pool);
      const { bursar, billingStore, provider } = await makeBursar(pool);
      const input = {
        subjectId: USER_ID,
        accountId: USER_ID,
        offerKey: "pro_month",
        returnUrl: "https://return.test",
        cancelUrl: "https://cancel.test",
        operationKey: "review-retired",
      };
      const first = await bursar.commerce!.createCheckout(input);
      const sameRevision = await bursar.commerce!.createCheckout(input);
      const config = structuredClone(CONFIG);
      Reflect.deleteProperty(config.commerce.offers, "pro_month");
      await bursar.catalog.publishAndActivate(config);
      const retry = await bursar.commerce!.createCheckout(input);
      const intent = await billingStore.getCheckoutIntent(first.intentId, USER_ID);
      expect(sameRevision).toEqual(first);
      expect(retry).toEqual(first);
      expect(intent?.providerSessionId).toBe(`session_bursar:checkout:${first.intentId}`);
      expect(provider.checkoutParams).toHaveLength(1);
    } finally {
      await pool.end();
    }
  }, 120000);
});

describe.runIf(DATABASE_URL)("checkout settlement terms", () => {
  it("grants using the checkout revision after top-up terms change", async () => {
    const pool = new pg.Pool({ connectionString: DATABASE_URL!, max: 1 });
    try {
      await applyMigrations(pool);
      await truncateBursarTables(pool);
      const { bursar } = await makeBursar(pool);
      const checkout = await bursar.commerce!.createCheckout({
        subjectId: USER_ID2,
        accountId: USER_ID,
        offerKey: "standard_topup",
        returnUrl: "https://return.test",
        cancelUrl: "https://cancel.test",
        operationKey: "pinned-terms",
      });
      await expect(
        bursar.commerce!.getCheckoutStatus({ intentId: checkout.intentId, subjectId: USER_ID2 }),
      ).resolves.toMatchObject({ intentId: checkout.intentId });
      await expect(
        bursar.commerce!.getCheckoutStatus({ intentId: checkout.intentId, subjectId: USER_ID }),
      ).rejects.toBeInstanceOf(CommerceResourceNotFoundError);
      const next = structuredClone(CONFIG);
      next.commerce.offers.standard_topup.credits_per_unit = "200";
      await bursar.catalog.publishAndActivate(next);
      const result = await bursar.ingestBillingEvent({
        provider: "stripe",
        eventId: "pinned-payment",
        eventType: "payment.succeeded",
        occurredAt: new Date().toISOString(),
        accountId: USER_ID,
        metadata: { checkout_intent_id: checkout.intentId },
        payment: {
          providerPaymentId: "pinned-payment-id",
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
    } finally {
      await pool.end();
    }
  }, 120000);
  it.each(["subscription.created", "payment.succeeded"] as const)(
    "provisions a first %s event from its removed checkout offer",
    async (eventType) => {
      const pool = new pg.Pool({ connectionString: DATABASE_URL!, max: 2 });
      try {
        await applyMigrations(pool);
        await truncateBursarTables(pool);
        const { bursar } = await makeBursar(pool);
        const checkout = await bursar.commerce!.createCheckout({
          subjectId: USER_ID2,
          accountId: USER_ID,
          offerKey: "pro_month",
          returnUrl: "https://return.test",
          cancelUrl: "https://cancel.test",
          operationKey: "pinned-subscription",
        });
        const next = structuredClone(CONFIG);
        Reflect.deleteProperty(next.commerce.offers, "pro_month");
        await bursar.catalog.publishAndActivate(next);
        for (const [suffix, accountId, priceId] of [
          ["wrong-account", USER_ID3, "price_pro_month"],
          ["wrong-price", USER_ID, "price_starter_month"],
        ] as const) {
          const rejected = await bursar.ingestBillingEvent({
            provider: "stripe",
            eventId: `rejected-${eventType}-${suffix}`,
            eventType: "subscription.created",
            occurredAt: new Date().toISOString(),
            accountId,
            metadata: { checkout_intent_id: checkout.intentId },
            subscription: {
              providerSubscriptionId: `rejected-${suffix}`,
              status: "active",
              refs: { priceId },
            },
          });
          expect(rejected.handled).toBe(false);
        }
        const result = await bursar.ingestBillingEvent({
          provider: "stripe",
          eventId: "pinned-subscription-event",
          eventType,
          occurredAt: new Date().toISOString(),
          accountId: USER_ID,
          metadata: { checkout_intent_id: checkout.intentId },
          subscription: {
            providerSubscriptionId: "pinned-subscription-id",
            status: "active",
            refs: { priceId: "price_pro_month" },
          },
          ...(eventType === "payment.succeeded"
            ? {
                payment: {
                  providerPaymentId: "subscription-payment",
                  amountMinor: 2000,
                  taxMinor: 0,
                  currency: "USD",
                  purpose: "subscription" as const,
                  status: "succeeded" as const,
                },
              }
            : undefined),
        });
        expect(result.handled).toBe(true);
        const rows = await pool.query(
          "SELECT o.offer_key, o.plan_key FROM bursar.billing_subscriptions s JOIN bursar.catalog_offers o ON o.id = s.offer_id WHERE s.provider_subscription_id = $1",
          ["pinned-subscription-id"],
        );
        expect(rows.rows[0]).toMatchObject({ offer_key: "pro_month", plan_key: "pro" });
        expect((await bursar.credits.getUserPlan(USER_ID)).planKey).toBe("pro");
        await bursar.ingestBillingEvent({
          provider: "stripe",
          eventId: "subsequent-plan-change",
          eventType: "subscription.plan_changed",
          occurredAt: new Date(Date.now() + 1000).toISOString(),
          accountId: USER_ID,
          metadata: { checkout_intent_id: checkout.intentId },
          subscription: {
            providerSubscriptionId: "pinned-subscription-id",
            status: "active",
            refs: { priceId: "price_starter_month" },
          },
        });
        const changed = await pool.query(
          "SELECT o.offer_key FROM bursar.billing_subscriptions s JOIN bursar.catalog_offers o ON o.id = s.offer_id WHERE s.provider_subscription_id = $1",
          ["pinned-subscription-id"],
        );
        expect(changed.rows[0]?.offer_key).toBe("starter_month");
      } finally {
        await pool.end();
      }
    },
    120000,
  );
  it("provisions a Dodo payment-first checkout through the provider mapper", async () => {
    const pool = new pg.Pool({ connectionString: DATABASE_URL!, max: 2 });
    try {
      await applyMigrations(pool);
      await truncateBursarTables(pool);
      const config = structuredClone(CONFIG);
      Object.assign(config.commerce.providers, { dodo: { type: "dodo" } });
      Object.assign(config.commerce.offers.pro_month.providers, {
        dodo: { type: "dodo_product", product_id: "prod_pinned" },
      });
      class DodoFixtureProvider extends IntegrationProvider {
        override readonly provider = "dodo";
      }
      const provider = new DodoFixtureProvider();
      const billingStore = new PostgresBillingStore({
        postgres: pool,
        tenantId: TEST_TENANT_ID,
        providerEnvironment: "test",
      });
      const creditStore = new PostgresStore({
        postgres: pool,
        tenantId: TEST_TENANT_ID,
        providerEnvironment: "test",
      });
      const bursar = new Bursar({
        creditStore,
        billingStore,
        commerceOptions: { providerEnvironment: "test", providers: { dodo: () => provider } },
      });
      await bursar.catalog.publishAndActivate(config);
      const checkout = await bursar.commerce!.createCheckout({
        subjectId: USER_ID2,
        accountId: USER_ID,
        provider: "dodo",
        offerKey: "pro_month",
        operationKey: "dodo-pinned",
        returnUrl: "https://return.test",
        cancelUrl: "https://cancel.test",
      });
      Reflect.deleteProperty(config.commerce.offers, "pro_month");
      await bursar.catalog.publishAndActivate(config);
      const outcomes: boolean[] = [];
      const sink = {
        ingestBillingEvent: async (event: Parameters<Bursar["ingestBillingEvent"]>[0]) => {
          const result = await bursar.ingestBillingEvent(event);
          outcomes.push(result.handled);
          return result;
        },
      };
      const metadata = { checkout_intent_id: checkout.intentId, plan_slug: "pro" };
      const payment = {
        payment_id: "dodo-no-product",
        subscription_id: "dodo-sub",
        settlement_amount: 2000,
        settlement_currency: "USD",
      };
      await expect(
        mapDodoEvent("payment.succeeded", payment, USER_ID, metadata, sink),
      ).rejects.toThrow();
      expect(outcomes.every((handled) => !handled)).toBe(true);
      outcomes.length = 0;
      await mapDodoEvent(
        "payment.succeeded",
        { ...payment, payment_id: "dodo-with-product", product_id: "prod_pinned" },
        USER_ID,
        metadata,
        sink,
      );
      expect(outcomes).toEqual([true]);
      expect((await bursar.credits.getUserPlan(USER_ID)).planKey).toBe("pro");
    } finally {
      await pool.end();
    }
  }, 120000);
});

describe.runIf(DATABASE_URL)("auto-recharge settlement terms", () => {
  it("uses the claimed attempt revision after the catalog changes", async () => {
    const pool = new pg.Pool({ connectionString: DATABASE_URL!, max: 1 });
    try {
      await applyMigrations(pool);
      await truncateBursarTables(pool);
      const { bursar } = await makeBursar(pool);
      await bursar.ingestBillingEvent({
        provider: "stripe",
        eventId: "recharge-customer",
        eventType: "customer.created",
        occurredAt: new Date().toISOString(),
        accountId: USER_ID,
        customer: { providerCustomerId: CUSTOMER_ID },
      });
      await bursar.commerce!.autoRecharge.enable({
        accountId: USER_ID,
        returnUrl: "https://return.test",
      });
      const attempts = await pool.query(
        "SELECT id FROM bursar.billing_auto_recharge_attempts WHERE subject_id=$1",
        [USER_ID],
      );
      expect(attempts.rows).toHaveLength(1);
      const next = structuredClone(CONFIG);
      next.commerce.offers.standard_topup.credits_per_unit = "200";
      await bursar.catalog.publishAndActivate(next);
      const result = await bursar.ingestBillingEvent({
        provider: "stripe",
        eventId: "recharge-succeeded",
        eventType: "payment.succeeded",
        occurredAt: new Date().toISOString(),
        accountId: USER_ID,
        metadata: { auto_recharge_attempt_id: attempts.rows[0].id },
        payment: {
          providerPaymentId: "auto_pay_processing",
          amountMinor: 500,
          taxMinor: 0,
          currency: "USD",
          purpose: "credit_topup",
          status: "succeeded",
          refs: { priceId: "price_topup_500" },
        },
      });
      expect(result.handled).toBe(true);
      expect((await bursar.credits.getBalance(USER_ID)).balance.toString()).toBe("100");
    } finally {
      await pool.end();
    }
  }, 120000);
});

describe.runIf(DATABASE_URL)("provider checkout idempotency scope", () => {
  it("uses a durable intent identity for each subject sharing a local key", async () => {
    const pool = new pg.Pool({ connectionString: DATABASE_URL!, max: 2 });
    try {
      await applyMigrations(pool);
      await truncateBursarTables(pool);
      const provider = new IntegrationProvider();
      vi.spyOn(provider, "createCheckoutSession").mockImplementation(async (params) => {
        provider.checkoutParams.push(params);
        return { url: params.returnUrl, providerSessionId: `session_${params.idempotencyKey}` };
      });
      const { bursar } = await makeBursar(pool, provider);
      const common = {
        offerKey: "standard_topup",
        returnUrl: "https://return.test",
        cancelUrl: "https://cancel.test",
        operationKey: "shared-key",
      };
      const first = await bursar.commerce!.createCheckout({
        ...common,
        subjectId: USER_ID,
        accountId: USER_ID,
      });
      const second = await bursar.commerce!.createCheckout({
        ...common,
        subjectId: USER_ID2,
        accountId: USER_ID2,
      });
      expect(first.intentId).not.toBe(second.intentId);
      expect(provider.checkoutParams.map((params) => params.idempotencyKey)).toEqual([
        `bursar:checkout:${first.intentId}`,
        `bursar:checkout:${second.intentId}`,
      ]);
      expect(
        await bursar.commerce!.createCheckout({
          ...common,
          subjectId: USER_ID,
          accountId: USER_ID,
        }),
      ).toEqual(first);
      expect(
        await bursar.commerce!.createCheckout({
          ...common,
          subjectId: USER_ID2,
          accountId: USER_ID2,
        }),
      ).toEqual(second);
      expect(provider.checkoutParams).toHaveLength(2);
    } finally {
      await pool.end();
    }
  }, 120000);
});

describe.runIf(DATABASE_URL)("checkout catalog reservation races", () => {
  it("retries a stale snapshot and pins the new catalog before contacting the provider", async () => {
    const pool = new pg.Pool({ connectionString: DATABASE_URL!, max: 2 });
    try {
      await applyMigrations(pool);
      await truncateBursarTables(pool);
      const { bursar, billingStore, provider } = await makeBursar(pool);
      let release = () => {};
      let reached = () => {};
      const waiting = new Promise<void>((resolve) => {
        release = resolve;
      });
      const arrived = new Promise<void>((resolve) => {
        reached = resolve;
      });
      const original = billingStore.createOrGetCheckoutIntent.bind(billingStore);
      const spy = vi
        .spyOn(billingStore, "createOrGetCheckoutIntent")
        .mockImplementationOnce(async (input) => {
          reached();
          await waiting;
          return original(input);
        });
      const input = {
        subjectId: USER_ID,
        accountId: USER_ID,
        offerKey: "standard_topup",
        returnUrl: "https://return.test",
        cancelUrl: "https://cancel.test",
        operationKey: "snapshot-race",
      };
      const pending = bursar.commerce!.createCheckout(input);
      await arrived;
      const next = structuredClone(CONFIG);
      next.commerce.offers.standard_topup.quantity.default = 2;
      await bursar.catalog.publishAndActivate(next);
      release();
      const result = await pending;
      expect(provider.checkoutParams).toHaveLength(1);
      expect(provider.checkoutParams[0]?.quantity).toBe(2);
      const row = await pool.query(
        "SELECT i.catalog_revision_id = r.id AS pinned FROM bursar.billing_checkout_intents i JOIN bursar.catalog_revisions r ON r.status = 'active' WHERE i.id = $1",
        [result.intentId],
      );
      expect(row.rows[0]?.pinned).toBe(true);
      await expect(
        bursar.commerce!.createCheckout({ ...input, quantity: 1 }),
      ).rejects.toBeInstanceOf(CheckoutConflictError);
      spy.mockRestore();
    } finally {
      await pool.end();
    }
  }, 120000);
  it("uses the winning catalog when a stale concurrent request reaches its intent", async () => {
    const pool = new pg.Pool({ connectionString: DATABASE_URL!, max: 4 });
    let releaseStale = () => {};
    let releaseWinner = () => {};
    let releaseReservation = () => {};
    try {
      await applyMigrations(pool);
      await truncateBursarTables(pool);
      const { bursar, billingStore, provider } = await makeBursar(pool);
      let reachedStale!: () => void;
      const waitStale = new Promise<void>((resolve) => {
        releaseStale = resolve;
      });
      const arrivedStale = new Promise<void>((resolve) => {
        reachedStale = resolve;
      });
      let reachedReservation!: () => void;
      const reservationArrived = new Promise<void>((resolve) => {
        reachedReservation = resolve;
      });
      const reservationGate = new Promise<void>((resolve) => {
        releaseReservation = resolve;
      });
      const original = billingStore.createOrGetCheckoutIntent.bind(billingStore);
      vi.spyOn(billingStore, "createOrGetCheckoutIntent").mockImplementationOnce(async (input) => {
        reachedStale();
        await waitStale;
        try {
          return await original(input);
        } finally {
          reachedReservation();
          await reservationGate;
        }
      });
      let reachedWinner!: () => void;
      const waitWinner = new Promise<void>((resolve) => {
        releaseWinner = resolve;
      });
      const arrivedWinner = new Promise<void>((resolve) => {
        reachedWinner = resolve;
      });
      const createSession = provider.createCheckoutSession.bind(provider);
      vi.spyOn(provider, "createCheckoutSession").mockImplementationOnce(async (params) => {
        reachedWinner();
        await waitWinner;
        return createSession(params);
      });
      const input = {
        subjectId: USER_ID,
        accountId: USER_ID,
        offerKey: "standard_topup",
        returnUrl: "https://return.test",
        cancelUrl: "https://cancel.test",
        operationKey: "revision-winner",
      };
      const stale = bursar.commerce!.createCheckout(input);
      await arrivedStale;
      const next = structuredClone(CONFIG);
      next.commerce.offers.standard_topup.providers.stripe.price_id = "price_new_topup";
      await bursar.catalog.publishAndActivate(next);
      const winner = bursar.commerce!.createCheckout(input);
      await arrivedWinner;
      releaseStale();
      await reservationArrived;
      releaseWinner();
      const winnerResult = await winner;
      releaseReservation();
      const staleResult = await stale;
      expect(staleResult.intentId).toBe(winnerResult.intentId);
      expect(provider.checkoutParams.length).toBeGreaterThan(0);
      expect(
        provider.checkoutParams.every((params) => params.productId === "price_new_topup"),
      ).toBe(true);
    } finally {
      releaseStale();
      releaseWinner();
      releaseReservation();
      await pool.end();
    }
  }, 120000);
  it("reloads a concurrent winner after catalog defaults change", async () => {
    const pool = new pg.Pool({ connectionString: DATABASE_URL!, max: 2 });
    try {
      await applyMigrations(pool);
      await truncateBursarTables(pool);
      const { bursar, billingStore, provider } = await makeBursar(pool);
      let release = () => {};
      let reached = () => {};
      const waiting = new Promise<void>((resolve) => {
        release = resolve;
      });
      const arrived = new Promise<void>((resolve) => {
        reached = resolve;
      });
      const original = billingStore.createOrGetCheckoutIntent.bind(billingStore);
      const spy = vi
        .spyOn(billingStore, "createOrGetCheckoutIntent")
        .mockImplementationOnce(async (input) => {
          reached();
          await waiting;
          return original(input);
        });
      const input = {
        subjectId: USER_ID,
        accountId: USER_ID,
        offerKey: "standard_topup",
        returnUrl: "https://return.test",
        cancelUrl: "https://cancel.test",
        operationKey: "winner-race",
      };
      const pending = bursar.commerce!.createCheckout(input);
      await arrived;
      const winner = await bursar.commerce!.createCheckout(input);
      const next = structuredClone(CONFIG);
      next.commerce.offers.standard_topup.quantity.default = 2;
      await bursar.catalog.publishAndActivate(next);
      release();
      expect(await pending).toEqual(winner);
      expect(provider.checkoutParams).toHaveLength(1);
      await expect(
        bursar.commerce!.createCheckout({ ...input, provider: "another" }),
      ).rejects.toBeInstanceOf(CheckoutConflictError);
      spy.mockRestore();
    } finally {
      await pool.end();
    }
  }, 120000);
});
