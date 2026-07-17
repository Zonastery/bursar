import { describe, expect, it, vi } from "vitest";
import { handleDodoBillingEvent } from "../src/providers/dodo/event-mapper.js";
import type { BillingEventSink } from "../src/bursar.js";

describe("Dodo subscription event mapping", () => {
  it("normalizes cadence and product metadata for active subscriptions", async () => {
    const sink = {
      ingestBillingEvent: vi.fn().mockResolvedValue({ handled: true }),
    } as unknown as BillingEventSink;

    await handleDodoBillingEvent(
      "subscription.active",
      {
        id: "evt_1",
        subscription_id: "sub_1",
        product_id: "prod_yearly",
        status: "active",
        payment_frequency_interval: "Year",
        payment_frequency_count: 1,
        previous_billing_date: "2026-07-17T00:00:00Z",
        next_billing_date: "2027-07-17T00:00:00Z",
      },
      "user_1",
      { plan_slug: "sage", billing_interval: "year" },
      sink,
    );

    expect(sink.ingestBillingEvent).toHaveBeenCalledWith(
      expect.objectContaining({
        eventType: "subscription.created",
        subscription: expect.objectContaining({
          providerSubscriptionId: "sub_1",
          interval: "year",
          intervalCount: 1,
          periodStart: "2026-07-17T00:00:00Z",
          refs: { productId: "prod_yearly" },
        }),
      }),
    );
  });
});
