import type { BillingManager, BillingSubscriptionStatus } from "../../billing/index.js";
import type { BillingPaymentInfo } from "../../billing/billing-types.js";
import type { ProviderLogger } from "../types.js";

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
    case "subscription.active":
    case "subscription.renewed": {
      if (!userId) {
        logger?.error?.("Dodo subscription event: no userId", { event: type });
        return;
      }
      const subId = String(data.subscription_id ?? "");
      const periodEnd = data.next_billing_date as string | null;

      if (type === "subscription.active") {
        await bm.handleEvent({
          ...baseEvent(`${rawId}_created`),
          eventType: "subscription.created",
          subscription: {
            providerSubscriptionId: subId,
            status: "active",
            periodEnd,
            refs: metadata.plan_slug ? { lookupKey: metadata.plan_slug } : undefined,
          },
        });
      }

      await bm.handleEvent({
        ...baseEvent(`${rawId}_activated`),
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
      await bm.handleEvent({
        ...baseEvent(rawId),
        eventType: "subscription.canceled",
        subscription: { providerSubscriptionId: subId },
      });
      return;
    }

    case "subscription.expired": {
      const subId = String(data.subscription_id ?? "");
      if (!subId) return;
      await bm.handleEvent({
        ...baseEvent(rawId),
        eventType: "subscription.expired",
        subscription: { providerSubscriptionId: subId },
      });
      return;
    }

    case "subscription.failed": {
      const subId = String(data.subscription_id ?? "");
      if (!subId) return;
      await bm.handleEvent({
        ...baseEvent(rawId),
        eventType: "subscription.updated",
        subscription: { providerSubscriptionId: subId, status: "past_due" },
      });
      return;
    }

    case "subscription.on_hold": {
      const subId = String(data.subscription_id ?? "");
      if (!subId) return;
      await bm.handleEvent({
        ...baseEvent(rawId),
        eventType: "subscription.paused",
        subscription: { providerSubscriptionId: subId },
      });
      return;
    }

    case "subscription.updated": {
      const subId = String(data.subscription_id ?? "");
      if (!subId) return;
      const periodEnd = data.next_billing_date as string | null;
      await bm.handleEvent({
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
      await bm.handleEvent({
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
      await bm.handleEvent({
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

      await bm.handleEvent({
        ...baseEvent(rawId),
        eventType: "payment.succeeded",
        ...(userId ? { userId } : {}),
        payment,
      });
      return;
    }

    default:
      logger?.debug?.("Unhandled Dodo webhook event type", { type });
  }
}
