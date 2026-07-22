import { describe, expect, it, vi } from "vitest";
import { handleDodoBillingEvent, normalizeDate } from "../src/providers/dodo/event-mapper.js";
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
  DODO_SUBSCRIPTION_CANCELLATION_SCHEDULED,
  DODO_SUBSCRIPTION_CANCELLATION_UNSCHEDULED,
  DODO_SUBSCRIPTION_PLAN_CHANGED,
  DODO_PAYMENT_SUCCEEDED,
  DODO_PAYMENT_FAILED,
  DODO_CHECKOUT_EXPIRED,
  DODO_REFUND_SUCCEEDED,
  DODO_DISPUTE_CREATED,
  DODO_DISPUTE_WON_CLOSED,
} from "./helpers/dodo-fixtures.js";

/** Shared mock sink. Each test that needs one calls makeSink(). */
function makeSink() {
  return {
    ingestBillingEvent: vi.fn().mockResolvedValue({ handled: true }),
  } as unknown as BillingEventSink;
}

// ── normalizeDate unit tests ──────────────────────────────────────────

describe("normalizeDate", () => {
  it("converts JS Date.toString() format to ISO 8601", () => {
    expect(normalizeDate(DODO_JS_DATE)).toBe(DODO_ISO_DATE);
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

// ── RawId fallback (Bug 1 regression tests) ─────────────────────────

describe("rawId fallback", () => {
  it("uses data.id when present (payment events)", async () => {
    const sink = makeSink();
    await handleDodoBillingEvent("payment.succeeded", DODO_PAYMENT_SUCCEEDED, "user_1", {}, sink);
    expect(sink.ingestBillingEvent).toHaveBeenCalledWith(
      expect.objectContaining({ eventId: "pay_dodo_success_001" }),
    );
  });

  it("falls back to dodo:{type}:{subscription_id} when data.id is absent (subscription.active)", async () => {
    const sink = makeSink();
    await handleDodoBillingEvent(
      "subscription.active",
      DODO_SUBSCRIPTION_ACTIVE,
      "user_1",
      {},
      sink,
    );
    expect(sink.ingestBillingEvent).toHaveBeenCalledWith(
      expect.objectContaining({ eventId: "dodo:subscription.active:sub_dodo_active_001" }),
    );
  });

  it("falls back to dodo:{type}:{subscription_id} (subscription.renewed)", async () => {
    const sink = makeSink();
    await handleDodoBillingEvent(
      "subscription.renewed",
      DODO_SUBSCRIPTION_RENEWED,
      "user_1",
      {},
      sink,
    );
    expect(sink.ingestBillingEvent).toHaveBeenCalledWith(
      expect.objectContaining({ eventId: "dodo:subscription.renewed:sub_dodo_renewed_001" }),
    );
  });

  it("falls back to dodo:{type}:{subscription_id} (subscription.updated)", async () => {
    const sink = makeSink();
    await handleDodoBillingEvent(
      "subscription.updated",
      DODO_SUBSCRIPTION_UPDATED,
      "user_1",
      {},
      sink,
    );
    expect(sink.ingestBillingEvent).toHaveBeenCalledWith(
      expect.objectContaining({ eventId: "dodo:subscription.updated:sub_dodo_updated_001" }),
    );
  });

  it("produces unique rawIds for different subscriptions of the same event type", async () => {
    const sink = makeSink();
    await handleDodoBillingEvent(
      "subscription.active",
      { ...DODO_SUBSCRIPTION_ACTIVE, subscription_id: "sub_alpha" },
      "user_1",
      {},
      sink,
    );
    await handleDodoBillingEvent(
      "subscription.active",
      { ...DODO_SUBSCRIPTION_ACTIVE, subscription_id: "sub_beta" },
      "user_1",
      {},
      sink,
    );
    const calls = sink.ingestBillingEvent.mock.calls;
    expect(calls).toHaveLength(2);
    expect(calls[0][0].eventId).toBe("dodo:subscription.active:sub_alpha");
    expect(calls[1][0].eventId).toBe("dodo:subscription.active:sub_beta");
  });

  it("falls back to dodo:{type}:{customer_id} when subscription_id is also absent", async () => {
    const sink = makeSink();
    // subscription.active doesn't guard on subscription_id — the rawId fallback
    // to customer_id is exercised when both data.id and data.subscription_id are absent.
    const payload = { customer_id: "cus_dodo_001", status: "active" };
    await handleDodoBillingEvent("subscription.active", payload, "user_1", {}, sink);
    expect(sink.ingestBillingEvent).toHaveBeenCalledWith(
      expect.objectContaining({ eventId: "dodo:subscription.active:cus_dodo_001" }),
    );
  });

  it("produces dodo:{type}: (empty suffix) when both subscription_id and customer_id are absent", async () => {
    const sink = makeSink();
    await handleDodoBillingEvent("subscription.active", {}, "user_1", {}, sink);
    expect(sink.ingestBillingEvent).toHaveBeenCalledWith(
      expect.objectContaining({ eventId: "dodo:subscription.active:" }),
    );
  });
});

// ── Date normalization (Bug 2 regression tests) ──────────────────────

describe("date normalization through event mapper", () => {
  it("converts JS Date.toString() dates to ISO 8601 for subscription.active → subscription.created", async () => {
    const sink = makeSink();
    await handleDodoBillingEvent(
      "subscription.active",
      DODO_SUBSCRIPTION_ACTIVE,
      "user_1",
      {},
      sink,
    );
    const call = sink.ingestBillingEvent.mock.calls[0][0];
    expect(call.subscription.periodStart).toBe(DODO_ISO_DATE);
    // next_billing_date in fixture is August — verify it's different from periodStart
    expect(call.subscription.periodEnd).toBe("2026-08-18T05:15:24.000Z");
  });

  it("converts JS Date.toString() dates to ISO 8601 for subscription.renewed → subscription.activated", async () => {
    const sink = makeSink();
    await handleDodoBillingEvent(
      "subscription.renewed",
      DODO_SUBSCRIPTION_RENEWED,
      "user_1",
      {},
      sink,
    );
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
    await handleDodoBillingEvent(
      "subscription.updated",
      DODO_SUBSCRIPTION_UPDATED,
      "user_1",
      {},
      sink,
    );
    expect(sink.ingestBillingEvent).toHaveBeenCalledWith(
      expect.objectContaining({
        subscription: expect.objectContaining({ periodEnd: DODO_ISO_DATE }),
      }),
    );
  });

  it("omits periodStart/periodEnd when dates are absent", async () => {
    const sink = makeSink();
    await handleDodoBillingEvent(
      "subscription.active",
      DODO_SUBSCRIPTION_ACTIVE_NO_DATES,
      "user_1",
      {},
      sink,
    );
    const call = sink.ingestBillingEvent.mock.calls[0][0];
    expect(call.subscription.periodStart).toBeUndefined();
    // periodEnd is passed explicitly — check it's null
    expect(call.subscription.periodEnd).toBeNull();
  });
});

// ── Event type routing ──────────────────────────────────────────────

describe("event type routing", () => {
  it("subscription.active → subscription.created", async () => {
    const sink = makeSink();
    await handleDodoBillingEvent(
      "subscription.active",
      DODO_SUBSCRIPTION_ACTIVE,
      "user_1",
      {},
      sink,
    );
    expect(sink.ingestBillingEvent).toHaveBeenCalledWith(
      expect.objectContaining({ eventType: "subscription.created" }),
    );
  });

  it("subscription.renewed → subscription.activated", async () => {
    const sink = makeSink();
    await handleDodoBillingEvent(
      "subscription.renewed",
      DODO_SUBSCRIPTION_RENEWED,
      "user_1",
      {},
      sink,
    );
    expect(sink.ingestBillingEvent).toHaveBeenCalledWith(
      expect.objectContaining({ eventType: "subscription.activated" }),
    );
  });

  it("subscription.cancelled → subscription.canceled", async () => {
    const sink = makeSink();
    await handleDodoBillingEvent(
      "subscription.cancelled",
      DODO_SUBSCRIPTION_CANCELLED,
      null,
      {},
      sink,
    );
    expect(sink.ingestBillingEvent).toHaveBeenCalledWith(
      expect.objectContaining({
        eventType: "subscription.canceled",
        subscription: { providerSubscriptionId: "sub_dodo_cancelled_001" },
      }),
    );
  });

  it("subscription.expired → subscription.expired", async () => {
    const sink = makeSink();
    await handleDodoBillingEvent("subscription.expired", DODO_SUBSCRIPTION_EXPIRED, null, {}, sink);
    expect(sink.ingestBillingEvent).toHaveBeenCalledWith(
      expect.objectContaining({
        eventType: "subscription.expired",
        subscription: { providerSubscriptionId: "sub_dodo_expired_001" },
      }),
    );
  });

  it("subscription.failed → subscription.updated with past_due status", async () => {
    const sink = makeSink();
    await handleDodoBillingEvent("subscription.failed", DODO_SUBSCRIPTION_FAILED, null, {}, sink);
    expect(sink.ingestBillingEvent).toHaveBeenCalledWith(
      expect.objectContaining({
        eventType: "subscription.updated",
        subscription: { providerSubscriptionId: "sub_dodo_failed_001", status: "past_due" },
      }),
    );
  });

  it("subscription.on_hold → subscription.updated with past_due status", async () => {
    const sink = makeSink();
    await handleDodoBillingEvent("subscription.on_hold", DODO_SUBSCRIPTION_ON_HOLD, null, {}, sink);
    expect(sink.ingestBillingEvent).toHaveBeenCalledWith(
      expect.objectContaining({
        eventType: "subscription.updated",
        subscription: { providerSubscriptionId: "sub_dodo_on_hold_001", status: "past_due" },
      }),
    );
  });

  it("subscription.cancellation_scheduled → subscription.cancellation_scheduled", async () => {
    const sink = makeSink();
    await handleDodoBillingEvent(
      "subscription.cancellation_scheduled",
      DODO_SUBSCRIPTION_CANCELLATION_SCHEDULED,
      null,
      {},
      sink,
    );
    expect(sink.ingestBillingEvent).toHaveBeenCalledWith(
      expect.objectContaining({
        eventType: "subscription.cancellation_scheduled",
        subscription: {
          providerSubscriptionId: "sub_dodo_cancel_sched_001",
          cancelAtPeriodEnd: true,
        },
      }),
    );
  });

  it("subscription.cancellation_unscheduled → subscription.cancellation_unscheduled", async () => {
    const sink = makeSink();
    await handleDodoBillingEvent(
      "subscription.cancellation_unscheduled",
      DODO_SUBSCRIPTION_CANCELLATION_UNSCHEDULED,
      null,
      {},
      sink,
    );
    expect(sink.ingestBillingEvent).toHaveBeenCalledWith(
      expect.objectContaining({
        eventType: "subscription.cancellation_unscheduled",
        subscription: {
          providerSubscriptionId: "sub_dodo_cancel_unsched_001",
          cancelAtPeriodEnd: false,
        },
      }),
    );
  });

  it("subscription.plan_changed → subscription.plan_changed with product_id refs", async () => {
    const sink = makeSink();
    await handleDodoBillingEvent(
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
          refs: { productId: "prod_sage" },
        }),
      }),
    );
  });

  it("payment.succeeded → payment.succeeded", async () => {
    const sink = makeSink();
    await handleDodoBillingEvent("payment.succeeded", DODO_PAYMENT_SUCCEEDED, null, {}, sink);
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
    await handleDodoBillingEvent("payment.failed", DODO_PAYMENT_FAILED, "user_1", {}, sink);
    expect(sink.ingestBillingEvent).toHaveBeenCalledWith(
      expect.objectContaining({
        eventType: "payment.failed",
        subscription: { providerSubscriptionId: "sub_dodo_active_001" },
      }),
    );
  });

  it("checkout.expired → checkout.expired", async () => {
    const sink = makeSink();
    await handleDodoBillingEvent("checkout.expired", DODO_CHECKOUT_EXPIRED, null, {}, sink);
    expect(sink.ingestBillingEvent).toHaveBeenCalledWith(
      expect.objectContaining({ eventType: "checkout.expired" }),
    );
  });

  it("refund.succeeded → refund.created", async () => {
    const sink = makeSink();
    await handleDodoBillingEvent("refund.succeeded", DODO_REFUND_SUCCEEDED, null, {}, sink);
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
    await handleDodoBillingEvent(
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
        eventId: "refund_dodo_without_id",
        eventType: "refund.created",
      }),
    );
  });

  it("dispute.* → dispute.created for open dispute types", async () => {
    const sink = makeSink();
    await handleDodoBillingEvent("dispute.created", DODO_DISPUTE_CREATED, null, {}, sink);
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
    await handleDodoBillingEvent("dispute.won", DODO_DISPUTE_WON_CLOSED, null, {}, sink);
    expect(sink.ingestBillingEvent).toHaveBeenCalledWith(
      expect.objectContaining({ eventType: "dispute.closed" }),
    );
  });

  it("does not call sink for unknown event types", async () => {
    const sink = makeSink();
    await handleDodoBillingEvent("unknown.event.type", {}, null, {}, sink);
    expect(sink.ingestBillingEvent).not.toHaveBeenCalled();
  });

  it("passes metadata through to the sink event", async () => {
    const sink = makeSink();
    const metadata = { userId: "user_1", plan_slug: "monk", billing_interval: "month" };
    await handleDodoBillingEvent(
      "subscription.active",
      DODO_SUBSCRIPTION_ACTIVE,
      "user_1",
      metadata,
      sink,
    );
    expect(sink.ingestBillingEvent).toHaveBeenCalledWith(expect.objectContaining({ metadata }));
  });
});

// ── Ref resolution ──────────────────────────────────────────────────

describe("ref resolution", () => {
  it("uses data.product_id for refs when present", async () => {
    const sink = makeSink();
    await handleDodoBillingEvent(
      "subscription.active",
      DODO_SUBSCRIPTION_ACTIVE,
      "user_1",
      {},
      sink,
    );
    expect(sink.ingestBillingEvent).toHaveBeenCalledWith(
      expect.objectContaining({
        subscription: expect.objectContaining({ refs: { productId: "prod_monk" } }),
      }),
    );
  });

  it("falls back to metadata.plan_slug when data.product_id is absent", async () => {
    const sink = makeSink();
    await handleDodoBillingEvent(
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

  it("sets refs to undefined when neither product_id nor plan_slug are present", async () => {
    const sink = makeSink();
    const payload = { subscription_id: "sub_no_refs", status: "active" };
    await handleDodoBillingEvent("subscription.active", payload, "user_1", {}, sink);
    const call = sink.ingestBillingEvent.mock.calls[0][0];
    expect(call.subscription.refs).toBeUndefined();
  });
});

// ── Edge cases ──────────────────────────────────────────────────────

describe("edge cases", () => {
  it("skips subscription.cancelled when subscription_id is missing", async () => {
    const sink = makeSink();
    await handleDodoBillingEvent("subscription.cancelled", {}, null, {}, sink);
    expect(sink.ingestBillingEvent).not.toHaveBeenCalled();
  });

  it("skips subscription.expired when subscription_id is missing", async () => {
    const sink = makeSink();
    await handleDodoBillingEvent("subscription.expired", {}, null, {}, sink);
    expect(sink.ingestBillingEvent).not.toHaveBeenCalled();
  });

  it("skips subscription.active when userId is missing (logs error)", async () => {
    const sink = makeSink();
    await handleDodoBillingEvent("subscription.active", DODO_SUBSCRIPTION_ACTIVE, null, {}, sink);
    expect(sink.ingestBillingEvent).not.toHaveBeenCalled();
  });

  it("skips subscription.renewed when userId is missing (logs error)", async () => {
    const sink = makeSink();
    await handleDodoBillingEvent("subscription.renewed", DODO_SUBSCRIPTION_RENEWED, null, {}, sink);
    expect(sink.ingestBillingEvent).not.toHaveBeenCalled();
  });

  it("normalizes cadence fields (yearly interval)", async () => {
    const sink = makeSink();
    await handleDodoBillingEvent(
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
    await handleDodoBillingEvent(
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
