/**
 * TypeScript types for the provider-agnostic billing module.
 * Mirrors Python bursar/billing/models.py.
 */

// ── Enums ───────────────────────────────────────────────────────────────

export type BillingProvider = "stripe" | "dodo" | "mock";

export const BillingEventType = {
  /** @emitted by stripe, dodo */
  CUSTOMER_CREATED: "customer.created",
  /** @emitted by stripe, dodo */
  CUSTOMER_UPDATED: "customer.updated",
  /** @emitted by stripe, dodo */
  CUSTOMER_DELETED: "customer.deleted",
  /** @emitted by stripe, dodo */
  CHECKOUT_COMPLETED: "checkout.completed",
  /** @aspirational */
  CHECKOUT_EXPIRED: "checkout.expired",
  /** @emitted by stripe, dodo */
  SUBSCRIPTION_CREATED: "subscription.created",
  /** @emitted by stripe, dodo */
  SUBSCRIPTION_UPDATED: "subscription.updated",
  /** @emitted by stripe, dodo */
  SUBSCRIPTION_ACTIVATED: "subscription.activated",
  /** @emitted (mapped from invoice.paid by stripe) */
  SUBSCRIPTION_RENEWED: "subscription.renewed",
  /** @emitted by dodo */
  SUBSCRIPTION_PLAN_CHANGED: "subscription.plan_changed",
  /** @emitted by stripe, dodo */
  SUBSCRIPTION_CANCELLATION_SCHEDULED: "subscription.cancellation_scheduled",
  /** @aspirational */
  SUBSCRIPTION_CANCELLATION_UNSCHEDULED: "subscription.cancellation_unscheduled",
  /** @emitted by stripe, dodo */
  SUBSCRIPTION_CANCELED: "subscription.canceled",
  /** @emitted by dodo */
  SUBSCRIPTION_EXPIRED: "subscription.expired",
  /** @emitted by dodo */
  SUBSCRIPTION_PAUSED: "subscription.paused",
  /** @aspirational */
  SUBSCRIPTION_RESUMED: "subscription.resumed",
  /** @aspirational */
  SUBSCRIPTION_TRIAL_WILL_END: "subscription.trial_will_end",
  /** @aspirational */
  INVOICE_CREATED: "invoice.created",
  /** @aspirational */
  INVOICE_FINALIZED: "invoice.finalized",
  /** @aspirational */
  INVOICE_FINALIZATION_FAILED: "invoice.finalization_failed",
  /** @aspirational */
  INVOICE_UPCOMING: "invoice.upcoming",
  /** @emitted by stripe */
  INVOICE_PAID: "invoice.paid",
  /** @aspirational */
  INVOICE_PAYMENT_FAILED: "invoice.payment_failed",
  /** @aspirational */
  INVOICE_PAYMENT_ACTION_REQUIRED: "invoice.payment_action_required",
  /** @aspirational */
  INVOICE_VOIDED: "invoice.voided",
  /** @emitted by stripe, dodo */
  PAYMENT_SUCCEEDED: "payment.succeeded",
  /** @emitted by stripe */
  PAYMENT_FAILED: "payment.failed",
  /** @emitted by stripe */
  REFUND_CREATED: "refund.created",
  /** @aspirational */
  REFUND_UPDATED: "refund.updated",
  /** @aspirational */
  REFUND_FAILED: "refund.failed",
  /** @emitted by stripe */
  DISPUTE_CREATED: "dispute.created",
  /** @emitted by stripe */
  DISPUTE_CLOSED: "dispute.closed",
  /** @aspirational */
  PAYMENT_METHOD_ATTACHED: "payment_method.attached",
  /** @aspirational */
  PAYMENT_METHOD_UPDATED: "payment_method.updated",
  /** @aspirational */
  PAYMENT_METHOD_DETACHED: "payment_method.detached",
} as const;

export type BillingEventType = (typeof BillingEventType)[keyof typeof BillingEventType];

export type BillingSubscriptionStatus =
  | "incomplete"
  | "incomplete_expired"
  | "trialing"
  | "active"
  | "past_due"
  | "canceled"
  | "unpaid"
  | "paused"
  | "expired";

export type BillingOfferInterval = "day" | "week" | "month" | "year";

export type EntitlementMode = "allowance" | "cycle_grant";

// ── Provider ref ────────────────────────────────────────────────────────

export interface ProviderRef {
  productId?: string | null;
  priceId?: string | null;
  variantId?: string | null;
  lookupKey?: string | null;
}

// ── Subscription grant ──────────────────────────────────────────────────

export interface SubscriptionGrant {
  mode?: EntitlementMode;
  credits?: number | null;
  bucket?: string | null;
  replacePrior?: boolean;
}

// ── Customer info ───────────────────────────────────────────────────────

export interface BillingCustomerInfo {
  providerCustomerId?: string | null;
  email?: string | null;
}

// ── Subscription info ───────────────────────────────────────────────────

export interface BillingSubscriptionInfo {
  providerSubscriptionId: string;
  status?: BillingSubscriptionStatus | null;
  cancelAtPeriodEnd?: boolean | null;
  periodStart?: string | null;
  periodEnd?: string | null;
  refs?: ProviderRef | null;
  interval?: string | null;
  intervalCount?: number | null;
}

// ── Invoice info ────────────────────────────────────────────────────────

export interface BillingInvoiceInfo {
  providerInvoiceId: string;
  status?: string | null;
  amountPaidMinor?: number | null;
  amountDueMinor?: number | null;
  currency?: string | null;
  periodStart?: string | null;
  periodEnd?: string | null;
}

// ── Payment info ────────────────────────────────────────────────────────

export interface BillingPaymentInfo {
  providerPaymentId: string;
  amountMinor: number;
  taxMinor?: number | null;
  currency: string;
  refs?: ProviderRef | null;
  purpose: "subscription" | "credit_topup" | "unknown";
}

// ── Refund info ─────────────────────────────────────────────────────────

export interface BillingRefundInfo {
  providerRefundId: string;
  providerPaymentId?: string | null;
  amountMinor: number;
  currency: string;
  reason?: string | null;
}

// ── Dispute info ────────────────────────────────────────────────────────

export interface BillingDisputeInfo {
  providerDisputeId: string;
  providerPaymentId?: string | null;
  status?: string | null;
  reason?: string | null;
}

// ── Event ───────────────────────────────────────────────────────────────

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
}

// ── Event result ────────────────────────────────────────────────────────

export type BillingEventHandler = (event: BillingEvent, userId: string) => Promise<void>;

export interface BillingEventResult {
  handled: boolean;
  action?: string | null;
  error?: string | null;
  subscriptionId?: string | null;
}

// ── Event claim ─────────────────────────────────────────────────────────

export type BillingEventClaim =
  { status: "claimed"; claimToken: string } | { status: "duplicate" } | { status: "retry" };

// ── Subscription state ──────────────────────────────────────────────────

export interface BillingSubscriptionState {
  userId: string;
  provider: string;
  providerSubscriptionId: string;
  providerCustomerId?: string | null;
  offerKey?: string | null;
  plan?: string | null;
  status?: BillingSubscriptionStatus;
  currentPeriodStart?: string | null;
  currentPeriodEnd?: string | null;
  cancelAtPeriodEnd?: boolean;
  interval?: string | null;
  intervalCount?: number | null;
  metadata?: Record<string, unknown> | null;
}

export type CheckoutIntentStatus = "open" | "completed" | "failed" | "expired";

export interface CheckoutIntent {
  id: string;
  actorKey: string;
  provider: string;
  type: "subscription" | "credit_pack";
  productId: string;
  requestFingerprint: string;
  status: CheckoutIntentStatus;
  providerSessionId?: string | null;
  checkoutUrl?: string | null;
  expiresAt: string;
}

// ── Typed store result types ─────────────────────────────────────────────

export interface BillingGrantResult {
  mode?: string;
  credits?: number | null;
  bucket?: string;
  replacePrior?: boolean;
}

export interface BillingOfferResult {
  offerKey: string;
  plan?: string | null;
  interval?: string;
  intervalCount?: number;
  grant?: BillingGrantResult;
}

export interface BillingTopupResult {
  topupKey: string;
  creditsPerUnit?: number;
  depositTo?: string;
  maxAmountMinor?: number;
}

// ── Config models ───────────────────────────────────────────────────────

export interface BillingOffer {
  plan: string;
  interval?: BillingOfferInterval;
  intervalCount?: number;
  grant?: SubscriptionGrant;
  providers?: Record<string, ProviderRef>;
  validFrom?: string | null;
  validTo?: string | null;
}

export interface BillingCreditTopup {
  depositTo: string;
  creditsPerUnit?: number;
  minAmountMinor?: number;
  maxAmountMinor?: number;
  taxBehavior?: "exclude_tax" | "include_tax";
  providers?: Record<string, ProviderRef>;
}

export interface BillingConfig {
  currency?: string;
  subscriptions?: Record<string, BillingOffer>;
  topups?: Record<string, BillingCreditTopup>;
}

// ── Billing preferences ──────────────────────────────────────────────────

export interface BillingPreferences {
  userId: string;
  autoRecharge: boolean;
  overageProtection: boolean;
  emailNotifications: boolean;
  usageAlerts: boolean;
  invoiceReminders: boolean;
  usageLimitAlerts: boolean;
}

// ── Billing customer record (reverse lookup) ──────────────────────────────

export interface BillingCustomerRecord {
  provider: string;
  providerCustomerId: string;
}
