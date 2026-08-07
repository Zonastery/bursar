import { describe, expect, it } from "vitest";
import type Stripe from "stripe";
import type { BillingEventSink } from "../src/billing/contracts.js";
import type { BillingEvent } from "../src/billing/types/index.js";
import { handleStripeWebhook } from "../src/providers/stripe/event-mapper.js";

const fakeStripe = {
  checkout: { sessions: { retrieve: async () => ({ line_items: { data: [] } }) } },
  subscriptions: {
    retrieve: async () => ({ id: "sub_1", status: "active", metadata: { userId: "u1" } }),
  },
} as unknown as Stripe;

function sink(): BillingEventSink & { events: BillingEvent[] } {
  const events: BillingEvent[] = [];
  return {
    events,
    ingestBillingEvent: async (event: BillingEvent) => {
      events.push(event);
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
  it("maps subscription update, cancellation, and invoice events", async () => {
    const target = sink();
    await handleStripeWebhook(
      event("customer.subscription.updated", {
        id: "sub_1",
        customer: "cus_1",
        status: "active",
        metadata: { userId: "u1" },
        current_period_end: 1767225600,
      }),
      target,
      fakeStripe,
    );
    await handleStripeWebhook(
      event("customer.subscription.deleted", { id: "sub_1", customer: "cus_1" }),
      target,
      fakeStripe,
    );
    await handleStripeWebhook(
      event("invoice.paid", {
        id: "in_1",
        subscription: "sub_1",
        customer: "cus_1",
        metadata: { userId: "u1" },
        amount_paid: 1000,
        amount_due: 1000,
        currency: "usd",
      }),
      target,
      fakeStripe,
    );
    expect(target.events.map((item) => item.eventType)).toEqual([
      "subscription.updated",
      "subscription.canceled",
      "invoice.paid",
    ]);
  });

  it("maps checkout subscriptions and ignores missing-user/unknown events", async () => {
    const target = sink();
    await handleStripeWebhook(
      event("checkout.session.completed", {
        id: "cs_1",
        client_reference_id: "u1",
        mode: "subscription",
        subscription: "sub_1",
        customer: "cus_1",
        metadata: { plan_slug: "pro" },
      }),
      target,
      fakeStripe,
    );
    await handleStripeWebhook(
      event("checkout.session.completed", { id: "cs_2", mode: "payment" }),
      target,
      fakeStripe,
    );
    await handleStripeWebhook(event("charge.succeeded", {}), target, fakeStripe);
    expect(target.events).toHaveLength(1);
    expect(target.events[0]!.eventType).toBe("checkout.completed");
  });
});
