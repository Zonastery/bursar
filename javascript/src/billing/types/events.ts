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
  userId?: string | null;
  customer?: BillingCustomerInfo | null;
  subscription?: BillingSubscriptionInfo | null;
  invoice?: BillingInvoiceInfo | null;
  payment?: BillingPaymentInfo | null;
  refund?: BillingRefundInfo | null;
  dispute?: BillingDisputeInfo | null;
  metadata?: Record<string, unknown> | null;
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

function requireObject(value: unknown, field: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new TypeError(`${field} must be an object`);
  }
  return value as Record<string, unknown>;
}

function rejectUnknownKeys(
  value: Record<string, unknown>,
  allowed: readonly string[],
  field: string,
) {
  const known = new Set(allowed);
  const unknown = Object.keys(value).filter((key) => !known.has(key));
  if (unknown.length > 0) {
    throw new TypeError(
      `${field} contains unsupported field${unknown.length === 1 ? "" : "s"}: ${unknown.join(", ")}`,
    );
  }
}

function requireNonEmptyString(value: unknown, field: string): void {
  if (typeof value !== "string" || !value.trim()) {
    throw new TypeError(`${field} must be a non-empty string`);
  }
}

function requireOptionalNonEmptyString(value: unknown, field: string): void {
  if (value !== null && value !== undefined) requireNonEmptyString(value, field);
}

function requireOptionalString(value: unknown, field: string): void {
  if (value !== null && value !== undefined && typeof value !== "string") {
    throw new TypeError(`${field} must be a string`);
  }
}

function requireOptionalBoolean(value: unknown, field: string): void {
  if (value !== null && value !== undefined && typeof value !== "boolean") {
    throw new TypeError(`${field} must be a boolean`);
  }
}

function requireOneOf(value: unknown, allowed: ReadonlySet<string>, field: string): void {
  if (typeof value !== "string" || !allowed.has(value)) {
    throw new TypeError(`${field} has an unsupported value`);
  }
}

function validateProviderRef(value: unknown, field: string): void {
  if (value === null || value === undefined) return;
  const reference = requireObject(value, field);
  const keys = ["productId", "priceId", "variantId", "lookupKey"] as const;
  rejectUnknownKeys(reference, keys, field);
  for (const key of keys) requireOptionalNonEmptyString(reference[key], `${field}.${key}`);
  if (!keys.some((key) => reference[key] !== null && reference[key] !== undefined)) {
    throw new TypeError(`${field} must contain a provider identifier`);
  }
}

function requireMinorUnits(value: unknown, field: string, positive = false): void {
  if (!Number.isSafeInteger(value) || (value as number) < (positive ? 1 : 0)) {
    throw new TypeError(
      `${field} must be a ${positive ? "positive" : "non-negative"} safe integer`,
    );
  }
}

function requireCurrency(value: unknown, field: string): void {
  if (typeof value !== "string" || !/^[A-Z]{3}$/.test(value)) {
    throw new TypeError(`${field} must be an uppercase three-letter currency code`);
  }
}

function requireOptionalInstant(value: unknown, field: string): void {
  if (value === null || value === undefined) return;
  if (
    typeof value !== "string" ||
    !ISO_INSTANT_WITH_OFFSET.test(value) ||
    Number.isNaN(Date.parse(value))
  ) {
    throw new TypeError(`${field} must be an ISO 8601 instant with an offset`);
  }
}

/** Validate an event at the public ingestion boundary before claiming it. */
export function assertBillingEvent(value: unknown): asserts value is BillingEvent {
  const record = requireObject(value, "billing event");
  rejectUnknownKeys(
    record,
    [
      "provider",
      "eventId",
      "eventType",
      "occurredAt",
      "userId",
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
  const event = record as unknown as BillingEvent;
  requireNonEmptyString(event.provider, "billing event provider");
  requireNonEmptyString(event.eventId, "billing event id");
  requireOptionalNonEmptyString(event.userId, "billing event userId");
  requireOptionalNonEmptyString(event.billingEventId, "billing event billingEventId");
  if (event.metadata !== null && event.metadata !== undefined) {
    requireObject(event.metadata, "billing event metadata");
  }
  if (!BILLING_EVENT_TYPES.has(event.eventType)) {
    throw new TypeError(`unsupported billing event type: ${String(event.eventType)}`);
  }
  if (
    typeof event.occurredAt !== "string" ||
    !ISO_INSTANT_WITH_OFFSET.test(event.occurredAt) ||
    Number.isNaN(Date.parse(event.occurredAt))
  ) {
    throw new TypeError("billing event occurredAt must be an ISO 8601 instant with an offset");
  }

  const eventName: string = event.eventType;
  if (event.customer !== null && event.customer !== undefined) {
    const customer = requireObject(event.customer, `${eventName} customer`);
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
  if (event.subscription !== null && event.subscription !== undefined) {
    const subscription = requireObject(event.subscription, `${eventName} subscription`);
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
    if (
      subscription.intervalCount !== null &&
      subscription.intervalCount !== undefined &&
      (!Number.isSafeInteger(subscription.intervalCount) ||
        (subscription.intervalCount as number) <= 0)
    ) {
      throw new TypeError(
        `${eventName} subscription.intervalCount must be a positive safe integer`,
      );
    }
  } else if (eventName.startsWith("subscription.")) {
    throw new TypeError(`${eventName} requires subscription data`);
  }
  if (event.invoice !== null && event.invoice !== undefined) {
    const invoice = requireObject(event.invoice, `${eventName} invoice`);
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
  if (event.payment !== null && event.payment !== undefined) {
    const payment = requireObject(event.payment, `${eventName} payment`);
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
  if (event.refund !== null && event.refund !== undefined) {
    const refund = requireObject(event.refund, `${eventName} refund`);
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
  if (event.dispute !== null && event.dispute !== undefined) {
    const dispute = requireObject(event.dispute, `${eventName} dispute`);
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

export type BillingEventHandler = (event: BillingEvent, userId: string) => Promise<void>;

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
  | { status: "retry" };
