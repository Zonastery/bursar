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
  optionalProviderBoolean,
  optionalProviderString,
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

const DODO_SUBSCRIPTION_STATUS: Readonly<Record<string, BillingSubscriptionStatus>> = {
  pending: "incomplete",
  trialing: "trialing",
  active: "active",
  paused: "paused",
  on_hold: "past_due",
  cancelled: "canceled",
  failed: "past_due",
  expired: "expired",
};

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

function normalizeInterval(
  value: unknown,
  field: string,
): "day" | "week" | "month" | "year" | undefined {
  if (value === null || value === undefined) return undefined;
  const interval = requireProviderString(value, field).toLowerCase();
  if (interval === "day" || interval === "week" || interval === "month" || interval === "year") {
    return interval;
  }
  throw new TypeError(`${field} must be day, week, month, or year`);
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
  if (typeof raw !== "string" && !(raw instanceof Date)) {
    throw new TypeError(`${field} must be a valid instant`);
  }
  const normalized = normalizeDate(raw);
  if (!normalized) throw new TypeError(`${field} must be a valid instant`);
  return normalized;
}

function optionalDate(raw: unknown, field: string): string | null {
  if (raw === null || raw === undefined) return null;
  return requireDate(raw, field);
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

function objectValue(value: unknown): Record<string, unknown> | undefined {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return undefined;
  return value as Record<string, unknown>;
}

function optionalObject(value: unknown, field: string): Record<string, unknown> | undefined {
  if (value === null || value === undefined) return undefined;
  return requireObject(value, field);
}

function requireObject(value: unknown, field: string): Record<string, unknown> {
  const object = objectValue(value);
  if (!object) throw new TypeError(`${field} must be an object`);
  return object;
}

function customerFields(data: Record<string, unknown>) {
  const customer = optionalObject(data.customer, "Dodo customer");
  return {
    providerCustomerId:
      optionalProviderString(data.customer_id, "Dodo customer_id") ??
      optionalProviderString(customer?.customer_id, "Dodo customer.customer_id"),
    email: optionalProviderString(customer?.email, "Dodo customer.email") ?? null,
  };
}

function productId(data: Record<string, unknown>): string | undefined {
  const direct = optionalProviderString(data.product_id, "Dodo product_id");
  if (direct) return direct;

  if (data.product_cart === null || data.product_cart === undefined) return undefined;
  if (!Array.isArray(data.product_cart)) {
    throw new TypeError("Dodo product_cart must be an array");
  }
  for (const item of data.product_cart) {
    const cartItem = requireObject(item, "Dodo product_cart item");
    const cartProductId = optionalProviderString(
      cartItem.product_id,
      "Dodo product_cart item.product_id",
    );
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
    normalizeInterval(
      data.payment_frequency_interval,
      "Dodo subscription.payment_frequency_interval",
    ) ??
    normalizeInterval(
      data.subscription_period_interval,
      "Dodo subscription.subscription_period_interval",
    ) ??
    normalizeInterval(metadata.billing_interval, "Dodo metadata.billing_interval");
  const rawIntervalCount =
    data.payment_frequency_count ?? data.subscription_period_count ?? (interval ? 1 : undefined);
  if (rawIntervalCount !== undefined && !interval) {
    throw new TypeError("Dodo subscription interval count requires an interval");
  }
  const intervalCount =
    rawIntervalCount === undefined
      ? undefined
      : requireMinorUnits(rawIntervalCount, "Dodo subscription interval count", true);
  const periodStart = optionalDate(
    data.previous_billing_date,
    "Dodo subscription.previous_billing_date",
  );
  const cancelAtPeriodEnd = optionalProviderBoolean(
    data.cancel_at_next_billing_date,
    "Dodo subscription.cancel_at_next_billing_date",
  );
  return {
    ...(interval ? { interval } : {}),
    ...(intervalCount !== undefined ? { intervalCount } : {}),
    ...(periodStart ? { periodStart } : {}),
    ...(cancelAtPeriodEnd !== undefined ? { cancelAtPeriodEnd } : {}),
  };
}

function subscriptionStatus(
  value: unknown,
  logger: ReturnType<typeof normalizeProviderLogger>,
): BillingSubscriptionStatus | null {
  if (value === null || value === undefined) return null;
  const raw = requireProviderString(value, "Dodo subscription.status");
  const status = DODO_SUBSCRIPTION_STATUS[raw];
  if (!status) logger.warn("Unsupported Dodo subscription status", { status: raw });
  return status ?? null;
}

export async function handleDodoBillingEvent(
  payload: DodoWebhookPayload,
  accountId: string | null,
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
      ...(accountId ? { accountId } : {}),
      ...(customerInfo.providerCustomerId ? { customer: customerInfo } : {}),
      ...(Object.keys(metadata).length ? { metadata } : {}),
    };
  }

  switch (type) {
    case "subscription.active": {
      const subId = subscriptionId(data);

      const refs = subscriptionRefs(data, metadata);
      log.debug("Dodo subscription.active mapped", {
        subscriptionId: subId,
        productId: productId(data),
        planSlug: metadata.plan_slug,
        hasAccountId: Boolean(accountId),
        refs,
      });
      await callBillingEventSink(sink, {
        ...baseEvent(),
        eventType: "subscription.created",
        subscription: {
          providerSubscriptionId: subId,
          status:
            data.status === null || data.status === undefined
              ? "active"
              : subscriptionStatus(data.status, log),
          periodEnd: optionalDate(data.next_billing_date, "Dodo subscription.next_billing_date"),
          ...subscriptionFields(data, metadata),
          refs,
        },
      });
      return;
    }

    case "subscription.renewed": {
      const subId = subscriptionId(data);

      await callBillingEventSink(sink, {
        ...baseEvent(),
        eventType: "subscription.renewed",
        subscription: {
          providerSubscriptionId: subId,
          status: "active",
          periodEnd: optionalDate(data.next_billing_date, "Dodo subscription.next_billing_date"),
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

    case "subscription.paused": {
      const subId = subscriptionId(data);
      await callBillingEventSink(sink, {
        ...baseEvent(),
        eventType: "subscription.paused",
        subscription: {
          providerSubscriptionId: subId,
          status: "paused",
          ...subscriptionFields(data, metadata),
          refs: subscriptionRefs(data, metadata),
        },
      });
      return;
    }

    case "subscription.updated": {
      const subId = subscriptionId(data);
      const periodEnd = optionalDate(data.next_billing_date, "Dodo subscription.next_billing_date");
      await callBillingEventSink(sink, {
        ...baseEvent(),
        eventType: "subscription.updated",
        subscription: {
          providerSubscriptionId: subId,
          status: subscriptionStatus(data.status, log),
          ...(periodEnd ? { periodEnd } : {}),
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
      const subscriptionId = optionalProviderString(
        data.subscription_id,
        "Dodo payment.subscription_id",
      );
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
        ...(accountId ? { accountId } : {}),
        ...(subscriptionId
          ? {
              subscription: {
                providerSubscriptionId: subscriptionId,
                status:
                  data.subscription_status === null || data.subscription_status === undefined
                    ? "active"
                    : subscriptionStatus(data.subscription_status, log),
                periodStart: optionalDate(
                  data.previous_billing_date,
                  "Dodo payment.previous_billing_date",
                ),
                periodEnd: optionalDate(data.next_billing_date, "Dodo payment.next_billing_date"),
              },
            }
          : {}),
        payment,
      });
      return;
    }

    case "payment.failed":
    case "payment.cancelled": {
      const subId = optionalProviderString(data.subscription_id, "Dodo payment.subscription_id");
      const paymentId = requireProviderString(data.payment_id, "Dodo payment.payment_id");
      const providerProductId = productId(data);
      await callBillingEventSink(sink, {
        ...baseEvent(),
        eventType: "payment.failed",
        ...(accountId ? { accountId } : {}),
        subscription: subId ? { providerSubscriptionId: subId } : undefined,
        payment: {
          providerPaymentId: paymentId,
          amountMinor: requireMinorUnits(data.total_amount, "Dodo payment.total_amount"),
          taxMinor: requireMinorUnits(data.tax ?? 0, "Dodo payment.tax"),
          currency: requireCurrency(data.currency, "Dodo payment.currency"),
          purpose: subId ? ("subscription" as const) : ("credit_topup" as const),
          status: type === "payment.cancelled" ? ("canceled" as const) : ("failed" as const),
          refs: providerProductId ? { productId: providerProductId } : undefined,
        },
      });
      return;
    }

    case "refund.succeeded":
    case "refund.failed": {
      const refund: BillingRefundInfo = {
        providerRefundId: requireProviderString(data.refund_id ?? data.id, "Dodo refund.refund_id"),
        providerPaymentId: requireProviderString(data.payment_id, "Dodo refund.payment_id"),
        amountMinor: requireMinorUnits(
          data.refund_amount ?? data.amount,
          "Dodo refund.amount",
          true,
        ),
        currency: requireCurrency(data.currency, "Dodo refund.currency"),
        reason: optionalProviderString(data.reason, "Dodo refund.reason") ?? null,
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
        const dispute: BillingDisputeInfo = {
          providerDisputeId: requireProviderString(
            data.dispute_id ?? data.id,
            "Dodo dispute.dispute_id",
          ),
          providerPaymentId: requireProviderString(data.payment_id, "Dodo dispute.payment_id"),
          status: disputeStatus(type),
          reason: optionalProviderString(data.reason, "Dodo dispute.reason") ?? null,
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
