import { describe, expect, it } from "vitest";
import { handleStripeWebhook } from "../src/providers/stripe/event-mapper.js";

const fakeStripe: any = {
  checkout: { sessions: { retrieve: async () => ({ line_items: { data: [] } }) } },
  subscriptions: {
    retrieve: async () => ({ id: "sub_1", status: "active", metadata: { userId: "u1" } }),
  },
};

function sink() {
  const events: any[] = [];
  return {
    events,
    ingestBillingEvent: (event: any) => {
      events.push(event);
      return { handled: true };
    },
  };
}

const event = (type: string, object: unknown) =>
  ({ id: `evt_${type}`, type, data: { object } }) as any;

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
    expect(target.events[0].eventType).toBe("checkout.completed");
  });
});
