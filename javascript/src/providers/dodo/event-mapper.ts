import type {
  BillingManager,
  BillingEventResult,
  BillingSubscriptionStatus,
} from "../../billing/index.js";
import type {
  BillingPaymentInfo,
  BillingRefundInfo,
  BillingDisputeInfo,
} from "../../billing/billing-types.js";
import type { ProviderLogger } from "../types.js";

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

/**
 * Wrapper around bm.handleEvent that throws on unhandled results (except
 * "unhandled_event_type" which is a permanent no-op). Ensures the provider
 * receives a retryable signal when the event could not be processed.
 */
async function callBillingManager(
  bm: BillingManager,
  event: Parameters<BillingManager["handleEvent"]>[0],
): Promise<BillingEventResult> {
  const result = await bm.handleEvent(event);
  if (!result.handled && result.error !== "unhandled_event_type") {
    throw new Error(`BillingManager failed to handle event: ${result.error}`);
  }
  return result;
}

export async function handleDodoBillingEvent(
  type: string,
  data: Record<string, unknown>,
  userId: string | null,
  metadata: Record<string, string>,
  bm: BillingManager,
  logger?: ProviderLogger,
): Promise<void> {
  const rawId = String(data.id ?? data.payment_id ?? "");

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
    };
  }

  switch (type) {
    case "subscription.active": {
      if (!userId) {
        logger?.error?.("Dodo subscription event: no userId", { event: type });
        return;
      }
      const subId = String(data.subscription_id ?? "");
      const periodEnd = data.next_billing_date as string | null;

      await callBillingManager(bm, {
        ...baseEvent(rawId),
        eventType: "subscription.created",
        subscription: {
          providerSubscriptionId: subId,
          status: (String(data.status ?? "active") || "active") as BillingSubscriptionStatus,
          periodEnd,
          refs: metadata.plan_slug ? { lookupKey: metadata.plan_slug } : undefined,
        },
      });
      return;
    }

    case "subscription.renewed": {
      if (!userId) {
        logger?.error?.("Dodo subscription event: no userId", { event: type });
        return;
      }
      const subId = String(data.subscription_id ?? "");
      const periodEnd = data.next_billing_date as string | null;

      await callBillingManager(bm, {
        ...baseEvent(rawId),
        eventType: "subscription.activated",
        subscription: {
          providerSubscriptionId: subId,
          status: "active",
          periodEnd,
        },
      });
      return;
    }

    case "subscription.cancelled": {
      const subId = String(data.subscription_id ?? "");
      if (!subId) return;
      await callBillingManager(bm, {
        ...baseEvent(rawId),
        eventType: "subscription.canceled",
        subscription: { providerSubscriptionId: subId },
      });
      return;
    }

    case "subscription.expired": {
      const subId = String(data.subscription_id ?? "");
      if (!subId) return;
      await callBillingManager(bm, {
        ...baseEvent(rawId),
        eventType: "subscription.expired",
        subscription: { providerSubscriptionId: subId },
      });
      return;
    }

    case "subscription.failed": {
      const subId = String(data.subscription_id ?? "");
      if (!subId) return;
      await callBillingManager(bm, {
        ...baseEvent(rawId),
        eventType: "subscription.updated",
        subscription: { providerSubscriptionId: subId, status: "past_due" },
      });
      return;
    }

    case "subscription.on_hold": {
      const subId = String(data.subscription_id ?? "");
      if (!subId) return;
      await callBillingManager(bm, {
        ...baseEvent(rawId),
        eventType: "subscription.updated",
        subscription: { providerSubscriptionId: subId, status: "past_due" },
      });
      return;
    }

    case "subscription.updated": {
      const subId = String(data.subscription_id ?? "");
      if (!subId) return;
      const periodEnd = data.next_billing_date as string | null;
      await callBillingManager(bm, {
        ...baseEvent(rawId),
        eventType: "subscription.updated",
        subscription: {
          providerSubscriptionId: subId,
          status: (String(data.status ?? "") || null) as BillingSubscriptionStatus | null,
          ...(periodEnd ? { periodEnd } : {}),
        },
      });
      return;
    }

    case "subscription.cancellation_scheduled": {
      const subId = String(data.subscription_id ?? "");
      if (!subId) return;
      await callBillingManager(bm, {
        ...baseEvent(rawId),
        eventType: "subscription.cancellation_scheduled",
        subscription: { providerSubscriptionId: subId, cancelAtPeriodEnd: true },
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
      await callBillingManager(bm, {
        ...baseEvent(rawId),
        eventType: "subscription.plan_changed",
        subscription: {
          providerSubscriptionId: subId,
          status: "active",
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

      await callBillingManager(bm, {
        ...baseEvent(rawId),
        eventType: "payment.succeeded",
        ...(userId ? { userId } : {}),
        payment,
      });
      return;
    }

    case "payment.failed": {
      const subId = String(data.subscription_id ?? "");
      const paymentId = String(data.payment_id ?? "");
      await callBillingManager(bm, {
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
      await callBillingManager(bm, {
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
        await callBillingManager(bm, {
          ...baseEvent(rawId),
          eventType,
          dispute,
        });
        return;
      }
      logger?.debug?.("Unhandled Dodo webhook event type", { type });
    }
  }
}
