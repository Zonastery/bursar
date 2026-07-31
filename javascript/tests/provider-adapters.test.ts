import { describe, expect, it } from "vitest";
import { NotFoundError } from "dodopayments";
import { DodoProvider } from "../src/providers/dodo/provider.js";
import { MockPaymentProvider } from "../src/providers/mock/provider.js";
import { StripeProvider } from "../src/providers/stripe/provider.js";

const sink = { ingestBillingEvent: () => ({ handled: true, action: "ok" }) };

describe("payment provider adapter contracts", () => {
  it("maps Dodo requests, idempotency, and response DTOs", async () => {
    const calls: unknown[][] = [];
    const client: any = {
      checkoutSessions: {
        create: async (...args: unknown[]) => {
          calls.push(args);
          if ((args[0] as Record<string, unknown>)?.confirm === true) {
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
        create: async () => ({ customer_id: "cus_1" }),
      },
      payments: {
        retrieve: async (id: string) =>
          id === "pay_auto"
            ? { payment_id: id, status: "succeeded", total_amount: 500, currency: "USD" }
            : { payment_link: "https://invoice.test" },
      },
      subscriptions: {
        update: async (...args: unknown[]) => calls.push(args),
        changePlan: async (...args: unknown[]) => calls.push(args),
        previewChangePlan: async () => ({
          immediate_charge: {
            line_items: [],
            summary: { total_amount: 12, settlement_amount: 10, settlement_currency: "USD" },
            effective_at: "2026-01-01T00:00:00Z",
          },
        }),
      },
    };
    const provider = new DodoProvider(
      () => client,
      { webhookKey: "k", setupProductId: "setup" },
      sink,
    );
    await expect(
      provider.createCheckoutSession({
        productId: "prod_1",
        returnUrl: "https://return",
        quantity: 2,
        idempotencyKey: "idem_1",
      }),
    ).resolves.toEqual({ url: "https://checkout.test", providerSessionId: "sess_1" });
    expect(calls[0]).toEqual([
      {
        product_cart: [{ product_id: "prod_1", quantity: 2 }],
        customer: undefined,
        return_url: "https://return",
        cancel_url: undefined,
        metadata: undefined,
      },
      { idempotencyKey: "idem_1" },
    ]);
    await expect(provider.getCheckoutSessionStatus("sess_1")).resolves.toEqual({
      paymentStatus: "paid",
    });
    await expect(
      provider.createCustomerPortalSession({ customerId: "cus_1", returnUrl: "https://return" }),
    ).resolves.toEqual({ url: "https://portal.test" });
    await expect(provider.listPaymentMethods("cus_1")).resolves.toEqual([
      {
        id: "pm_1",
        last4: "4242",
        brand: "visa",
        expiryMonth: 1,
        expiryYear: 2030,
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
    await expect(provider.getInvoiceUrl("pay_1")).resolves.toEqual({ url: "https://invoice.test" });
    await expect(
      provider.previewChangePlan({ providerSubscriptionId: "sub_1", productId: "prod_2" }),
    ).resolves.toMatchObject({ totalAmount: 12, settlementAmount: 10 });
  });

  it("maps Stripe checkout calls and rejects missing webhook signatures", async () => {
    const calls: Record<string, unknown>[] = [];
    const checkoutCalls: unknown[][] = [];
    const stripe: any = {
      customers: {
        create: async (args: Record<string, unknown>) => {
          calls.push(args);
          return { id: "cus_1" };
        },
      },
      checkout: {
        sessions: {
          create: async (...args: unknown[]) => {
            checkoutCalls.push(args);
            return { url: "https://checkout.test" };
          },
          retrieve: async () => ({ status: "expired" }),
        },
      },
      billingPortal: { sessions: { create: async () => ({ url: "https://portal.test" }) } },
      paymentMethods: {
        list: async () => ({
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
      prices: { retrieve: async () => ({ unit_amount: 500, currency: "usd" }) },
      paymentIntents: {
        create: async () => ({ id: "pi_auto", status: "succeeded", amount: 500, currency: "usd" }),
      },
      invoices: { retrieve: async () => ({ hosted_invoice_url: "https://invoice.test" }) },
      subscriptions: { update: async (...args: unknown[]) => calls.push({ args }) },
      webhooks: { constructEvent: () => ({}) },
    };
    const provider = new StripeProvider(() => stripe, sink, "secret");
    await expect(
      provider.createCheckoutSession({
        userId: "u1",
        productId: "price_1",
        returnUrl: "https://ok",
        cancelUrl: "https://cancel",
        idempotencyKey: "idem_1",
      }),
    ).resolves.toEqual({ url: "https://checkout.test", customerId: "cus_1" });
    expect(checkoutCalls[0]?.[0]).toMatchObject({
      line_items: [{ price: "price_1", quantity: 1 }],
    });
    expect(checkoutCalls[0]?.[1]).toEqual({ idempotencyKey: "idem_1" });
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
    await expect(provider.handleWebhook({ rawBody: "{}", headers: {} })).resolves.toEqual({
      received: false,
      retryable: false,
      provider: "stripe",
      eventId: null,
      eventType: null,
    });
  });

  it("uses the Dodo SDK's typed not-found error", async () => {
    const client = {
      checkoutSessions: {
        retrieve: async () => {
          throw new NotFoundError(404, {}, "missing", new Headers());
        },
      },
    } as any;
    const provider = new DodoProvider(() => client, { webhookKey: "k" }, sink);
    await expect(provider.getCheckoutSessionStatus("missing")).resolves.toBeNull();
  });

  it("does not reinterpret arbitrary provider error shapes", async () => {
    const error = { statusCode: 404 };
    const client = {
      checkoutSessions: {
        retrieve: async () => {
          throw error;
        },
      },
    } as any;
    const provider = new DodoProvider(() => client, { webhookKey: "k" }, sink);
    await expect(provider.getCheckoutSessionStatus("missing")).rejects.toBe(error);
  });

  it("keeps the mock provider deterministic and complete", async () => {
    const provider = new MockPaymentProvider(sink);
    await expect(provider.createCheckoutSession({ returnUrl: "https://return" })).resolves.toEqual({
      url: "https://return",
    });
    await expect(
      provider.createCustomerPortalSession({ returnUrl: "https://portal" }),
    ).resolves.toEqual({ url: "https://portal" });
    await expect(
      provider.createUpdatePaymentMethodSession({ returnUrl: "https://update" }),
    ).resolves.toEqual({ url: "https://update" });
    await expect(
      provider.createPaymentMethodSetupSession({ returnUrl: "https://setup" }),
    ).resolves.toEqual({ url: "https://setup" });
    await expect(provider.getInvoiceUrl("pay_1")).resolves.toEqual({
      url: "https://example.com/invoice",
    });
    await expect(provider.listPaymentMethods("cus_1")).resolves.toEqual([]);
    await expect(provider.getDefaultPaymentMethod("cus_1")).resolves.toBeNull();
    await expect(
      provider.previewSavedPaymentCharge({
        customerId: "cus_1",
        paymentMethodId: "pm_1",
        productId: "prod_1",
        quantity: 1,
      }),
    ).resolves.toEqual({ amountMinor: 0, currency: "USD" });
    await expect(
      provider.chargeSavedPaymentMethod({
        customerId: "cus_1",
        paymentMethodId: "pm_1",
        productId: "prod_1",
        quantity: 1,
        idempotencyKey: "topup_1",
      }),
    ).resolves.toMatchObject({ providerPaymentId: "mock_pay_topup_1", status: "succeeded" });
    await expect(
      provider.createCustomer({ userId: "u1", email: "u1@example.com" }),
    ).resolves.toMatchObject({ customerId: expect.stringMatching(/^mock_cus_/) });
    await expect(
      provider.changePlan({ providerSubscriptionId: "sub_1", productId: "prod_2" }),
    ).resolves.toBeUndefined();
    await expect(
      provider.previewChangePlan({ providerSubscriptionId: "sub_1", productId: "prod_2" }),
    ).resolves.toMatchObject({ totalAmount: 0, settlementAmount: 0, currency: "USD" });
    await expect(provider.cancelSubscription("sub_1")).resolves.toBeUndefined();
    await expect(provider.reactivateSubscription("sub_1")).resolves.toBeUndefined();
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
