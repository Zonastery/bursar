import { describe, expect, it, vi } from "vitest";
import type Stripe from "stripe";
import type { BillingEventSink } from "../src/billing/contracts.js";
import type { BillingEvent } from "../src/billing/types/index.js";
import { handleStripeWebhook } from "../src/providers/stripe/event-mapper.js";

function subscriptionFixture() {
  return {
    id: "sub_1",
    customer: "cus_1",
    status: "active",
    cancel_at_period_end: false,
    metadata: { userId: "u1" },
    trial_end: null,
    cancel_at: null,
    ended_at: null,
    items: {
      data: [
        {
          id: "si_1",
          current_period_start: 1_764_547_200,
          current_period_end: 1_767_225_600,
          price: { id: "price_pro", product: "prod_pro" },
        },
      ],
    },
  };
}

function fakeStripe(options?: {
  checkoutRetrieve?: (id: string) => Promise<unknown>;
  subscriptionRetrieve?: (id: string) => Promise<unknown>;
}): Stripe {
  return {
    checkout: {
      sessions: {
        retrieve:
          options?.checkoutRetrieve ??
          (async () => ({
            line_items: {
              data: [{ price: { id: "price_topup", product: "prod_topup" } }],
            },
          })),
      },
    },
    subscriptions: {
      retrieve: options?.subscriptionRetrieve ?? (async () => subscriptionFixture()),
    },
  } as unknown as Stripe;
}

function sink(): BillingEventSink & { events: BillingEvent[] } {
  const events: BillingEvent[] = [];
  return {
    events,
    ingestBillingEvent: async (billingEvent: BillingEvent) => {
      events.push(billingEvent);
      return { handled: true };
    },
  };
}

const event = (type: string, object: unknown) =>
  ({
    id: `evt_${type}`,
    type,
    created: 1_767_225_600,
    data: { object },
  }) as unknown as Stripe.Event;

describe("Stripe webhook mapper", () => {
  it("maps current subscription periods and invoice parent references", async () => {
    const target = sink();
    const subscription = subscriptionFixture();
    const stripe = fakeStripe();

    await handleStripeWebhook(event("customer.subscription.updated", subscription), target, stripe);
    await handleStripeWebhook(
      event("customer.subscription.deleted", {
        ...subscription,
        status: "canceled",
        ended_at: 1_767_225_600,
      }),
      target,
      stripe,
    );
    await handleStripeWebhook(
      event("invoice.paid", {
        id: "in_1",
        parent: {
          type: "subscription_details",
          subscription_details: {
            subscription: "sub_1",
            metadata: { userId: "u1", source: "subscription" },
          },
        },
        customer: "cus_1",
        metadata: { invoice_key: "invoice_value" },
        amount_paid: 1000,
        amount_due: 1000,
        currency: "usd",
        period_start: 1_764_547_200,
        period_end: 1_767_225_600,
      }),
      target,
      stripe,
    );

    expect(target.events.map((item) => item.eventType)).toEqual([
      "subscription.updated",
      "subscription.canceled",
      "invoice.paid",
    ]);
    expect(target.events[0]?.subscription).toMatchObject({
      periodStart: new Date(1_764_547_200 * 1000).toISOString(),
      periodEnd: new Date(1_767_225_600 * 1000).toISOString(),
      refs: { priceId: "price_pro", productId: "prod_pro" },
    });
    expect(target.events[2]).toMatchObject({
      userId: "u1",
      metadata: {
        userId: "u1",
        source: "subscription",
        invoice_key: "invoice_value",
      },
      invoice: { providerInvoiceId: "in_1", currency: "USD" },
    });
  });

  it("waits for delayed checkout payment events and separates tax from subtotal", async () => {
    const target = sink();
    const checkoutRetrieve = vi.fn(async () => ({
      line_items: {
        data: [{ price: { id: "price_topup", product: "prod_topup" } }],
      },
    }));
    const stripe = fakeStripe({ checkoutRetrieve });
    const session = {
      id: "cs_1",
      client_reference_id: "u1",
      mode: "payment",
      customer: "cus_1",
      customer_details: { email: "u1@example.com" },
      payment_intent: "pi_1",
      amount_subtotal: 1000,
      amount_total: 1180,
      total_details: { amount_tax: 180 },
      currency: "usd",
      metadata: { checkout_intent_id: "intent_1" },
    };

    await handleStripeWebhook(
      event("checkout.session.completed", { ...session, payment_status: "unpaid" }),
      target,
      stripe,
    );
    expect(target.events).toEqual([]);
    expect(checkoutRetrieve).not.toHaveBeenCalled();

    await handleStripeWebhook(
      event("checkout.session.async_payment_succeeded", {
        ...session,
        payment_status: "paid",
      }),
      target,
      stripe,
    );
    expect(target.events[0]).toMatchObject({
      eventType: "payment.succeeded",
      userId: "u1",
      customer: { providerCustomerId: "cus_1", email: "u1@example.com" },
      metadata: { checkout_intent_id: "intent_1" },
      payment: {
        providerPaymentId: "pi_1",
        amountMinor: 1000,
        taxMinor: 180,
        currency: "USD",
        status: "succeeded",
        refs: { priceId: "price_topup", productId: "prod_topup" },
      },
    });

    await handleStripeWebhook(
      event("checkout.session.async_payment_failed", {
        ...session,
        id: "cs_2",
        payment_intent: "pi_2",
        payment_status: "unpaid",
      }),
      target,
      stripe,
    );
    expect(target.events[1]).toMatchObject({
      eventType: "payment.failed",
      payment: { providerPaymentId: "pi_2", status: "failed" },
    });
  });

  it("maps subscription checkout and expiration with current object shapes", async () => {
    const target = sink();
    const stripe = fakeStripe();

    await handleStripeWebhook(
      event("checkout.session.completed", {
        id: "cs_sub",
        client_reference_id: "u1",
        mode: "subscription",
        payment_status: "paid",
        subscription: "sub_1",
        customer: "cus_1",
        metadata: { plan_slug: "pro", checkout_intent_id: "intent_sub" },
      }),
      target,
      stripe,
    );
    await handleStripeWebhook(
      event("checkout.session.expired", {
        id: "cs_expired",
        client_reference_id: "u1",
        customer: "cus_1",
        metadata: { checkout_intent_id: "intent_expired" },
      }),
      target,
      stripe,
    );

    expect(target.events[0]).toMatchObject({
      eventType: "checkout.completed",
      metadata: { plan_slug: "pro", checkout_intent_id: "intent_sub" },
      subscription: {
        providerSubscriptionId: "sub_1",
        periodStart: new Date(1_764_547_200 * 1000).toISOString(),
        periodEnd: new Date(1_767_225_600 * 1000).toISOString(),
        refs: { lookupKey: "pro" },
      },
    });
    expect(target.events[1]).toMatchObject({
      eventType: "checkout.expired",
      metadata: { checkout_intent_id: "intent_expired" },
    });
  });

  it("maps failed invoices to canonical failed payments", async () => {
    const target = sink();
    await handleStripeWebhook(
      event("invoice.payment_failed", {
        id: "in_failed",
        parent: {
          type: "subscription_details",
          subscription_details: {
            subscription: "sub_1",
            metadata: { userId: "u1" },
          },
        },
        customer: "cus_1",
        subtotal: 1000,
        total_taxes: [{ amount: 180 }],
        currency: "usd",
        payments: {
          data: [{ payment: { type: "payment_intent", payment_intent: "pi_failed" } }],
        },
      }),
      target,
      fakeStripe(),
    );

    expect(target.events[0]).toMatchObject({
      eventType: "payment.failed",
      payment: {
        providerPaymentId: "pi_failed",
        amountMinor: 1000,
        taxMinor: 180,
        currency: "USD",
        status: "failed",
        refs: { priceId: "price_pro", productId: "prod_pro" },
      },
    });
  });

  it("propagates transient Stripe retrieval failures for webhook retries", async () => {
    const target = sink();
    const error = new Error("Stripe temporarily unavailable");
    const stripe = fakeStripe({
      checkoutRetrieve: async () => Promise.reject(error),
    });

    await expect(
      handleStripeWebhook(
        event("checkout.session.completed", {
          id: "cs_retry",
          mode: "payment",
          payment_status: "paid",
          payment_intent: "pi_retry",
          amount_subtotal: 1000,
          amount_total: 1000,
          currency: "usd",
        }),
        target,
        stripe,
      ),
    ).rejects.toBe(error);
    expect(target.events).toEqual([]);
  });

  it("ignores unsupported Stripe events", async () => {
    const target = sink();
    await handleStripeWebhook(event("charge.succeeded", {}), target, fakeStripe());
    expect(target.events).toEqual([]);
  });
});
