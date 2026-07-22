import type { BillingSubscriptionStatus } from "../../billing/index.js";
import type { BillingEventSink } from "../../bursar.js";
import type {
  BillingPaymentInfo,
  BillingRefundInfo,
  BillingDisputeInfo,
} from "../../billing/billing-types.js";
import { type ProviderLogger, normalizeProviderLogger } from "../types.js";
import { callBillingEventSink } from "../_shared.js";

// Dodo dispute statuses that indicate the dispute is closed (resolved).
// Maps to the internal "dispute.closed" event type.
const DISPUTE_CLOSED_TYPES = new Set([
  "dispute.won",
  "dispute.lost",
  "dispute.accepted",
  "dispute.cancelled",
  "dispute.challenged",
  "dispute.expired",
]);

function normalizeInterval(value: unknown): string | undefined {
  const interval = String(value ?? "").toLowerCase();
  return ["day", "week", "month", "year"].includes(interval) ? interval : undefined;
}

/** Dodo sometimes sends dates in JS toString() format (e.g. "Sat Jul 18 2026 05:15:24 GMT+0000..."). Normalize to ISO 8601. */
export function normalizeDate(raw: unknown): string | null {
  if (!raw) return null;
  const d = new Date(String(raw));
  return isNaN(d.getTime()) ? null : d.toISOString();
}

function subscriptionFields(data: Record<string, unknown>, metadata: Record<string, string>) {
  const interval =
    normalizeInterval(data.payment_frequency_interval) ??
    normalizeInterval(data.subscription_period_interval) ??
    normalizeInterval(metadata.billing_interval);
  const rawIntervalCount =
    data.payment_frequency_count ?? data.subscription_period_count ?? (interval ? 1 : undefined);
  const intervalCount = Number(rawIntervalCount);
  const ps = normalizeDate(data.previous_billing_date);
  return {
    ...(interval ? { interval } : {}),
    ...(Number.isFinite(intervalCount) && intervalCount > 0 ? { intervalCount } : {}),
    ...(ps ? { periodStart: ps } : {}),
  };
}

export async function handleDodoBillingEvent(
  type: string,
  data: Record<string, unknown>,
  userId: string | null,
  metadata: Record<string, string>,
  sink: BillingEventSink,
  logger?: ProviderLogger | null,
): Promise<void> {
  const log = normalizeProviderLogger(logger);
  const sourceId = data.id ?? data.refund_id ?? data.payment_id;
  const rawId = sourceId
    ? String(sourceId)
    : `dodo:${type}:${data.subscription_id ?? data.customer_id ?? ""}`;

  const customerInfo = {
    providerCustomerId: String(data.customer_id ?? ""),
    email: (data.customer as { email?: string } | undefined)?.email ?? null,
  };

  function baseEvent(eventId: string) {
    return {
      provider: "dodo" as const,
      eventId,
      occurredAt: new Date().toISOString(),
      ...(userId ? { userId } : {}),
      ...(customerInfo.providerCustomerId ? { customer: customerInfo } : {}),
      ...(Object.keys(metadata).length ? { metadata } : {}),
    };
  }

  switch (type) {
    case "checkout.expired": {
      await callBillingEventSink(sink, {
        ...baseEvent(rawId),
        eventType: "checkout.expired",
      });
      return;
    }

    case "subscription.active": {
      if (!userId) {
        log.error("Dodo subscription event: no userId", { event: type });
        return;
      }
      const subId = String(data.subscription_id ?? "");

      const refs = data.product_id
        ? { productId: String(data.product_id) }
        : metadata.plan_slug
          ? { lookupKey: metadata.plan_slug }
          : undefined;
      log.debug("Dodo subscription.active mapped", {
        subscriptionId: subId,
        productId: data.product_id ? String(data.product_id) : undefined,
        planSlug: metadata.plan_slug,
        hasUserId: Boolean(userId),
        refs,
      });
      await callBillingEventSink(sink, {
        ...baseEvent(rawId),
        eventType: "subscription.created",
        subscription: {
          providerSubscriptionId: subId,
          status: (String(data.status ?? "active") || "active") as BillingSubscriptionStatus,
          periodEnd: normalizeDate(data.next_billing_date),
          ...subscriptionFields(data, metadata),
          refs,
        },
      });
      return;
    }

    case "subscription.renewed": {
      if (!userId) {
        log.error("Dodo subscription event: no userId", { event: type });
        return;
      }
      const subId = String(data.subscription_id ?? "");

      await callBillingEventSink(sink, {
        ...baseEvent(rawId),
        eventType: "subscription.activated",
        subscription: {
          providerSubscriptionId: subId,
          status: "active",
          periodEnd: normalizeDate(data.next_billing_date),
          ...subscriptionFields(data, metadata),
          refs: data.product_id
            ? { productId: String(data.product_id) }
            : metadata.plan_slug
              ? { lookupKey: metadata.plan_slug }
              : undefined,
        },
      });
      return;
    }

    case "subscription.cancelled": {
      const subId = String(data.subscription_id ?? "");
      if (!subId) return;
      await callBillingEventSink(sink, {
        ...baseEvent(rawId),
        eventType: "subscription.canceled",
        subscription: { providerSubscriptionId: subId },
      });
      return;
    }

    case "subscription.expired": {
      const subId = String(data.subscription_id ?? "");
      if (!subId) return;
      await callBillingEventSink(sink, {
        ...baseEvent(rawId),
        eventType: "subscription.expired",
        subscription: { providerSubscriptionId: subId },
      });
      return;
    }

    case "subscription.failed": {
      const subId = String(data.subscription_id ?? "");
      if (!subId) return;
      await callBillingEventSink(sink, {
        ...baseEvent(rawId),
        eventType: "subscription.updated",
        subscription: { providerSubscriptionId: subId, status: "past_due" },
      });
      return;
    }

    case "subscription.on_hold": {
      const subId = String(data.subscription_id ?? "");
      if (!subId) return;
      await callBillingEventSink(sink, {
        ...baseEvent(rawId),
        eventType: "subscription.updated",
        subscription: { providerSubscriptionId: subId, status: "past_due" },
      });
      return;
    }

    case "subscription.updated": {
      const subId = String(data.subscription_id ?? "");
      if (!subId) return;
      const pe = normalizeDate(data.next_billing_date);
      await callBillingEventSink(sink, {
        ...baseEvent(rawId),
        eventType: "subscription.updated",
        subscription: {
          providerSubscriptionId: subId,
          status: (String(data.status ?? "") || null) as BillingSubscriptionStatus | null,
          ...(pe ? { periodEnd: pe } : {}),
          ...subscriptionFields(data, metadata),
        },
      });
      return;
    }

    case "subscription.cancellation_scheduled": {
      const subId = String(data.subscription_id ?? "");
      if (!subId) return;
      await callBillingEventSink(sink, {
        ...baseEvent(rawId),
        eventType: "subscription.cancellation_scheduled",
        subscription: { providerSubscriptionId: subId, cancelAtPeriodEnd: true },
      });
      return;
    }

    case "subscription.cancellation_unscheduled": {
      const subId = String(data.subscription_id ?? "");
      if (!subId) return;
      await callBillingEventSink(sink, {
        ...baseEvent(rawId),
        eventType: "subscription.cancellation_unscheduled",
        subscription: { providerSubscriptionId: subId, cancelAtPeriodEnd: false },
      });
      return;
    }

    case "subscription.plan_changed": {
      const subId = String(data.subscription_id ?? "");
      if (!subId) return;
      const productId = String(data.product_id ?? "");
      const refs = productId
        ? { productId }
        : metadata.plan_slug
          ? { lookupKey: metadata.plan_slug }
          : undefined;
      await callBillingEventSink(sink, {
        ...baseEvent(rawId),
        eventType: "subscription.plan_changed",
        subscription: {
          providerSubscriptionId: subId,
          status: "active",
          ...subscriptionFields(data, metadata),
          refs,
        },
      });
      return;
    }

    case "payment.succeeded": {
      const paymentId = String(data.payment_id ?? "");
      const subscriptionId = String(data.subscription_id ?? "");
      const payment: BillingPaymentInfo = {
        providerPaymentId: paymentId || rawId,
        amountMinor: Number(data.settlement_amount ?? data.amount ?? 0),
        taxMinor: data.settlement_tax ? Number(data.settlement_tax) : null,
        currency: String(data.settlement_currency ?? data.currency ?? "USD").toUpperCase(),
        purpose: subscriptionId ? "subscription" : "credit_topup",
        refs: data.product_id ? { productId: String(data.product_id) } : undefined,
      };

      await callBillingEventSink(sink, {
        ...baseEvent(rawId),
        eventType: "payment.succeeded",
        ...(userId ? { userId } : {}),
        ...(subscriptionId
          ? {
              subscription: {
                providerSubscriptionId: subscriptionId,
                status: (String(data.subscription_status ?? "active") ||
                  "active") as BillingSubscriptionStatus,
                periodStart: normalizeDate(data.previous_billing_date),
                periodEnd: normalizeDate(data.next_billing_date),
              },
            }
          : {}),
        payment,
      });
      return;
    }

    case "payment.failed": {
      const subId = String(data.subscription_id ?? "");
      const paymentId = String(data.payment_id ?? "");
      await callBillingEventSink(sink, {
        ...baseEvent(rawId),
        eventType: "payment.failed",
        subscription: subId ? { providerSubscriptionId: subId } : undefined,
        ...(paymentId || data.settlement_amount
          ? {
              payment: {
                providerPaymentId: paymentId || rawId,
                amountMinor: Number(data.settlement_amount ?? data.amount ?? 0),
                taxMinor: data.settlement_tax ? Number(data.settlement_tax) : null,
                currency: String(data.settlement_currency ?? data.currency ?? "USD").toUpperCase(),
                purpose: "subscription" as const,
                refs: data.product_id ? { productId: String(data.product_id) } : undefined,
              },
            }
          : {}),
      });
      return;
    }

    case "refund.succeeded": {
      const refundId = String(data.refund_id ?? data.id ?? "");
      const refund: BillingRefundInfo = {
        providerRefundId: refundId,
        providerPaymentId: String(data.payment_id ?? "") || null,
        amountMinor: Number(data.refund_amount ?? data.amount ?? 0),
        currency: String(data.currency ?? "USD").toUpperCase(),
        reason: (data.reason as string | undefined) ?? null,
      };
      await callBillingEventSink(sink, {
        ...baseEvent(rawId),
        eventType: "refund.created",
        refund,
      });
      return;
    }

    default: {
      if (type.startsWith("dispute.")) {
        const disputeId = String(data.dispute_id ?? data.id ?? "");
        const dispute: BillingDisputeInfo = {
          providerDisputeId: disputeId,
          providerPaymentId: String(data.payment_id ?? "") || null,
          reason: (data.reason as string | undefined) ?? null,
        };
        const eventType = DISPUTE_CLOSED_TYPES.has(type) ? "dispute.closed" : "dispute.created";
        await callBillingEventSink(sink, {
          ...baseEvent(rawId),
          eventType,
          dispute,
        });
        return;
      }
      log.debug("Unhandled Dodo webhook event type", { type });
    }
  }
}
