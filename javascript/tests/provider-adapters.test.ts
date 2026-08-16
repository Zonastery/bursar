import { describe, expect, it, vi } from "vitest";
import DodoPayments, { NotFoundError } from "dodopayments";
import type Stripe from "stripe";
import { z } from "zod";
import { ProviderResponseError, StoreUnavailableError } from "../src/errors.js";
import { callBillingEventSink } from "../src/providers/_shared.js";
import type { BillingEventSink } from "../src/billing/contracts.js";
import type { DodoClient } from "../src/providers/dodo/client-contract.js";
import { DodoProvider } from "../src/providers/dodo/provider.js";
import { MockPaymentProvider } from "../src/providers/mock/provider.js";
import { StripeProvider } from "../src/providers/stripe/provider.js";
import { isExternalObject, type ExternalObject, type ExternalValue } from "../src/shared/json.js";

function testDodoClient<TClient>(client: TClient): DodoClient {
  // SAFETY: Each Dodo fixture implements exactly the provider methods exercised by its scenario.
  return client as DodoClient;
}

function testStripeClient<TClient>(client: TClient): Stripe {
  // SAFETY: Each Stripe fixture implements exactly the provider methods exercised by its scenario.
  return client as Stripe;
}

const sink = {
  ingestBillingEvent: async () => ({ handled: true, action: "ok" }),
} satisfies BillingEventSink;

describe("payment provider adapter contracts", () => {
  it("retries an in-flight duplicate before returning its result", async () => {
    vi.useFakeTimers();
    try {
      const ingestBillingEvent = vi
        .fn()
        .mockResolvedValueOnce({ handled: false, error: "claim_busy" })
        .mockResolvedValueOnce({ handled: false, error: "claim_busy" })
        .mockResolvedValue({ handled: true, action: "duplicate" });
      const pending = callBillingEventSink(
        { ingestBillingEvent },
        {
          provider: "stripe",
          eventId: "evt_busy",
          eventType: "invoice.paid",
          occurredAt: "2026-07-29T12:00:00.000Z",
        },
      );

      await vi.runAllTimersAsync();

      await expect(pending).resolves.toMatchObject({ handled: true, action: "duplicate" });
      expect(ingestBillingEvent).toHaveBeenCalledTimes(3);
    } finally {
      vi.useRealTimers();
    }
  });

  it.each(["invalid_request", "idempotency_conflict", "max_retries_exceeded"])(
    "acknowledges the permanent billing claim outcome %s",
    async (error) => {
      const result = { handled: false, error };

      await expect(
        callBillingEventSink(
          { ingestBillingEvent: vi.fn().mockResolvedValue(result) },
          {
            provider: "stripe",
            eventId: `evt_${error}`,
            eventType: "invoice.paid",
            occurredAt: "2026-07-29T12:00:00.000Z",
          },
        ),
      ).resolves.toEqual(result);
    },
  );

  it("keeps a legacy retry claim retryable", async () => {
    await expect(
      callBillingEventSink(
        {
          ingestBillingEvent: vi
            .fn()
            .mockResolvedValue({ handled: false, error: "claim_failed_retry" }),
        },
        {
          provider: "stripe",
          eventId: "evt_claim_retry",
          eventType: "invoice.paid",
          occurredAt: "2026-07-29T12:00:00.000Z",
        },
      ),
    ).rejects.toBeInstanceOf(StoreUnavailableError);
  });

  it("maps Dodo requests, idempotency, and response DTOs", async () => {
    const calls: ExternalValue[][] = [];
    const customerCreate = vi.fn(async () => ({ customer_id: "cus_1" }));
    const updatePaymentMethod = vi.fn(async () => ({
      payment_link: "https://update-payment-method.test",
    }));
    const client = {
      webhooks: {
        unwrap: () => ({ type: "payment.succeeded", data: {} }),
      },
      checkoutSessions: {
        create: async (...args: ExternalValue[]) => {
          calls.push(args);
          if (isExternalObject(args[0]) && args[0].confirm === true) {
            return { session_id: "sess_auto", payment_id: "pay_auto" };
          }
          return { checkout_url: "https://checkout.test", session_id: "sess_1" };
        },
        retrieve: async () => ({ payment_status: "paid" }),
      },
      customers: {
        customerPortal: { create: async () => ({ link: "https://portal.test" }) },
        retrievePaymentMethods: async () => ({
          items: [
            {
              payment_method: "card",
              payment_method_id: "pm_1",
              recurring_enabled: true,
              card: {
                last4_digits: "4242",
                card_network: "visa",
                expiry_month: "1",
                expiry_year: "2030",
              },
            },
            {
              payment_method: "card",
              payment_method_id: "pm_1_duplicate",
              recurring_enabled: true,
              card: {
                last4_digits: "4242",
                card_network: "visa",
                expiry_month: "1",
                expiry_year: "2030",
              },
            },
            { payment_method: "paypal", payment_method_id: "pm_2" },
          ],
        }),
        create: customerCreate,
      },
      payments: {
        retrieve: async (id: string) =>
          id === "pay_auto"
            ? { payment_id: id, status: "succeeded", total_amount: 500, currency: "USD" }
            : {
                invoice_url: "https://invoice.test/document.pdf",
                payment_link: "https://checkout.test/not-an-invoice",
              },
      },
      subscriptions: {
        update: async (...args: ExternalValue[]) => calls.push(args),
        updatePaymentMethod,
        changePlan: async (...args: ExternalValue[]) => calls.push(args),
        previewChangePlan: async () => ({
          immediate_charge: {
            line_items: [],
            summary: { total_amount: 12, settlement_amount: 10, settlement_currency: "USD" },
            effective_at: "2026-01-01T00:00:00Z",
          },
        }),
      },
    };
    const typedClient = testDodoClient(client);
    const provider = new DodoProvider({
      getClient: () => typedClient,
      webhookKey: "k",
      setupProductId: "setup",
      eventSink: sink,
    });
    await expect(
      provider.createCheckoutSession({
        accountId: "user-1",
        productId: "prod_1",
        type: "credit_pack",
        returnUrl: "https://return",
        cancelUrl: "https://cancel",
        quantity: 2,
        metadata: { bursar_account_id: "untrusted" },
        idempotencyKey: "idem_1",
      }),
    ).resolves.toEqual({ url: "https://checkout.test", providerSessionId: "sess_1" });
    await expect(
      provider.createCheckoutSession({
        accountId: "user-1",
        productId: "prod_1",
        type: "credit_pack",
        returnUrl: "https://return",
        cancelUrl: "https://cancel",
        quantity: Number.MAX_SAFE_INTEGER + 1,
        metadata: {},
        idempotencyKey: "unsafe_quantity",
      }),
    ).rejects.toBeInstanceOf(ProviderResponseError);
    expect(calls[0]).toEqual([
      {
        product_cart: [{ product_id: "prod_1", quantity: 2 }],
        customer: undefined,
        return_url: "https://return",
        cancel_url: "https://cancel",
        metadata: { bursar_account_id: "user-1" },
      },
      { headers: { "Idempotency-Key": "idem_1" } },
    ]);
    await expect(provider.getCheckoutSessionStatus("sess_1")).resolves.toEqual({
      paymentStatus: "paid",
    });
    await expect(
      provider.createCustomerPortalSession({ customerId: "cus_1", returnUrl: "https://return" }),
    ).resolves.toEqual({ url: "https://portal.test" });
    await expect(
      provider.createUpdatePaymentMethodSession({
        customerId: "cus_1",
        subscriptionId: "sub_1",
        returnUrl: "https://return/update",
      }),
    ).resolves.toEqual({ url: "https://update-payment-method.test" });
    expect(updatePaymentMethod).toHaveBeenCalledWith("sub_1", {
      payment_method: { type: "new", return_url: "https://return/update" },
    });
    await expect(
      provider.createPaymentMethodSetupSession({
        customerId: "cus_1",
        returnUrl: "https://return/setup",
      }),
    ).resolves.toEqual({ url: "https://checkout.test" });
    expect(calls[1]).toEqual([
      {
        product_cart: [{ product_id: "setup", quantity: 1 }],
        customer: { customer_id: "cus_1" },
        return_url: "https://return/setup",
        metadata: { purpose: "setup_payment_method" },
        subscription_data: { on_demand: { mandate_only: true } },
      },
    ]);
    await expect(provider.listPaymentMethods("cus_1")).resolves.toEqual([
      {
        id: "pm_1",
        last4: "4242",
        brand: "visa",
        expiryMonth: 1,
        expiryYear: 2030,
        isDefault: true,
      },
    ]);
    await expect(
      provider.chargeSavedPaymentMethod({
        customerId: "cus_1",
        paymentMethodId: "pm_1",
        productId: "prod_topup",
        quantity: 1,
        metadata: { purpose: "credit_topup" },
        idempotencyKey: "auto_1",
      }),
    ).resolves.toMatchObject({ providerPaymentId: "pay_auto", status: "succeeded" });
    await expect(provider.getInvoiceUrl("pay_1")).resolves.toEqual({
      url: "https://invoice.test/document.pdf",
    });
    await expect(
      provider.createCustomer({
        email: "u1@example.com",
        name: "User One",
        metadata: {},
        idempotencyKey: "customer:user-1",
      }),
    ).resolves.toEqual({ customerId: "cus_1" });
    expect(customerCreate).toHaveBeenCalledWith(
      { email: "u1@example.com", name: "User One", metadata: {} },
      { headers: { "Idempotency-Key": "customer:user-1" } },
    );
    await expect(
      provider.previewChangePlan({
        providerSubscriptionId: "sub_1",
        productId: "prod_2",
        prorationBillingMode: "prorated_immediately",
      }),
    ).resolves.toMatchObject({ totalAmount: 12, settlementAmount: 10 });
  });

  it("locks a submitted email in the hosted Dodo checkout", async () => {
    const create = vi.fn(async () => ({
      checkout_url: "https://checkout.test",
      session_id: "sess_guest",
    }));
    const client = testDodoClient({ checkoutSessions: { create } });
    const provider = new DodoProvider({
      getClient: () => client,
      webhookKey: "k",
      eventSink: sink,
    });

    await provider.createCheckoutSession({
      accountId: "guest-account-1",
      email: "guest@example.com",
      productId: "prod_guest",
      type: "subscription",
      returnUrl: "https://return.test",
      cancelUrl: "https://cancel.test",
      metadata: { checkout_intent_id: "intent-1" },
      idempotencyKey: "guest-checkout:1",
    });

    expect(create).toHaveBeenCalledWith(
      {
        product_cart: [{ product_id: "prod_guest", quantity: 1 }],
        customer: { email: "guest@example.com" },
        return_url: "https://return.test",
        cancel_url: "https://cancel.test",
        metadata: {
          checkout_intent_id: "intent-1",
          bursar_account_id: "guest-account-1",
        },
        feature_flags: { allow_customer_editing_email: false },
      },
      { headers: { "Idempotency-Key": "guest-checkout:1" } },
    );
  });

  it("sends explicit Dodo idempotency headers for every keyed provider mutation", async () => {
    const checkoutCreate = vi.fn(async (...args: ExternalValue[]) => {
      const body = args[0];
      return isExternalObject(body) && body.confirm === true
        ? { session_id: "sess_charge", payment_id: "pay_charge" }
        : { checkout_url: "https://checkout.test", session_id: "sess_checkout" };
    });
    const customerCreate = vi.fn(async () => ({ customer_id: "cus_1" }));
    const subscriptionUpdate = vi.fn(async () => undefined);
    const cancelChangePlan = vi.fn(async () => undefined);
    const changePlan = vi.fn(async () => undefined);
    const client = {
      checkoutSessions: { create: checkoutCreate },
      customers: { create: customerCreate },
      payments: {
        retrieve: vi.fn(async () => ({
          payment_id: "pay_charge",
          status: "succeeded",
          total_amount: 500,
          currency: "USD",
        })),
      },
      subscriptions: {
        update: subscriptionUpdate,
        cancelChangePlan,
        changePlan,
      },
    };
    const typedClient = testDodoClient(client);
    const provider = new DodoProvider({
      getClient: () => typedClient,
      webhookKey: "k",
      eventSink: sink,
    });
    const requestOptions = (key: string) => ({ headers: { "Idempotency-Key": key } });

    await provider.createCheckoutSession({
      accountId: "user-1",
      productId: "prod_checkout",
      type: "subscription",
      returnUrl: "https://return.test",
      cancelUrl: "https://cancel.test",
      metadata: {},
      idempotencyKey: "checkout:1",
    });
    await provider.cancelSubscription("sub_1", "cancel:1");
    await provider.reactivateSubscription("sub_1", "reactivate:1");
    await provider.cancelScheduledPlanChange("sub_1", null, "cancel-change:1");
    await provider.chargeSavedPaymentMethod({
      customerId: "cus_1",
      paymentMethodId: "pm_1",
      productId: "prod_charge",
      quantity: 1,
      metadata: {},
      idempotencyKey: "charge:1",
    });
    await provider.createCustomer({
      email: "user@example.com",
      name: "User",
      metadata: {},
      idempotencyKey: "customer:1",
    });
    await provider.changePlan({
      providerSubscriptionId: "sub_1",
      productId: "prod_plan",
      prorationBillingMode: "do_not_bill",
      idempotencyKey: "change-plan:1",
    });

    expect(checkoutCreate).toHaveBeenNthCalledWith(
      1,
      expect.any(Object),
      requestOptions("checkout:1"),
    );
    expect(subscriptionUpdate).toHaveBeenNthCalledWith(
      1,
      "sub_1",
      { cancel_at_next_billing_date: true },
      requestOptions("cancel:1"),
    );
    expect(subscriptionUpdate).toHaveBeenNthCalledWith(
      2,
      "sub_1",
      { cancel_at_next_billing_date: false },
      requestOptions("reactivate:1"),
    );
    expect(cancelChangePlan).toHaveBeenCalledWith("sub_1", requestOptions("cancel-change:1"));
    expect(checkoutCreate).toHaveBeenNthCalledWith(
      2,
      expect.any(Object),
      requestOptions("charge:1"),
    );
    expect(customerCreate).toHaveBeenCalledWith(expect.any(Object), requestOptions("customer:1"));
    expect(changePlan).toHaveBeenCalledWith(
      "sub_1",
      expect.any(Object),
      requestOptions("change-plan:1"),
    );
  });

  it("transports explicit idempotency headers through the installed Dodo SDK", async () => {
    const requests: Request[] = [];
    const fetchMock = vi.fn(
      async (input: string | URL | Request, init?: RequestInit): Promise<Response> => {
        requests.push(new Request(input, init));
        return new Response(
          JSON.stringify({
            checkout_url: "https://checkout.test",
            session_id: "sess_transport",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      },
    );
    const client = new DodoPayments({
      bearerToken: "test_token",
      baseURL: "https://api.test.invalid",
      fetch: fetchMock,
      maxRetries: 0,
    });

    await client.checkoutSessions.create(
      {
        product_cart: [{ product_id: "prod_1", quantity: 1 }],
        return_url: "https://return.test",
      },
      { headers: { "Idempotency-Key": "transport:1" } },
    );

    expect(fetchMock).toHaveBeenCalledOnce();
    expect(requests[0]?.headers.get("idempotency-key")).toBe("transport:1");
  });

  it("maps Stripe checkout calls and rejects missing webhook signatures", async () => {
    const calls: ExternalObject[] = [];
    const customerCalls: ExternalValue[][] = [];
    const checkoutCalls: ExternalValue[][] = [];
    const paymentIntentCalls: ExternalValue[][] = [];
    const stripe = {
      customers: {
        create: async (...args: ExternalValue[]) => {
          customerCalls.push(args);
          if (isExternalObject(args[0])) calls.push(args[0]);
          return { id: "cus_1" };
        },
        retrieve: async () => ({
          deleted: false,
          invoice_settings: { default_payment_method: "pm_1" },
        }),
        listPaymentMethods: async () => ({
          data: [
            {
              id: "pm_1",
              card: { last4: "4242", brand: "visa", exp_month: 1, exp_year: 2030 },
            },
            {
              id: "pm_1_duplicate",
              card: { last4: "4242", brand: "visa", exp_month: 1, exp_year: 2030 },
            },
          ],
        }),
      },
      checkout: {
        sessions: {
          create: async (...args: ExternalValue[]) => {
            checkoutCalls.push(args);
            return { id: "cs_1", url: "https://checkout.test" };
          },
          retrieve: async () => ({ status: "expired" }),
        },
      },
      billingPortal: { sessions: { create: async () => ({ url: "https://portal.test" }) } },
      prices: { retrieve: async () => ({ unit_amount: 500, currency: "usd" }) },
      paymentIntents: {
        create: async (...args: ExternalValue[]) => {
          paymentIntentCalls.push(args);
          return { id: "pi_auto", status: "succeeded", amount: 500, currency: "usd" };
        },
      },
      invoices: { retrieve: async () => ({ hosted_invoice_url: "https://invoice.test" }) },
      subscriptions: { update: async (...args: ExternalValue[]) => calls.push({ args }) },
      webhooks: { constructEvent: () => ({}) },
    };
    const typedStripe = testStripeClient(stripe);
    const provider = new StripeProvider({
      getClient: () => typedStripe,
      webhookSecret: "secret",
      eventSink: sink,
    });
    await expect(
      provider.createCheckoutSession({
        accountId: "u1",
        productId: "price_1",
        type: "subscription",
        returnUrl: "https://ok",
        cancelUrl: "https://cancel",
        metadata: {},
        idempotencyKey: "idem_1",
      }),
    ).resolves.toEqual({
      url: "https://checkout.test",
      customerId: "cus_1",
      providerSessionId: "cs_1",
    });
    expect(customerCalls[0]?.[1]).toEqual({ idempotencyKey: "idem_1:customer" });
    expect(customerCalls[0]?.[0]).toMatchObject({
      metadata: { bursar_account_id: "u1" },
    });
    expect(checkoutCalls[0]?.[0]).toMatchObject({
      line_items: [{ price: "price_1", quantity: 1 }],
      metadata: { bursar_account_id: "u1" },
      subscription_data: { metadata: { bursar_account_id: "u1" } },
    });
    expect(checkoutCalls[0]?.[1]).toEqual({ idempotencyKey: "idem_1" });
    await expect(
      provider.createCustomer({
        email: "u1@example.com",
        name: "User One",
        metadata: {},
        idempotencyKey: "customer:user-1",
      }),
    ).resolves.toEqual({ customerId: "cus_1" });
    expect(customerCalls.at(-1)?.[1]).toEqual({ idempotencyKey: "customer:user-1" });
    const longKeyPrefix = "operation:" + "x".repeat(244);
    for (const suffix of ["a", "b"]) {
      await provider.createCheckoutSession({
        accountId: "u1",
        productId: "price_1",
        type: "subscription",
        returnUrl: "https://ok",
        cancelUrl: "https://cancel",
        metadata: {},
        idempotencyKey: `${longKeyPrefix}${suffix}`,
      });
    }
    const scopedKeys = customerCalls
      .slice(-2)
      .map((call) => z.object({ idempotencyKey: z.string() }).parse(call[1]).idempotencyKey);
    expect(scopedKeys[0]).not.toBe(scopedKeys[1]);
    expect(scopedKeys.every((key) => key.length <= 255)).toBe(true);
    await expect(provider.getCheckoutSessionStatus("sess_1")).resolves.toEqual({
      paymentStatus: "cancelled",
    });
    await expect(provider.getInvoiceUrl("pay_1")).resolves.toEqual({ url: "https://invoice.test" });
    await expect(provider.listPaymentMethods("cus_1")).resolves.toHaveLength(1);
    await expect(
      provider.chargeSavedPaymentMethod({
        customerId: "cus_1",
        paymentMethodId: "pm_1",
        productId: "price_topup",
        quantity: 1,
        metadata: { purpose: "credit_topup" },
        idempotencyKey: "auto_1",
      }),
    ).resolves.toMatchObject({ providerPaymentId: "pi_auto", status: "succeeded" });
    expect(paymentIntentCalls[0]?.[0]).toMatchObject({
      metadata: { purpose: "credit_topup", price_id: "price_topup" },
    });
    await expect(provider.handleWebhook({ rawBody: "{}", headers: {} })).resolves.toEqual({
      received: false,
      retryable: false,
      provider: "stripe",
      eventId: null,
      eventType: null,
    });
  });

  it("uses Stripe's current checkout-status and subscription-schedule APIs", async () => {
    const subscriptionUpdate = vi.fn(async () => ({ latest_invoice: "in_1" }));
    const scheduleCreate = vi.fn(async () => ({
      id: "sub_sched_1",
      phases: [
        {
          items: [{ price: "price_old", quantity: 1 }],
          start_date: 1_767_225_600,
          end_date: 1_769_904_000,
        },
      ],
    }));
    const scheduleUpdate = vi.fn(async () => ({}));
    const stripe = {
      checkout: {
        sessions: {
          retrieve: async (id: string) => {
            if (id === "cs_open") return { status: "open", payment_status: "unpaid" };
            return {
              status: "complete",
              payment_status: "unpaid",
              payment_intent: { status: "requires_payment_method" },
            };
          },
        },
      },
      subscriptions: {
        retrieve: async () => ({
          customer: "cus_1",
          items: {
            data: [
              {
                id: "si_1",
                current_period_start: 1_767_225_600,
                current_period_end: 1_769_904_000,
              },
            ],
          },
        }),
        update: subscriptionUpdate,
      },
      subscriptionSchedules: {
        create: scheduleCreate,
        update: scheduleUpdate,
      },
    };
    const typedStripe = testStripeClient(stripe);
    const provider = new StripeProvider({
      getClient: () => typedStripe,
      webhookSecret: "secret",
      eventSink: sink,
    });

    await expect(provider.getCheckoutSessionStatus("cs_open")).resolves.toEqual({
      paymentStatus: "processing",
    });
    await expect(provider.getCheckoutSessionStatus("cs_requires_method")).resolves.toEqual({
      paymentStatus: "requires_payment_method",
    });

    await expect(
      provider.changePlan({
        providerSubscriptionId: "sub_1",
        productId: "price_new",
        prorationBillingMode: "do_not_bill",
        effectiveAt: "next_billing_date",
        metadata: { plan: "pro" },
        idempotencyKey: "plan_1",
      }),
    ).resolves.toEqual({ providerOperationId: "sub_sched_1" });
    expect(scheduleCreate).toHaveBeenCalledWith(
      { from_subscription: "sub_1" },
      { idempotencyKey: "plan_1:schedule-create" },
    );
    expect(scheduleUpdate).toHaveBeenCalledWith(
      "sub_sched_1",
      expect.objectContaining({
        phases: [
          expect.objectContaining({
            items: [{ price: "price_old", quantity: 1 }],
            end_date: 1_769_904_000,
          }),
          expect.objectContaining({
            items: [{ price: "price_new", quantity: 1 }],
            start_date: 1_769_904_000,
            metadata: { plan: "pro" },
          }),
        ],
      }),
      { idempotencyKey: "plan_1:schedule-update" },
    );

    await provider.changePlan({
      providerSubscriptionId: "sub_1",
      productId: "price_now",
      prorationBillingMode: "do_not_bill",
      effectiveAt: "immediately",
      onPaymentFailure: "apply_change",
      metadata: { plan: "team" },
      idempotencyKey: "plan_2",
    });
    expect(subscriptionUpdate).toHaveBeenCalledWith(
      "sub_1",
      expect.objectContaining({
        items: [{ id: "si_1", price: "price_now", quantity: 1 }],
        proration_behavior: "none",
        payment_behavior: "allow_incomplete",
        metadata: { plan: "team" },
      }),
      { idempotencyKey: "plan_2:subscription-update" },
    );
  });

  it("uses the Dodo SDK's typed not-found error", async () => {
    const client = {
      webhooks: {
        unwrap: () => ({ type: "payment.succeeded", data: {} }),
      },
      checkoutSessions: {
        retrieve: async () => {
          throw new NotFoundError(404, {}, "missing", new Headers());
        },
      },
    };
    const typedClient = testDodoClient(client);
    const provider = new DodoProvider({
      getClient: () => typedClient,
      webhookKey: "k",
      eventSink: sink,
    });
    await expect(provider.getCheckoutSessionStatus("missing")).resolves.toBeNull();
  });

  it("does not reinterpret arbitrary provider error shapes", async () => {
    const error = { statusCode: 404 };
    const client = {
      webhooks: {
        unwrap: () => ({ type: "payment.succeeded", data: {} }),
      },
      checkoutSessions: {
        retrieve: async () => {
          throw error;
        },
      },
    };
    const typedClient = testDodoClient(client);
    const provider = new DodoProvider({
      getClient: () => typedClient,
      webhookKey: "k",
      eventSink: sink,
    });
    await expect(provider.getCheckoutSessionStatus("missing")).rejects.toBe(error);
  });

  it("keeps the mock provider deterministic and complete", async () => {
    const provider = new MockPaymentProvider({ eventSink: sink });
    await expect(
      provider.createCheckoutSession({
        accountId: "user-1",
        productId: "product-1",
        type: "credit_pack",
        returnUrl: "https://return",
        cancelUrl: "https://cancel",
        metadata: {},
        idempotencyKey: "checkout_1",
      }),
    ).resolves.toEqual({ url: "https://return" });
    await expect(
      provider.createCustomerPortalSession({ customerId: "cus_1", returnUrl: "https://portal" }),
    ).resolves.toEqual({ url: "https://portal" });
    await expect(
      provider.createUpdatePaymentMethodSession({
        customerId: "cus_1",
        subscriptionId: "sub_1",
        returnUrl: "https://update",
      }),
    ).resolves.toEqual({ url: "https://update" });
    await expect(
      provider.createPaymentMethodSetupSession({
        customerId: "cus_1",
        returnUrl: "https://setup",
      }),
    ).resolves.toEqual({ url: "https://setup" });
    await expect(provider.getInvoiceUrl("pay_1")).resolves.toEqual({
      url: "https://example.com/invoice",
    });
    await expect(provider.listPaymentMethods("cus_1")).resolves.toEqual([]);
    await expect(
      provider.previewSavedPaymentCharge({
        customerId: "cus_1",
        paymentMethodId: "pm_1",
        productId: "prod_1",
        quantity: 1,
        metadata: {},
        idempotencyKey: "preview_1",
      }),
    ).resolves.toEqual({ amountMinor: 0, currency: "USD" });
    await expect(
      provider.chargeSavedPaymentMethod({
        customerId: "cus_1",
        paymentMethodId: "pm_1",
        productId: "prod_1",
        quantity: 1,
        metadata: {},
        idempotencyKey: "topup_1",
      }),
    ).resolves.toMatchObject({ providerPaymentId: "mock_pay_topup_1", status: "succeeded" });
    await expect(
      provider.createCustomer({
        email: "u1@example.com",
        name: "User One",
        metadata: {},
        idempotencyKey: "customer:user-1",
      }),
    ).resolves.toMatchObject({ customerId: expect.stringMatching(/^mock_cus_/) });
    await expect(
      provider.changePlan({
        providerSubscriptionId: "sub_1",
        productId: "prod_2",
        prorationBillingMode: "prorated_immediately",
        idempotencyKey: "change_1",
      }),
    ).resolves.toBeUndefined();
    await expect(
      provider.previewChangePlan({
        providerSubscriptionId: "sub_1",
        productId: "prod_2",
        prorationBillingMode: "prorated_immediately",
      }),
    ).resolves.toMatchObject({ totalAmount: 0, settlementAmount: 0, currency: "USD" });
    await expect(provider.cancelSubscription("sub_1", "cancel_1")).resolves.toBeUndefined();
    await expect(provider.reactivateSubscription("sub_1", "reactivate_1")).resolves.toBeUndefined();
    await expect(
      provider.cancelScheduledPlanChange("sub_1", "op_1", "idem_1"),
    ).resolves.toBeUndefined();
    await expect(provider.handleWebhook({ rawBody: "not-json", headers: {} })).resolves.toEqual({
      received: false,
      retryable: false,
      provider: "mock",
      eventId: null,
      eventType: null,
    });
  });
});
