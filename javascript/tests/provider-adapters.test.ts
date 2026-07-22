import { describe, expect, it } from "vitest";
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
    const stripe: any = {
      customers: {
        create: async (args: Record<string, unknown>) => {
          calls.push(args);
          return { id: "cus_1" };
        },
      },
      checkout: {
        sessions: {
          create: async (args: Record<string, unknown>) => {
            calls.push(args);
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
    expect(calls[1]).toMatchObject({
      line_items: [{ price: "price_1", quantity: 1 }],
      idempotencyKey: "idem_1",
    });
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
    });
  });

  it.each(["status", "statusCode", "status_code"])(
    "contains Dodo SDK status aliases at the provider boundary (%s)",
    async (key) => {
      const client = {
        checkoutSessions: {
          retrieve: async () => {
            throw { [key]: 404 };
          },
        },
      } as any;
      const provider = new DodoProvider(() => client, { webhookKey: "k" }, sink);
      await expect(provider.getCheckoutSessionStatus("missing")).resolves.toBeNull();
    },
  );

  it("keeps the mock provider deterministic and complete", async () => {
    const provider = new MockPaymentProvider(sink);
    await expect(provider.createCheckoutSession({ returnUrl: "https://return" })).resolves.toEqual({
      url: "https://return",
    });
    await expect(
      provider.createCustomerPortalSession({ returnUrl: "https://portal" }),
    ).resolves.toEqual({ url: "https://portal" });
    await expect(provider.getInvoiceUrl("pay_1")).resolves.toEqual({
      url: "https://example.com/invoice",
    });
    await expect(provider.handleWebhook({ rawBody: "not-json", headers: {} })).resolves.toEqual({
      received: false,
      retryable: false,
    });
  });
});
