/**
 * TypeScript types for the provider-agnostic billing module.
 * Mirrors Python bursar/billing/models.py.
 */

// ── Enums ───────────────────────────────────────────────────────────────

export type BillingProvider = "stripe" | "dodo";

export type BillingEventType =
  | "customer.created"
  | "customer.updated"
  | "customer.deleted"
  | "checkout.completed"
  | "checkout.expired"
  | "subscription.created"
  | "subscription.updated"
  | "subscription.activated"
  | "subscription.renewed"
  | "subscription.plan_changed"
  | "subscription.cancellation_scheduled"
  | "subscription.cancellation_unscheduled"
  | "subscription.canceled"
  | "subscription.expired"
  | "subscription.paused"
  | "subscription.resumed"
  | "subscription.trial_will_end"
  | "invoice.created"
  | "invoice.finalized"
  | "invoice.finalization_failed"
  | "invoice.upcoming"
  | "invoice.paid"
  | "invoice.payment_failed"
  | "invoice.payment_action_required"
  | "invoice.voided"
  | "payment.succeeded"
  | "payment.failed"
  | "refund.created"
  | "refund.updated"
  | "refund.failed"
  | "dispute.created"
  | "dispute.closed"
  | "payment_method.attached"
  | "payment_method.updated"
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

// ── Provider refs ───────────────────────────────────────────────────────

export interface BillingProviderRefs {
  productId?: string | null;
  priceId?: string | null;
  variantId?: string | null;
  lookupKey?: string | null;
}

export interface BillingSubscriptionOfferRef {
  provider: string;
  productId?: string | null;
  priceId?: string | null;
  variantId?: string | null;
  lookupKey?: string | null;
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
  trialEnd?: string | null;
  refs?: BillingProviderRefs | null;
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
  refs?: BillingProviderRefs | null;
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
  offerKey: string;
  planKey: string;
  interval?: BillingOfferInterval;
  intervalCount?: number;
  entitlementMode?: EntitlementMode;
  cycleGrantCredits?: number | null;
  cycleGrantTier?: string | null;
  cycleGrantReplacePrior?: boolean;
  providerRefs?: Record<string, BillingSubscriptionOfferRef>;
}

export interface BillingCreditTopup {
  tier?: string;
  currency?: string;
  creditsPerMajorUnit?: number;
  minAmountMinor?: number;
  maxAmountMinor?: number;
  taxBehavior?: "exclude_tax" | "include_tax";
  providerRefs?: Record<string, BillingSubscriptionOfferRef>;
}

export interface BillingConfig {
  subscriptions?: Record<string, BillingOffer>;
  creditTopups?: Record<string, BillingCreditTopup>;
}
