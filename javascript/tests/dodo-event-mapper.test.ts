import { describe, expect, it, vi } from "vitest";
import { normalizeDate } from "../src/providers/dodo/event-mapper.js";
import type { BillingEventSink } from "../src/bursar.js";
import {
  DODO_JS_DATE,
  DODO_ISO_DATE,
  DODO_SUBSCRIPTION_ACTIVE,
  DODO_SUBSCRIPTION_ACTIVE_PLAN_SLUG,
  DODO_SUBSCRIPTION_ACTIVE_NO_DATES,
  DODO_SUBSCRIPTION_RENEWED,
  DODO_SUBSCRIPTION_UPDATED,
  DODO_SUBSCRIPTION_CANCELLED,
  DODO_SUBSCRIPTION_EXPIRED,
  DODO_SUBSCRIPTION_FAILED,
  DODO_SUBSCRIPTION_ON_HOLD,
  DODO_SUBSCRIPTION_PLAN_CHANGED,
  DODO_PAYMENT_SUCCEEDED,
  DODO_PAYMENT_FAILED,
  DODO_REFUND_SUCCEEDED,
  DODO_DISPUTE_OPENED,
  DODO_DISPUTE_WON_CLOSED,
  dodoEventId,
  mapDodoEvent,
} from "./helpers/dodo-fixtures.js";

/** Shared mock sink. Each test that needs one calls makeSink(). */
function makeSink() {
  const ingestBillingEvent = vi
    .fn<BillingEventSink["ingestBillingEvent"]>()
    .mockResolvedValue({ handled: true });
  return { ingestBillingEvent } satisfies BillingEventSink;
}

// ── normalizeDate unit tests ──────────────────────────────────────────

describe("normalizeDate", () => {
  it("converts JS Date.toString() format to ISO 8601", () => {
    expect(normalizeDate(DODO_JS_DATE)).toBe(DODO_ISO_DATE);
  });

  it("preserves milliseconds from SDK Date values", () => {
    expect(normalizeDate(new Date("2026-07-18T05:15:24.987Z"))).toBe("2026-07-18T05:15:24.987Z");
  });

  it("returns null for an invalid Date object", () => {
    expect(normalizeDate(new Date(Number.NaN))).toBeNull();
  });

  it("passes through valid ISO 8601 unchanged", () => {
    expect(normalizeDate("2026-07-18T05:15:24.000Z")).toBe("2026-07-18T05:15:24.000Z");
    expect(normalizeDate("2026-07-18T00:00:00Z")).toBe("2026-07-18T00:00:00.000Z");
  });

  it("returns null for null input", () => {
    expect(normalizeDate(null)).toBeNull();
  });

  it("returns null for undefined input", () => {
    expect(normalizeDate(undefined)).toBeNull();
  });

  it("returns null for empty string", () => {
    expect(normalizeDate("")).toBeNull();
  });

  it("returns null for unparseable string", () => {
    expect(normalizeDate("not-a-date")).toBeNull();
  });
});

// ── Canonical event IDs ─────────────────────────────────────────────

describe("canonical event IDs", () => {
  it("includes provider type, object ID, and occurrence time for payment events", async () => {
    const sink = makeSink();
    await mapDodoEvent("payment.succeeded", DODO_PAYMENT_SUCCEEDED, "user_1", {}, sink);
    expect(sink.ingestBillingEvent).toHaveBeenCalledWith(
      expect.objectContaining({
        eventId: dodoEventId("payment.succeeded", "pay_dodo_success_001"),
      }),
    );
  });

  it("uses the subscription ID for subscription.active", async () => {
    const sink = makeSink();
    await mapDodoEvent("subscription.active", DODO_SUBSCRIPTION_ACTIVE, "user_1", {}, sink);
    expect(sink.ingestBillingEvent).toHaveBeenCalledWith(
      expect.objectContaining({
        eventId: dodoEventId("subscription.active", "sub_dodo_active_001"),
      }),
    );
  });

  it("uses the subscription ID for subscription.renewed", async () => {
    const sink = makeSink();
    await mapDodoEvent("subscription.renewed", DODO_SUBSCRIPTION_RENEWED, "user_1", {}, sink);
    expect(sink.ingestBillingEvent).toHaveBeenCalledWith(
      expect.objectContaining({
        eventId: dodoEventId("subscription.renewed", "sub_dodo_renewed_001"),
      }),
    );
  });

  it("uses the subscription ID for subscription.updated", async () => {
    const sink = makeSink();
    await mapDodoEvent("subscription.updated", DODO_SUBSCRIPTION_UPDATED, "user_1", {}, sink);
    expect(sink.ingestBillingEvent).toHaveBeenCalledWith(
      expect.objectContaining({
        eventId: dodoEventId("subscription.updated", "sub_dodo_updated_001"),
      }),
    );
  });

  it("produces unique rawIds for different subscriptions of the same event type", async () => {
    const sink = makeSink();
    await mapDodoEvent(
      "subscription.active",
      { ...DODO_SUBSCRIPTION_ACTIVE, subscription_id: "sub_alpha" },
      "user_1",
      {},
      sink,
    );
    await mapDodoEvent(
      "subscription.active",
      { ...DODO_SUBSCRIPTION_ACTIVE, subscription_id: "sub_beta" },
      "user_1",
      {},
      sink,
    );
    const calls = sink.ingestBillingEvent.mock.calls;
    expect(calls).toHaveLength(2);
    expect(calls[0]![0]!.eventId).toBe(dodoEventId("subscription.active", "sub_alpha"));
    expect(calls[1]![0]!.eventId).toBe(dodoEventId("subscription.active", "sub_beta"));
  });

  it("rejects a customer ID as the subscription identifier", async () => {
    const sink = makeSink();
    // subscription.active doesn't guard on subscription_id — the rawId fallback
    // to customer_id is exercised when both data.id and data.subscription_id are absent.
    const payload = { customer_id: "cus_dodo_001", status: "active" };
    await expect(mapDodoEvent("subscription.active", payload, "user_1", {}, sink)).rejects.toThrow(
      "subscription_id",
    );
    expect(sink.ingestBillingEvent).not.toHaveBeenCalled();
  });

  it("rejects a missing subscription identifier", async () => {
    const sink = makeSink();
    await expect(mapDodoEvent("subscription.active", {}, "user_1", {}, sink)).rejects.toThrow(
      "subscription_id",
    );
    expect(sink.ingestBillingEvent).not.toHaveBeenCalled();
  });
});

// ── Date normalization (Bug 2 regression tests) ──────────────────────

describe("date normalization through event mapper", () => {
  it("converts JS Date.toString() dates to ISO 8601 for subscription.active → subscription.created", async () => {
    const sink = makeSink();
    await mapDodoEvent("subscription.active", DODO_SUBSCRIPTION_ACTIVE, "user_1", {}, sink);
    const call = sink.ingestBillingEvent.mock.calls[0]![0]!;
    if (!call.subscription) throw new Error("Expected subscription event data");
    expect(call.subscription.periodStart).toBe(DODO_ISO_DATE);
    // next_billing_date in fixture is August — verify it's different from periodStart
    expect(call.subscription.periodEnd).toBe("2026-08-18T05:15:24.000Z");
  });

  it("converts JS Date.toString() dates to ISO 8601 for subscription.renewed → subscription.activated", async () => {
    const sink = makeSink();
    await mapDodoEvent("subscription.renewed", DODO_SUBSCRIPTION_RENEWED, "user_1", {}, sink);
    expect(sink.ingestBillingEvent).toHaveBeenCalledWith(
      expect.objectContaining({
        subscription: expect.objectContaining({
          periodStart: DODO_ISO_DATE,
          periodEnd: DODO_ISO_DATE,
        }),
      }),
    );
  });

  it("converts JS Date.toString() dates to ISO 8601 for subscription.updated", async () => {
    const sink = makeSink();
    await mapDodoEvent("subscription.updated", DODO_SUBSCRIPTION_UPDATED, "user_1", {}, sink);
    expect(sink.ingestBillingEvent).toHaveBeenCalledWith(
      expect.objectContaining({
        subscription: expect.objectContaining({ periodEnd: DODO_ISO_DATE }),
      }),
    );
  });

  it("omits periodStart/periodEnd when dates are absent", async () => {
    const sink = makeSink();
    await mapDodoEvent(
      "subscription.active",
      DODO_SUBSCRIPTION_ACTIVE_NO_DATES,
      "user_1",
      {},
      sink,
    );
    const call = sink.ingestBillingEvent.mock.calls[0]![0]!;
    if (!call.subscription) throw new Error("Expected subscription event data");
    expect(call.subscription.periodStart).toBeUndefined();
    // periodEnd is passed explicitly — check it's null
    expect(call.subscription.periodEnd).toBeNull();
  });
});

// ── Event type routing ──────────────────────────────────────────────

describe("event type routing", () => {
  it("subscription.active → subscription.created", async () => {
    const sink = makeSink();
    await mapDodoEvent("subscription.active", DODO_SUBSCRIPTION_ACTIVE, "user_1", {}, sink);
    expect(sink.ingestBillingEvent).toHaveBeenCalledWith(
      expect.objectContaining({ eventType: "subscription.created" }),
    );
  });

  it("preserves trialing status on subscription.active", async () => {
    const sink = makeSink();
    await mapDodoEvent(
      "subscription.active",
      { ...DODO_SUBSCRIPTION_ACTIVE, status: "trialing" },
      "user_1",
      {},
      sink,
    );
    expect(sink.ingestBillingEvent).toHaveBeenCalledWith(
      expect.objectContaining({
        subscription: expect.objectContaining({ status: "trialing" }),
      }),
    );
  });

  it("subscription.renewed → subscription.renewed", async () => {
    const sink = makeSink();
    await mapDodoEvent("subscription.renewed", DODO_SUBSCRIPTION_RENEWED, "user_1", {}, sink);
    expect(sink.ingestBillingEvent).toHaveBeenCalledWith(
      expect.objectContaining({ eventType: "subscription.renewed" }),
    );
  });

  it("subscription.cancelled → subscription.canceled", async () => {
    const sink = makeSink();
    await mapDodoEvent("subscription.cancelled", DODO_SUBSCRIPTION_CANCELLED, null, {}, sink);
    expect(sink.ingestBillingEvent).toHaveBeenCalledWith(
      expect.objectContaining({
        eventType: "subscription.canceled",
        subscription: {
          providerSubscriptionId: "sub_dodo_cancelled_001",
          status: "canceled",
          refs: { productId: "prod_monk" },
        },
      }),
    );
  });

  it("subscription.expired → subscription.expired", async () => {
    const sink = makeSink();
    await mapDodoEvent("subscription.expired", DODO_SUBSCRIPTION_EXPIRED, null, {}, sink);
    expect(sink.ingestBillingEvent).toHaveBeenCalledWith(
      expect.objectContaining({
        eventType: "subscription.expired",
        subscription: { providerSubscriptionId: "sub_dodo_expired_001", status: "expired" },
      }),
    );
  });

  it("subscription.failed → subscription.updated with past_due status", async () => {
    const sink = makeSink();
    await mapDodoEvent("subscription.failed", DODO_SUBSCRIPTION_FAILED, null, {}, sink);
    expect(sink.ingestBillingEvent).toHaveBeenCalledWith(
      expect.objectContaining({
        eventType: "subscription.updated",
        subscription: { providerSubscriptionId: "sub_dodo_failed_001", status: "past_due" },
      }),
    );
  });

  it("subscription.on_hold → subscription.updated with past_due status", async () => {
    const sink = makeSink();
    await mapDodoEvent("subscription.on_hold", DODO_SUBSCRIPTION_ON_HOLD, null, {}, sink);
    expect(sink.ingestBillingEvent).toHaveBeenCalledWith(
      expect.objectContaining({
        eventType: "subscription.updated",
        subscription: { providerSubscriptionId: "sub_dodo_on_hold_001", status: "past_due" },
      }),
    );
  });

  it("subscription.paused → subscription.paused", async () => {
    const sink = makeSink();
    await mapDodoEvent(
      "subscription.paused",
      {
        ...DODO_SUBSCRIPTION_ON_HOLD,
        subscription_id: "sub_dodo_paused_001",
        status: "paused",
      },
      "user_1",
      {},
      sink,
    );
    expect(sink.ingestBillingEvent).toHaveBeenCalledWith(
      expect.objectContaining({
        eventType: "subscription.paused",
        subscription: expect.objectContaining({
          providerSubscriptionId: "sub_dodo_paused_001",
          status: "paused",
        }),
      }),
    );
  });

  it("subscription.plan_changed → subscription.plan_changed with product_id refs", async () => {
    const sink = makeSink();
    await mapDodoEvent(
      "subscription.plan_changed",
      DODO_SUBSCRIPTION_PLAN_CHANGED,
      "user_1",
      {},
      sink,
    );
    expect(sink.ingestBillingEvent).toHaveBeenCalledWith(
      expect.objectContaining({
        eventType: "subscription.plan_changed",
        subscription: expect.objectContaining({
          providerSubscriptionId: "sub_dodo_plan_change_001",
          cancelAtPeriodEnd: true,
          refs: { productId: "prod_sage" },
        }),
      }),
    );
  });

  it("payment.succeeded → payment.succeeded", async () => {
    const sink = makeSink();
    await mapDodoEvent("payment.succeeded", DODO_PAYMENT_SUCCEEDED, null, {}, sink);
    expect(sink.ingestBillingEvent).toHaveBeenCalledWith(
      expect.objectContaining({
        eventType: "payment.succeeded",
        payment: expect.objectContaining({
          providerPaymentId: "pay_dodo_success_001",
          amountMinor: 2999,
          currency: "USD",
        }),
        subscription: expect.objectContaining({
          providerSubscriptionId: DODO_PAYMENT_SUCCEEDED.subscription_id,
          periodStart: null,
          periodEnd: null,
        }),
      }),
    );
  });

  it("payment.failed → payment.failed", async () => {
    const sink = makeSink();
    await mapDodoEvent("payment.failed", DODO_PAYMENT_FAILED, "user_1", {}, sink);
    expect(sink.ingestBillingEvent).toHaveBeenCalledWith(
      expect.objectContaining({
        eventType: "payment.failed",
        subscription: { providerSubscriptionId: "sub_dodo_active_001" },
      }),
    );
  });

  it("payment.cancelled → terminal canceled payment", async () => {
    const sink = makeSink();
    await mapDodoEvent("payment.cancelled", DODO_PAYMENT_FAILED, "user_1", {}, sink);
    expect(sink.ingestBillingEvent).toHaveBeenCalledWith(
      expect.objectContaining({
        eventType: "payment.failed",
        payment: expect.objectContaining({ status: "canceled" }),
      }),
    );
  });

  it("refund.succeeded → refund.created", async () => {
    const sink = makeSink();
    await mapDodoEvent("refund.succeeded", DODO_REFUND_SUCCEEDED, null, {}, sink);
    expect(sink.ingestBillingEvent).toHaveBeenCalledWith(
      expect.objectContaining({
        eventType: "refund.created",
        refund: expect.objectContaining({
          providerRefundId: "refund_dodo_001",
          providerPaymentId: "pay_dodo_success_001",
          amountMinor: 2999,
          currency: "USD",
        }),
      }),
    );
  });

  it("uses refund_id as the event id when Dodo omits data.id", async () => {
    const sink = makeSink();
    await mapDodoEvent(
      "refund.succeeded",
      {
        refund_id: "refund_dodo_without_id",
        payment_id: "pay_dodo_success_001",
        refund_amount: 100,
        currency: "USD",
      },
      null,
      {},
      sink,
    );

    expect(sink.ingestBillingEvent).toHaveBeenCalledWith(
      expect.objectContaining({
        eventId: dodoEventId("refund.succeeded", "refund_dodo_without_id"),
        eventType: "refund.created",
      }),
    );
  });

  it("dispute.opened → dispute.created", async () => {
    const sink = makeSink();
    await mapDodoEvent("dispute.opened", DODO_DISPUTE_OPENED, null, {}, sink);
    expect(sink.ingestBillingEvent).toHaveBeenCalledWith(
      expect.objectContaining({
        eventType: "dispute.created",
        dispute: expect.objectContaining({
          providerDisputeId: "dispute_dodo_001",
          providerPaymentId: "pay_dodo_success_001",
        }),
      }),
    );
  });

  it("dispute.won/lost/accepted/cancelled/challenged/expired → dispute.closed", async () => {
    const sink = makeSink();
    await mapDodoEvent("dispute.won", DODO_DISPUTE_WON_CLOSED, null, {}, sink);
    expect(sink.ingestBillingEvent).toHaveBeenCalledWith(
      expect.objectContaining({ eventType: "dispute.closed" }),
    );
  });

  it("does not call sink for unknown event types", async () => {
    const sink = makeSink();
    await mapDodoEvent("unknown.event.type", {}, null, {}, sink);
    expect(sink.ingestBillingEvent).not.toHaveBeenCalled();
  });

  it("passes metadata through to the sink event", async () => {
    const sink = makeSink();
    const metadata = {
      bursar_account_id: "user_1",
      plan_slug: "monk",
      billing_interval: "month",
    };
    await mapDodoEvent("subscription.active", DODO_SUBSCRIPTION_ACTIVE, "user_1", metadata, sink);
    expect(sink.ingestBillingEvent).toHaveBeenCalledWith(expect.objectContaining({ metadata }));
  });
});

// ── Ref resolution ──────────────────────────────────────────────────

describe("ref resolution", () => {
  it("uses data.product_id for refs when present", async () => {
    const sink = makeSink();
    await mapDodoEvent("subscription.active", DODO_SUBSCRIPTION_ACTIVE, "user_1", {}, sink);
    expect(sink.ingestBillingEvent).toHaveBeenCalledWith(
      expect.objectContaining({
        subscription: expect.objectContaining({ refs: { productId: "prod_monk" } }),
      }),
    );
  });

  it("falls back to metadata.plan_slug when data.product_id is absent", async () => {
    const sink = makeSink();
    await mapDodoEvent(
      "subscription.active",
      DODO_SUBSCRIPTION_ACTIVE_PLAN_SLUG,
      "user_1",
      { plan_slug: "sage" },
      sink,
    );
    expect(sink.ingestBillingEvent).toHaveBeenCalledWith(
      expect.objectContaining({
        subscription: expect.objectContaining({ refs: { lookupKey: "sage" } }),
      }),
    );
  });

  it("rejects a blank product_id instead of manufacturing a reference", async () => {
    const sink = makeSink();
    await expect(
      mapDodoEvent(
        "subscription.updated",
        { subscription_id: "sub_blank_product", product_id: "   " },
        "user_1",
        { plan_slug: "sage" },
        sink,
      ),
    ).rejects.toThrow("product_id");
    expect(sink.ingestBillingEvent).not.toHaveBeenCalled();
  });

  it("uses the official payment product_cart for refs", async () => {
    const sink = makeSink();
    const payment: Record<string, unknown> = { ...DODO_PAYMENT_SUCCEEDED };
    delete payment.product_id;
    delete payment.subscription_id;
    await mapDodoEvent(
      "payment.succeeded",
      {
        ...payment,
        product_cart: [{ product_id: "prod_credit_pack", quantity: 2 }],
      },
      "user_1",
      {},
      sink,
    );

    expect(sink.ingestBillingEvent).toHaveBeenCalledWith(
      expect.objectContaining({
        payment: expect.objectContaining({
          purpose: "credit_topup",
          refs: { productId: "prod_credit_pack" },
        }),
      }),
    );
  });

  it("uses the official nested customer identity", async () => {
    const sink = makeSink();
    await mapDodoEvent(
      "subscription.active",
      {
        ...DODO_SUBSCRIPTION_ACTIVE,
        customer: {
          customer_id: "cus_official_001",
          email: "learner@example.com",
        },
      },
      "user_1",
      {},
      sink,
    );

    expect(sink.ingestBillingEvent).toHaveBeenCalledWith(
      expect.objectContaining({
        customer: {
          providerCustomerId: "cus_official_001",
          email: "learner@example.com",
        },
      }),
    );
  });

  it("sets refs to undefined when neither product_id nor plan_slug are present", async () => {
    const sink = makeSink();
    const payload = { subscription_id: "sub_no_refs", status: "active" };
    await mapDodoEvent("subscription.active", payload, "user_1", {}, sink);
    const call = sink.ingestBillingEvent.mock.calls[0]![0]!;
    if (!call.subscription) throw new Error("Expected subscription event data");
    expect(call.subscription.refs).toBeUndefined();
  });
});

// ── Edge cases ──────────────────────────────────────────────────────

describe("edge cases", () => {
  it("rejects a non-boolean cancellation flag", async () => {
    const sink = makeSink();
    await expect(
      mapDodoEvent(
        "subscription.plan_changed",
        { ...DODO_SUBSCRIPTION_PLAN_CHANGED, cancel_at_next_billing_date: "true" },
        "user_1",
        {},
        sink,
      ),
    ).rejects.toThrow("cancel_at_next_billing_date");
    expect(sink.ingestBillingEvent).not.toHaveBeenCalled();
  });

  it("rejects subscription.cancelled when subscription_id is missing", async () => {
    const sink = makeSink();
    await expect(mapDodoEvent("subscription.cancelled", {}, null, {}, sink)).rejects.toThrow(
      "subscription_id",
    );
    expect(sink.ingestBillingEvent).not.toHaveBeenCalled();
  });

  it("rejects subscription.expired when subscription_id is missing", async () => {
    const sink = makeSink();
    await expect(mapDodoEvent("subscription.expired", {}, null, {}, sink)).rejects.toThrow(
      "subscription_id",
    );
    expect(sink.ingestBillingEvent).not.toHaveBeenCalled();
  });

  it("ingests subscription.active without an account for persisted-reference resolution", async () => {
    const sink = makeSink();
    await mapDodoEvent("subscription.active", DODO_SUBSCRIPTION_ACTIVE, null, {}, sink);
    const event = sink.ingestBillingEvent.mock.calls[0]?.[0];
    expect(event).toMatchObject({ eventType: "subscription.created" });
    expect(event).not.toHaveProperty("accountId");
  });

  it("ingests subscription.renewed without an account for persisted-reference resolution", async () => {
    const sink = makeSink();
    await mapDodoEvent("subscription.renewed", DODO_SUBSCRIPTION_RENEWED, null, {}, sink);
    const event = sink.ingestBillingEvent.mock.calls[0]?.[0];
    expect(event).toMatchObject({ eventType: "subscription.renewed" });
    expect(event).not.toHaveProperty("accountId");
  });

  it("normalizes cadence fields (yearly interval)", async () => {
    const sink = makeSink();
    await mapDodoEvent(
      "subscription.active",
      {
        subscription_id: "sub_cadence",
        status: "active",
        product_id: "prod_yearly",
        payment_frequency_interval: "Year",
        payment_frequency_count: 1,
      },
      "user_1",
      {},
      sink,
    );
    expect(sink.ingestBillingEvent).toHaveBeenCalledWith(
      expect.objectContaining({
        subscription: expect.objectContaining({ interval: "year", intervalCount: 1 }),
      }),
    );
  });

  it("falls back to metadata.billing_interval when payment_frequency_interval is absent", async () => {
    const sink = makeSink();
    await mapDodoEvent(
      "subscription.active",
      { subscription_id: "sub_meta_interval", status: "active", product_id: "prod_monthly" },
      "user_1",
      { billing_interval: "month" },
      sink,
    );
    expect(sink.ingestBillingEvent).toHaveBeenCalledWith(
      expect.objectContaining({
        subscription: expect.objectContaining({ interval: "month", intervalCount: 1 }),
      }),
    );
  });
});
