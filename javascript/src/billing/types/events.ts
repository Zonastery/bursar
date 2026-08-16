import { z } from "zod";

import type { JsonObject, JsonValue } from "../../shared/json.js";
import type {
  BillingCustomerInfo,
  BillingDisputeInfo,
  BillingInvoiceInfo,
  BillingPaymentInfo,
  BillingRefundInfo,
} from "./documents.js";
import type { BillingSubscriptionInfo } from "./subscriptions.js";

export const BillingEventType = {
  CUSTOMER_CREATED: "customer.created",
  CUSTOMER_UPDATED: "customer.updated",
  CUSTOMER_DELETED: "customer.deleted",
  CHECKOUT_COMPLETED: "checkout.completed",
  CHECKOUT_EXPIRED: "checkout.expired",
  SUBSCRIPTION_CREATED: "subscription.created",
  SUBSCRIPTION_UPDATED: "subscription.updated",
  SUBSCRIPTION_ACTIVATED: "subscription.activated",
  SUBSCRIPTION_RENEWED: "subscription.renewed",
  SUBSCRIPTION_PLAN_CHANGED: "subscription.plan_changed",
  SUBSCRIPTION_CANCELLATION_SCHEDULED: "subscription.cancellation_scheduled",
  SUBSCRIPTION_CANCELLATION_UNSCHEDULED: "subscription.cancellation_unscheduled",
  SUBSCRIPTION_CANCELED: "subscription.canceled",
  SUBSCRIPTION_EXPIRED: "subscription.expired",
  SUBSCRIPTION_PAUSED: "subscription.paused",
  SUBSCRIPTION_RESUMED: "subscription.resumed",
  SUBSCRIPTION_TRIAL_WILL_END: "subscription.trial_will_end",
  INVOICE_CREATED: "invoice.created",
  INVOICE_FINALIZED: "invoice.finalized",
  INVOICE_FINALIZATION_FAILED: "invoice.finalization_failed",
  INVOICE_UPCOMING: "invoice.upcoming",
  INVOICE_PAID: "invoice.paid",
  INVOICE_PAYMENT_FAILED: "invoice.payment_failed",
  INVOICE_PAYMENT_ACTION_REQUIRED: "invoice.payment_action_required",
  INVOICE_VOIDED: "invoice.voided",
  PAYMENT_SUCCEEDED: "payment.succeeded",
  PAYMENT_FAILED: "payment.failed",
  REFUND_CREATED: "refund.created",
  REFUND_UPDATED: "refund.updated",
  REFUND_FAILED: "refund.failed",
  DISPUTE_CREATED: "dispute.created",
  DISPUTE_CLOSED: "dispute.closed",
  PAYMENT_METHOD_ATTACHED: "payment_method.attached",
  PAYMENT_METHOD_UPDATED: "payment_method.updated",
  PAYMENT_METHOD_DETACHED: "payment_method.detached",
} as const;

export type BillingEventType = (typeof BillingEventType)[keyof typeof BillingEventType];

export interface BillingEvent {
  provider: string;
  eventId: string;
  eventType: BillingEventType;
  occurredAt: string;
  /** Financial subject affected by this provider event. */
  accountId?: string | null;
  customer?: BillingCustomerInfo | null;
  subscription?: BillingSubscriptionInfo | null;
  invoice?: BillingInvoiceInfo | null;
  payment?: BillingPaymentInfo | null;
  refund?: BillingRefundInfo | null;
  dispute?: BillingDisputeInfo | null;
  metadata?: JsonObject | null;
  raw?: unknown;
  billingEventId?: string;
}

const BILLING_EVENT_TYPES = new Set<string>(Object.values(BillingEventType));
const SUBSCRIPTION_STATUSES = new Set([
  "incomplete",
  "incomplete_expired",
  "trialing",
  "active",
  "past_due",
  "canceled",
  "unpaid",
  "paused",
  "expired",
]);
const BILLING_INTERVALS = new Set(["day", "week", "month", "year"]);
const INVOICE_STATUSES = new Set(["draft", "open", "paid", "void", "uncollectible"]);
const PAYMENT_PURPOSES = new Set(["subscription", "credit_topup"]);
const PAYMENT_STATUSES = new Set(["pending", "succeeded", "failed", "canceled"]);
const DISPUTE_STATUSES = new Set(["needs_response", "under_review", "won", "lost", "closed"]);
const ISO_INSTANT_WITH_OFFSET = /(?:Z|[+-]\d{2}:\d{2})$/;

const billingEventEnvelopeSchema = z
  .object({
    provider: z.json().optional(),
    eventId: z.json().optional(),
    eventType: z.json().optional(),
    occurredAt: z.json().optional(),
    accountId: z.json().optional(),
    customer: z.json().nullable().optional(),
    subscription: z.json().nullable().optional(),
    invoice: z.json().nullable().optional(),
    payment: z.json().nullable().optional(),
    refund: z.json().nullable().optional(),
    dispute: z.json().nullable().optional(),
    metadata: z.json().nullable().optional(),
    raw: z.unknown().optional(),
    billingEventId: z.json().optional(),
  })
  .passthrough();

type BillingEventEnvelope = z.infer<typeof billingEventEnvelopeSchema>;
type BillingValue = JsonValue | undefined;

function requireObject(value: BillingValue, field: string): JsonObject {
  const parsed = z.record(z.string(), z.json()).safeParse(value);
  if (!parsed.success) {
    throw new TypeError(`${field} must be an object`);
  }
  return parsed.data;
}

function rejectUnknownKeys(
  value: BillingEventEnvelope | JsonObject,
  allowed: readonly string[],
  field: string,
): void {
  const known = new Set(allowed);
  const unknown = Object.keys(value).filter((key) => !known.has(key));
  if (unknown.length > 0) {
    throw new TypeError(
      `${field} contains unsupported field${unknown.length === 1 ? "" : "s"}: ${unknown.join(", ")}`,
    );
  }
}

function requireNonEmptyString(value: BillingValue, field: string): void {
  const parsed = z.string().safeParse(value);
  if (!parsed.success || !parsed.data.trim()) {
    throw new TypeError(`${field} must be a non-empty string`);
  }
}

function requireOptionalNonEmptyString(value: BillingValue, field: string): void {
  if (value !== null && value !== undefined) requireNonEmptyString(value, field);
}

function requireOptionalString(value: BillingValue, field: string): void {
  if (value !== null && value !== undefined && !z.string().safeParse(value).success) {
    throw new TypeError(`${field} must be a string`);
  }
}

function requireOptionalBoolean(value: BillingValue, field: string): void {
  if (value !== null && value !== undefined && !z.boolean().safeParse(value).success) {
    throw new TypeError(`${field} must be a boolean`);
  }
}

function requireOneOf(value: BillingValue, allowed: ReadonlySet<string>, field: string): void {
  const parsed = z.string().safeParse(value);
  if (!parsed.success || !allowed.has(parsed.data)) {
    throw new TypeError(`${field} has an unsupported value`);
  }
}

function validateProviderRef(value: BillingValue, field: string): void {
  if (value === null || value === undefined) return;
  const reference = requireObject(value, field);
  const keys = ["productId", "priceId", "variantId", "lookupKey"] as const;
  rejectUnknownKeys(reference, keys, field);
  for (const key of keys) requireOptionalNonEmptyString(reference[key], `${field}.${key}`);
  if (!keys.some((key) => reference[key] !== null && reference[key] !== undefined)) {
    throw new TypeError(`${field} must contain a provider identifier`);
  }
}

function requireMinorUnits(value: BillingValue, field: string, positive = false): void {
  const parsed = z.number().safeParse(value);
  if (!parsed.success || !Number.isSafeInteger(parsed.data) || parsed.data < (positive ? 1 : 0)) {
    throw new TypeError(
      `${field} must be a ${positive ? "positive" : "non-negative"} safe integer`,
    );
  }
}

function requireCurrency(value: BillingValue, field: string): void {
  const parsed = z
    .string()
    .regex(/^[A-Z]{3}$/u)
    .safeParse(value);
  if (!parsed.success) {
    throw new TypeError(`${field} must be an uppercase three-letter currency code`);
  }
}

function requireOptionalInstant(value: BillingValue, field: string): void {
  if (value === null || value === undefined) return;
  const parsed = z.string().safeParse(value);
  if (
    !parsed.success ||
    !ISO_INSTANT_WITH_OFFSET.test(parsed.data) ||
    Number.isNaN(Date.parse(parsed.data))
  ) {
    throw new TypeError(`${field} must be an ISO 8601 instant with an offset`);
  }
}

/** Validate an event at the public ingestion boundary before claiming it. */
export function assertBillingEvent<T>(value: T): asserts value is T & BillingEvent {
  const parsedEnvelope = billingEventEnvelopeSchema.safeParse(value);
  if (!parsedEnvelope.success) throw new TypeError("billing event must be an object");
  const record = parsedEnvelope.data;
  rejectUnknownKeys(
    record,
    [
      "provider",
      "eventId",
      "eventType",
      "occurredAt",
      "accountId",
      "customer",
      "subscription",
      "invoice",
      "payment",
      "refund",
      "dispute",
      "metadata",
      "raw",
      "billingEventId",
    ],
    "billing event",
  );
  requireNonEmptyString(record.provider, "billing event provider");
  requireNonEmptyString(record.eventId, "billing event id");
  requireOptionalNonEmptyString(record.accountId, "billing event accountId");
  requireOptionalNonEmptyString(record.billingEventId, "billing event billingEventId");
  if (record.metadata !== null && record.metadata !== undefined) {
    requireObject(record.metadata, "billing event metadata");
  }
  const parsedEventType = z.string().safeParse(record.eventType);
  if (!parsedEventType.success || !BILLING_EVENT_TYPES.has(parsedEventType.data)) {
    throw new TypeError(`unsupported billing event type: ${String(record.eventType)}`);
  }
  const eventName = parsedEventType.data;
  const parsedOccurredAt = z.string().safeParse(record.occurredAt);
  if (
    !parsedOccurredAt.success ||
    !ISO_INSTANT_WITH_OFFSET.test(parsedOccurredAt.data) ||
    Number.isNaN(Date.parse(parsedOccurredAt.data))
  ) {
    throw new TypeError("billing event occurredAt must be an ISO 8601 instant with an offset");
  }

  if (record.customer !== null && record.customer !== undefined) {
    const customer = requireObject(record.customer, `${eventName} customer`);
    rejectUnknownKeys(customer, ["providerCustomerId", "email"], `${eventName} customer`);
    requireOptionalNonEmptyString(
      customer.providerCustomerId,
      `${eventName} customer.providerCustomerId`,
    );
    requireOptionalNonEmptyString(customer.email, `${eventName} customer.email`);
    if (customer.providerCustomerId == null && customer.email == null) {
      throw new TypeError(`${eventName} customer requires providerCustomerId or email`);
    }
  } else if (eventName.startsWith("customer.")) {
    throw new TypeError(`${eventName} requires customer data`);
  }
  if (record.subscription !== null && record.subscription !== undefined) {
    const subscription = requireObject(record.subscription, `${eventName} subscription`);
    rejectUnknownKeys(
      subscription,
      [
        "providerSubscriptionId",
        "status",
        "cancelAtPeriodEnd",
        "periodStart",
        "periodEnd",
        "trialEnd",
        "cancelAt",
        "endedAt",
        "refs",
        "interval",
        "intervalCount",
      ],
      `${eventName} subscription`,
    );
    requireNonEmptyString(
      subscription.providerSubscriptionId,
      `${eventName} subscription.providerSubscriptionId`,
    );
    if (subscription.status != null) {
      requireOneOf(subscription.status, SUBSCRIPTION_STATUSES, `${eventName} subscription.status`);
    }
    requireOptionalBoolean(
      subscription.cancelAtPeriodEnd,
      `${eventName} subscription.cancelAtPeriodEnd`,
    );
    for (const [field, value] of [
      ["periodStart", subscription.periodStart],
      ["periodEnd", subscription.periodEnd],
      ["trialEnd", subscription.trialEnd],
      ["cancelAt", subscription.cancelAt],
      ["endedAt", subscription.endedAt],
    ] as const) {
      requireOptionalInstant(value, `${eventName} subscription.${field}`);
    }
    validateProviderRef(subscription.refs, `${eventName} subscription.refs`);
    if (subscription.interval != null) {
      requireOneOf(subscription.interval, BILLING_INTERVALS, `${eventName} subscription.interval`);
    }
    const intervalCount = z.number().safeParse(subscription.intervalCount);
    if (
      subscription.intervalCount !== null &&
      subscription.intervalCount !== undefined &&
      (!intervalCount.success ||
        !Number.isSafeInteger(intervalCount.data) ||
        intervalCount.data <= 0)
    ) {
      throw new TypeError(
        `${eventName} subscription.intervalCount must be a positive safe integer`,
      );
    }
  } else if (eventName.startsWith("subscription.")) {
    throw new TypeError(`${eventName} requires subscription data`);
  }
  if (record.invoice !== null && record.invoice !== undefined) {
    const invoice = requireObject(record.invoice, `${eventName} invoice`);
    rejectUnknownKeys(
      invoice,
      [
        "providerInvoiceId",
        "status",
        "amountPaidMinor",
        "amountDueMinor",
        "currency",
        "periodStart",
        "periodEnd",
      ],
      `${eventName} invoice`,
    );
    requireNonEmptyString(invoice.providerInvoiceId, `${eventName} invoice.providerInvoiceId`);
    requireOneOf(invoice.status, INVOICE_STATUSES, `${eventName} invoice.status`);
    requireMinorUnits(invoice.amountPaidMinor, `${eventName} invoice.amountPaidMinor`);
    requireMinorUnits(invoice.amountDueMinor, `${eventName} invoice.amountDueMinor`);
    requireCurrency(invoice.currency, `${eventName} invoice.currency`);
    requireOptionalInstant(invoice.periodStart, `${eventName} invoice.periodStart`);
    requireOptionalInstant(invoice.periodEnd, `${eventName} invoice.periodEnd`);
  } else if (eventName.startsWith("invoice.")) {
    throw new TypeError(`${eventName} requires invoice data`);
  }
  if (record.payment !== null && record.payment !== undefined) {
    const payment = requireObject(record.payment, `${eventName} payment`);
    rejectUnknownKeys(
      payment,
      ["providerPaymentId", "amountMinor", "taxMinor", "currency", "refs", "purpose", "status"],
      `${eventName} payment`,
    );
    requireNonEmptyString(payment.providerPaymentId, `${eventName} payment.providerPaymentId`);
    requireMinorUnits(payment.amountMinor, `${eventName} payment.amountMinor`);
    requireMinorUnits(payment.taxMinor, `${eventName} payment.taxMinor`);
    requireCurrency(payment.currency, `${eventName} payment.currency`);
    validateProviderRef(payment.refs, `${eventName} payment.refs`);
    requireOneOf(payment.purpose, PAYMENT_PURPOSES, `${eventName} payment.purpose`);
    requireOneOf(payment.status, PAYMENT_STATUSES, `${eventName} payment.status`);
  } else if (eventName.startsWith("payment.")) {
    throw new TypeError(`${eventName} requires payment data`);
  }
  if (record.refund !== null && record.refund !== undefined) {
    const refund = requireObject(record.refund, `${eventName} refund`);
    rejectUnknownKeys(
      refund,
      ["providerRefundId", "providerPaymentId", "amountMinor", "currency", "reason", "status"],
      `${eventName} refund`,
    );
    requireNonEmptyString(refund.providerRefundId, `${eventName} refund.providerRefundId`);
    requireNonEmptyString(refund.providerPaymentId, `${eventName} refund.providerPaymentId`);
    requireMinorUnits(refund.amountMinor, `${eventName} refund.amountMinor`, true);
    requireCurrency(refund.currency, `${eventName} refund.currency`);
    requireOptionalString(refund.reason, `${eventName} refund.reason`);
    requireOneOf(refund.status, PAYMENT_STATUSES, `${eventName} refund.status`);
  } else if (eventName.startsWith("refund.")) {
    throw new TypeError(`${eventName} requires refund data`);
  }
  if (record.dispute !== null && record.dispute !== undefined) {
    const dispute = requireObject(record.dispute, `${eventName} dispute`);
    rejectUnknownKeys(
      dispute,
      ["providerDisputeId", "providerPaymentId", "status", "reason"],
      `${eventName} dispute`,
    );
    requireNonEmptyString(dispute.providerDisputeId, `${eventName} dispute.providerDisputeId`);
    requireNonEmptyString(dispute.providerPaymentId, `${eventName} dispute.providerPaymentId`);
    requireOneOf(dispute.status, DISPUTE_STATUSES, `${eventName} dispute.status`);
    requireOptionalString(dispute.reason, `${eventName} dispute.reason`);
  } else if (eventName.startsWith("dispute.")) {
    throw new TypeError(`${eventName} requires dispute data`);
  }
}

export type BillingEventHandler = (event: BillingEvent, accountId: string) => Promise<void>;

export interface BillingEventResult {
  handled: boolean;
  action?: string | null;
  error?: string | null;
  subscriptionId?: string | null;
}

export type BillingEventClaim =
  | { status: "claimed"; claimToken: string; billingEventId: string }
  | { status: "duplicate" }
  | { status: "busy" }
  | { status: "invalid_request" }
  | { status: "idempotency_conflict"; billingEventId: string }
  | { status: "max_retries_exceeded"; billingEventId: string }
  | { status: "retry" };
