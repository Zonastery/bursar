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
const ISO_INSTANT_WITH_OFFSET = /(?:Z|[+-]\d{2}:\d{2})$/;

function requireNonEmptyString(value: unknown, field: string): void {
  if (typeof value !== "string" || !value.trim()) {
    throw new TypeError(`${field} must be a non-empty string`);
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
export function assertBillingEvent(event: BillingEvent): void {
  requireNonEmptyString(event.provider, "billing event provider");
  requireNonEmptyString(event.eventId, "billing event id");
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
  if (eventName.startsWith("customer.")) {
    if (!event.customer?.providerCustomerId && !event.customer?.email) {
      throw new TypeError(`${eventName} requires customer data`);
    }
  }
  if (eventName.startsWith("subscription.")) {
    requireNonEmptyString(
      event.subscription?.providerSubscriptionId,
      `${eventName} subscription.providerSubscriptionId`,
    );
    for (const [field, value] of [
      ["periodStart", event.subscription?.periodStart],
      ["periodEnd", event.subscription?.periodEnd],
      ["trialEnd", event.subscription?.trialEnd],
      ["cancelAt", event.subscription?.cancelAt],
      ["endedAt", event.subscription?.endedAt],
    ] as const) {
      requireOptionalInstant(value, `${eventName} subscription.${field}`);
    }
    if (
      event.subscription?.intervalCount !== null &&
      event.subscription?.intervalCount !== undefined &&
      (!Number.isSafeInteger(event.subscription.intervalCount) ||
        event.subscription.intervalCount <= 0)
    ) {
      throw new TypeError(
        `${eventName} subscription.intervalCount must be a positive safe integer`,
      );
    }
  }
  if (eventName.startsWith("invoice.")) {
    const invoice = event.invoice;
    if (!invoice) throw new TypeError(`${eventName} requires invoice data`);
    requireNonEmptyString(invoice.providerInvoiceId, `${eventName} invoice.providerInvoiceId`);
    requireMinorUnits(invoice.amountPaidMinor, `${eventName} invoice.amountPaidMinor`);
    requireMinorUnits(invoice.amountDueMinor, `${eventName} invoice.amountDueMinor`);
    requireCurrency(invoice.currency, `${eventName} invoice.currency`);
    requireOptionalInstant(invoice.periodStart, `${eventName} invoice.periodStart`);
    requireOptionalInstant(invoice.periodEnd, `${eventName} invoice.periodEnd`);
  }
  if (eventName.startsWith("payment.")) {
    const payment = event.payment;
    if (!payment) throw new TypeError(`${eventName} requires payment data`);
    requireNonEmptyString(payment.providerPaymentId, `${eventName} payment.providerPaymentId`);
    requireMinorUnits(payment.amountMinor, `${eventName} payment.amountMinor`);
    requireMinorUnits(payment.taxMinor, `${eventName} payment.taxMinor`);
    requireCurrency(payment.currency, `${eventName} payment.currency`);
  }
  if (eventName.startsWith("refund.")) {
    const refund = event.refund;
    if (!refund) throw new TypeError(`${eventName} requires refund data`);
    requireNonEmptyString(refund.providerRefundId, `${eventName} refund.providerRefundId`);
    requireNonEmptyString(refund.providerPaymentId, `${eventName} refund.providerPaymentId`);
    requireMinorUnits(refund.amountMinor, `${eventName} refund.amountMinor`, true);
    requireCurrency(refund.currency, `${eventName} refund.currency`);
  }
  if (eventName.startsWith("dispute.")) {
    const dispute = event.dispute;
    if (!dispute) throw new TypeError(`${eventName} requires dispute data`);
    requireNonEmptyString(dispute.providerDisputeId, `${eventName} dispute.providerDisputeId`);
    requireNonEmptyString(dispute.providerPaymentId, `${eventName} dispute.providerPaymentId`);
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
