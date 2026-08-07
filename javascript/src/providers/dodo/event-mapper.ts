import type { BillingSubscriptionStatus } from "../../billing/index.js";
import type { BillingEventSink } from "../../bursar.js";
import type {
  BillingPaymentInfo,
  BillingRefundInfo,
  BillingDisputeInfo,
} from "../../billing/types/index.js";
import type { DodoWebhookPayload } from "./client-contract.js";
import { type ProviderLogger, normalizeProviderLogger } from "../types.js";
import {
  callBillingEventSink,
  requireCurrency,
  requireMinorUnits,
  requireProviderString,
} from "../_shared.js";

// Dodo dispute statuses that indicate the dispute is closed (resolved).
// Maps to the internal "dispute.closed" event type.
const DISPUTE_CLOSED_TYPES = new Set([
  "dispute.won",
  "dispute.lost",
  "dispute.accepted",
  "dispute.cancelled",
  "dispute.expired",
]);
const DISPUTE_OPEN_TYPES = new Set(["dispute.opened", "dispute.challenged"]);

function disputeStatus(
  type: string,
): "needs_response" | "under_review" | "won" | "lost" | "closed" {
  switch (type) {
    case "dispute.opened":
      return "needs_response";
    case "dispute.challenged":
      return "under_review";
    case "dispute.won":
      return "won";
    case "dispute.lost":
    case "dispute.accepted":
      return "lost";
    default:
      return "closed";
  }
}

function normalizeInterval(value: unknown): string | undefined {
  const interval = String(value ?? "").toLowerCase();
  return ["day", "week", "month", "year"].includes(interval) ? interval : undefined;
}

/** Dodo sometimes sends dates in JS toString() format (e.g. "Sat Jul 18 2026 05:15:24 GMT+0000..."). Normalize to ISO 8601. */
export function normalizeDate(raw: unknown): string | null {
  if (!raw) return null;
  if (raw instanceof Date) {
    return Number.isNaN(raw.getTime()) ? null : raw.toISOString();
  }
  const value = String(raw).trim();
  if (!/(?:Z|[+-]\d{2}:?\d{2}|GMT(?:[+-]\d{4})?)(?:\s*\([^)]*\))?$/i.test(value)) {
    return null;
  }
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? null : d.toISOString();
}

function requireDate(raw: unknown, field: string): string {
  const normalized = normalizeDate(raw);
  if (!normalized) throw new TypeError(`${field} must be a valid instant`);
  return normalized;
}

export function dodoBillingEventId(payload: DodoWebhookPayload): string {
  const data = objectValue(payload.data);
  if (!data) throw new TypeError("Dodo webhook data must be an object");
  const resourceKey = payload.type.startsWith("payment.")
    ? "payment_id"
    : payload.type.startsWith("subscription.")
      ? "subscription_id"
      : payload.type.startsWith("refund.")
        ? "refund_id"
        : payload.type.startsWith("dispute.")
          ? "dispute_id"
          : "id";
  const sourceId = data[resourceKey] ?? data.id;
  const objectId = requireProviderString(sourceId, "Dodo webhook object identifier");
  const occurredAt = requireDate(payload.timestamp, "Dodo webhook timestamp");
  return `dodo:${payload.type}:${objectId}:${occurredAt}`;
}

function nonEmptyString(value: unknown): string | undefined {
  if (value === null || value === undefined) return undefined;
  const normalized = String(value).trim();
  return normalized || undefined;
}

function objectValue(value: unknown): Record<string, unknown> | undefined {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return undefined;
  return value as Record<string, unknown>;
}

function customerFields(data: Record<string, unknown>) {
  const customer = objectValue(data.customer);
  return {
    providerCustomerId: nonEmptyString(data.customer_id) ?? nonEmptyString(customer?.customer_id),
    email: nonEmptyString(customer?.email) ?? null,
  };
}

function productId(data: Record<string, unknown>): string | undefined {
  const direct = nonEmptyString(data.product_id);
  if (direct) return direct;

  if (!Array.isArray(data.product_cart)) return undefined;
  for (const item of data.product_cart) {
    const cartProductId = nonEmptyString(objectValue(item)?.product_id);
    if (cartProductId) return cartProductId;
  }
  return undefined;
}

function subscriptionRefs(data: Record<string, unknown>, metadata: Record<string, string>) {
  const providerProductId = productId(data);
  if (providerProductId) return { productId: providerProductId };
  const lookupKey = metadata.plan_slug?.trim();
  return lookupKey ? { lookupKey } : undefined;
}

function subscriptionId(data: Record<string, unknown>): string {
  return requireProviderString(data.subscription_id, "Dodo subscription.subscription_id");
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
  payload: DodoWebhookPayload,
  userId: string | null,
  metadata: Record<string, string>,
  sink: BillingEventSink,
  logger?: ProviderLogger | null,
): Promise<void> {
  const log = normalizeProviderLogger(logger);
  const data = objectValue(payload.data);
  if (!data) throw new TypeError("Dodo webhook data must be an object");
  const type = payload.type;
  const customerInfo = customerFields(data);

  function baseEvent() {
    return {
      provider: "dodo" as const,
      eventId: dodoBillingEventId(payload),
      occurredAt: requireDate(payload.timestamp, "Dodo webhook timestamp"),
      ...(userId ? { userId } : {}),
      ...(customerInfo.providerCustomerId ? { customer: customerInfo } : {}),
      ...(Object.keys(metadata).length ? { metadata } : {}),
    };
  }

  switch (type) {
    case "subscription.active": {
      if (!userId) {
        log.error("Dodo subscription event: no userId", { event: type });
        return;
      }
      const subId = subscriptionId(data);

      const refs = subscriptionRefs(data, metadata);
      log.debug("Dodo subscription.active mapped", {
        subscriptionId: subId,
        productId: productId(data),
        planSlug: metadata.plan_slug,
        hasUserId: Boolean(userId),
        refs,
      });
      await callBillingEventSink(sink, {
        ...baseEvent(),
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
      const subId = subscriptionId(data);

      await callBillingEventSink(sink, {
        ...baseEvent(),
        eventType: "subscription.renewed",
        subscription: {
          providerSubscriptionId: subId,
          status: "active",
          periodEnd: normalizeDate(data.next_billing_date),
          ...subscriptionFields(data, metadata),
          refs: subscriptionRefs(data, metadata),
        },
      });
      return;
    }

    case "subscription.cancelled": {
      const subId = subscriptionId(data);
      await callBillingEventSink(sink, {
        ...baseEvent(),
        eventType: "subscription.canceled",
        subscription: {
          providerSubscriptionId: subId,
          status: "canceled",
          ...subscriptionFields(data, metadata),
          refs: subscriptionRefs(data, metadata),
        },
      });
      return;
    }

    case "subscription.expired": {
      const subId = subscriptionId(data);
      await callBillingEventSink(sink, {
        ...baseEvent(),
        eventType: "subscription.expired",
        subscription: {
          providerSubscriptionId: subId,
          status: "expired",
          ...subscriptionFields(data, metadata),
          refs: subscriptionRefs(data, metadata),
        },
      });
      return;
    }

    case "subscription.failed": {
      const subId = subscriptionId(data);
      await callBillingEventSink(sink, {
        ...baseEvent(),
        eventType: "subscription.updated",
        subscription: {
          providerSubscriptionId: subId,
          status: "past_due",
          ...subscriptionFields(data, metadata),
          refs: subscriptionRefs(data, metadata),
        },
      });
      return;
    }

    case "subscription.on_hold": {
      const subId = subscriptionId(data);
      await callBillingEventSink(sink, {
        ...baseEvent(),
        eventType: "subscription.updated",
        subscription: {
          providerSubscriptionId: subId,
          status: "past_due",
          ...subscriptionFields(data, metadata),
          refs: subscriptionRefs(data, metadata),
        },
      });
      return;
    }

    case "subscription.updated": {
      const subId = subscriptionId(data);
      const pe = normalizeDate(data.next_billing_date);
      await callBillingEventSink(sink, {
        ...baseEvent(),
        eventType: "subscription.updated",
        subscription: {
          providerSubscriptionId: subId,
          status: (String(data.status ?? "") || null) as BillingSubscriptionStatus | null,
          ...(pe ? { periodEnd: pe } : {}),
          ...subscriptionFields(data, metadata),
          refs: subscriptionRefs(data, metadata),
        },
      });
      return;
    }

    case "subscription.plan_changed": {
      const subId = subscriptionId(data);
      await callBillingEventSink(sink, {
        ...baseEvent(),
        eventType: "subscription.plan_changed",
        subscription: {
          providerSubscriptionId: subId,
          status: "active",
          ...subscriptionFields(data, metadata),
          refs: subscriptionRefs(data, metadata),
        },
      });
      return;
    }

    case "payment.succeeded": {
      const paymentId = requireProviderString(data.payment_id, "Dodo payment.payment_id");
      const subscriptionId = String(data.subscription_id ?? "");
      const providerProductId = productId(data);
      const payment: BillingPaymentInfo = {
        providerPaymentId: paymentId,
        amountMinor: requireMinorUnits(data.settlement_amount, "Dodo payment.settlement_amount"),
        taxMinor: requireMinorUnits(data.settlement_tax ?? 0, "Dodo payment.settlement_tax"),
        currency: requireCurrency(data.settlement_currency, "Dodo payment.settlement_currency"),
        purpose: subscriptionId ? "subscription" : "credit_topup",
        status: "succeeded",
        refs: providerProductId ? { productId: providerProductId } : undefined,
      };

      await callBillingEventSink(sink, {
        ...baseEvent(),
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
      const paymentId = requireProviderString(data.payment_id, "Dodo payment.payment_id");
      const providerProductId = productId(data);
      await callBillingEventSink(sink, {
        ...baseEvent(),
        eventType: "payment.failed",
        ...(userId ? { userId } : {}),
        subscription: subId ? { providerSubscriptionId: subId } : undefined,
        payment: {
          providerPaymentId: paymentId,
          amountMinor: requireMinorUnits(data.total_amount, "Dodo payment.total_amount"),
          taxMinor: requireMinorUnits(data.tax ?? 0, "Dodo payment.tax"),
          currency: requireCurrency(data.currency, "Dodo payment.currency"),
          purpose: subId ? ("subscription" as const) : ("credit_topup" as const),
          status: "failed" as const,
          refs: providerProductId ? { productId: providerProductId } : undefined,
        },
      });
      return;
    }

    case "refund.succeeded":
    case "refund.failed": {
      const refundId = String(data.refund_id ?? data.id ?? "");
      const refund: BillingRefundInfo = {
        providerRefundId: requireProviderString(refundId, "Dodo refund.refund_id"),
        providerPaymentId: requireProviderString(data.payment_id, "Dodo refund.payment_id"),
        amountMinor: requireMinorUnits(
          data.refund_amount ?? data.amount,
          "Dodo refund.amount",
          true,
        ),
        currency: requireCurrency(data.currency, "Dodo refund.currency"),
        reason: (data.reason as string | undefined) ?? null,
        status: type === "refund.succeeded" ? "succeeded" : "failed",
      };
      await callBillingEventSink(sink, {
        ...baseEvent(),
        eventType: type === "refund.succeeded" ? "refund.created" : "refund.failed",
        refund,
      });
      return;
    }

    default: {
      if (DISPUTE_OPEN_TYPES.has(type) || DISPUTE_CLOSED_TYPES.has(type)) {
        const disputeId = String(data.dispute_id ?? data.id ?? "");
        const dispute: BillingDisputeInfo = {
          providerDisputeId: requireProviderString(disputeId, "Dodo dispute.dispute_id"),
          providerPaymentId: requireProviderString(data.payment_id, "Dodo dispute.payment_id"),
          status: disputeStatus(type),
          reason: (data.reason as string | undefined) ?? null,
        };
        const eventType = DISPUTE_CLOSED_TYPES.has(type) ? "dispute.closed" : "dispute.created";
        await callBillingEventSink(sink, {
          ...baseEvent(),
          eventType,
          dispute,
        });
        return;
      }
      log.debug("Unhandled Dodo webhook event type", { type });
    }
  }
}
