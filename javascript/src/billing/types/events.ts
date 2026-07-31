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
  | { status: "retry" };
