/**
 * TypeScript types for the provider-agnostic billing module.
 * Mirrors Python bursar/billing/models.py.
 */

// ── Enums ───────────────────────────────────────────────────────────────

export type BillingProvider = "stripe" | "dodo" | "mock";

export type BillingEventType =
  /** @emitted by stripe, dodo */
  | "customer.created"
  /** @emitted by stripe, dodo */
  | "customer.updated"
  /** @emitted by stripe, dodo */
  | "customer.deleted"
  /** @emitted by stripe, dodo */
  | "checkout.completed"
  /** @aspirational */
  | "checkout.expired"
  /** @emitted by stripe, dodo */
  | "subscription.created"
  /** @emitted by stripe, dodo */
  | "subscription.updated"
  /** @emitted by stripe, dodo */
  | "subscription.activated"
  /** @emitted (mapped from invoice.paid by stripe) */
  | "subscription.renewed"
  /** @emitted by dodo */
  | "subscription.plan_changed"
  /** @emitted by stripe, dodo */
  | "subscription.cancellation_scheduled"
  /** @aspirational */
  | "subscription.cancellation_unscheduled"
  /** @emitted by stripe, dodo */
  | "subscription.canceled"
  /** @emitted by dodo */
  | "subscription.expired"
  /** @emitted by dodo */
  | "subscription.paused"
  /** @aspirational */
  | "subscription.resumed"
  /** @aspirational */
  | "subscription.trial_will_end"
  /** @aspirational */
  | "invoice.created"
  /** @aspirational */
  | "invoice.finalized"
  /** @aspirational */
  | "invoice.finalization_failed"
  /** @aspirational */
  | "invoice.upcoming"
  /** @emitted by stripe */
  | "invoice.paid"
  /** @aspirational */
  | "invoice.payment_failed"
  /** @aspirational */
  | "invoice.payment_action_required"
  /** @aspirational */
  | "invoice.voided"
  /** @emitted by stripe, dodo */
  | "payment.succeeded"
  /** @emitted by stripe */
  | "payment.failed"
  /** @emitted by stripe */
  | "refund.created"
  /** @aspirational */
  | "refund.updated"
  /** @aspirational */
  | "refund.failed"
  /** @emitted by stripe */
  | "dispute.created"
  /** @emitted by stripe */
  | "dispute.closed"
  /** @aspirational */
  | "payment_method.attached"
  /** @aspirational */
  | "payment_method.updated"
  /** @aspirational */
  | "payment_method.detached";

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
  eventType: string;
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

export interface BillingEventResult {
  handled: boolean;
  action?: string | null;
  error?: string | null;
  subscriptionId?: string | null;
}

// ── Event claim ─────────────────────────────────────────────────────────

export type BillingEventClaim =
  { status: "claimed" } | { status: "duplicate" } | { status: "retry" };

// ── Subscription state ──────────────────────────────────────────────────

export interface BillingSubscriptionState {
  userId: string;
  provider: string;
  providerSubscriptionId: string;
  providerCustomerId?: string | null;
  offerKey?: string | null;
  planKey?: string | null;
  status?: string;
  currentPeriodStart?: string | null;
  currentPeriodEnd?: string | null;
  cancelAtPeriodEnd?: boolean;
  interval?: string | null;
  intervalCount?: number | null;
  metadata?: Record<string, unknown> | null;
}

// ── Config models ───────────────────────────────────────────────────────

export interface BillingOffer {
  plan: string;
  interval?: BillingOfferInterval;
  intervalCount?: number;
  grant?: SubscriptionGrant;
  providers?: Record<string, ProviderRef>;
}

export interface BillingCreditTopup {
  creditsPerUnit?: number;
  depositTo?: string;
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
