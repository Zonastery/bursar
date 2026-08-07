import type { ProviderRef } from "./common.js";

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

export const SUBSCRIPTION_STATUS = {
  ACTIVE: "active",
  TRIALING: "trialing",
  CANCELED: "canceled",
  INCOMPLETE: "incomplete",
  PAST_DUE: "past_due",
  INCOMPLETE_EXPIRED: "incomplete_expired",
  UNPAID: "unpaid",
  PAUSED: "paused",
  EXPIRED: "expired",
} as const satisfies Record<string, BillingSubscriptionStatus>;

export interface BillingSubscriptionInfo {
  providerSubscriptionId: string;
  status?: BillingSubscriptionStatus | null;
  cancelAtPeriodEnd?: boolean | null;
  periodStart?: string | null;
  periodEnd?: string | null;
  trialEnd?: string | null;
  cancelAt?: string | null;
  endedAt?: string | null;
  refs?: ProviderRef | null;
  interval?: string | null;
  intervalCount?: number | null;
}

export interface BillingSubscriptionState {
  subscriptionId?: string | null;
  userId: string;
  provider: string;
  providerSubscriptionId: string;
  providerCustomerId?: string | null;
  offerId?: string | null;
  offerKey?: string | null;
  planId?: string | null;
  plan?: string | null;
  status?: BillingSubscriptionStatus;
  currentPeriodStart?: string | null;
  currentPeriodEnd?: string | null;
  trialEnd?: string | null;
  cancelAt?: string | null;
  endedAt?: string | null;
  graceEndsAt?: string | null;
  graceExpiredAt?: string | null;
  providerUpdatedAt?: string | null;
  cancelAtPeriodEnd?: boolean;
  interval?: string | null;
  intervalCount?: number | null;
  metadata?: Record<string, unknown> | null;
}

export type BillingSubscriptionChangeState =
  | "awaiting_payment"
  | "scheduled"
  | "applied"
  | "failed"
  | "canceled";

export interface BillingSubscriptionOfferContext {
  offerId: string;
  offerKey: string;
  planId?: string | null;
  plan?: string | null;
  interval?: string | null;
  intervalCount?: number | null;
}

export interface BillingSubscriptionChange {
  id: string;
  subscriptionId: string;
  fromOfferId: string;
  toOfferId: string;
  fromOffer: BillingSubscriptionOfferContext;
  toOffer: BillingSubscriptionOfferContext;
  effectiveAt: string | null;
  effective: "immediate" | "renewal";
  state: BillingSubscriptionChangeState;
  prorationBehavior: "provider_default" | "invoice_immediately" | "none";
  idempotencyKey: string;
  providerOperationId?: string | null;
  errorMessage?: string | null;
}

export interface BillingSubscriptionChangeInput {
  provider: string;
  providerSubscriptionId: string;
  toOfferId: string;
  effectiveAt: string;
  effective: "immediate" | "renewal";
  idempotencyKey: string;
  prorationBehavior?: "provider_default" | "invoice_immediately" | "none";
}

export type CheckoutIntentStatus = "open" | "completed" | "failed" | "expired";

export interface CheckoutIntent {
  id: string;
  subjectId: string;
  provider: string;
  checkoutKind: "subscription" | "credit_topup";
  productKey: string;
  requestDigest: string;
  status: CheckoutIntentStatus;
  providerSessionId?: string | null;
  checkoutUrl?: string | null;
  expiresAt: string;
}
