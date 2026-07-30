import type { AutoRechargeService } from "./auto-recharge-service.js";
import type {
  BillingAutoRechargeAttempt,
  BillingAutoRechargeProfile,
  BillingCustomerRecord,
  BillingDisputeInfo,
  BillingEvent,
  BillingEventResult,
  BillingInvoiceInfo,
  BillingOfferResult,
  BillingPaymentInfo,
  BillingPreferences,
  BillingRefundInfo,
  BillingSubscriptionChange,
  BillingSubscriptionChangeInput,
  BillingSubscriptionState,
  BillingTopupResult,
  CheckoutIntent,
} from "./types/index.js";

export interface CheckoutIntentCreate {
  subjectId: string;
  provider: string;
  checkoutKind: "subscription" | "credit_topup";
  productKey: string;
  requestDigest: string;
  expiresAt: string;
}

export interface CheckoutIntentUpdate {
  status?: "open" | "completed" | "failed" | "expired";
  providerSessionId?: string | null;
  checkoutUrl?: string | null;
}

export interface BillingSubscriptionChangeUpdate {
  state?: BillingSubscriptionChange["state"];
  providerOperationId?: string | null;
  errorMessage?: string | null;
}

export interface BillingSubscriptionConflictCreate {
  userId?: string | null;
  provider: string;
  duplicateSubscriptionId: string;
  existingSubscriptionId?: string | null;
  eventId?: string | null;
  metadata?: Record<string, unknown>;
}

export interface BillingPaymentUpsert {
  provider: string;
  providerPaymentId: string;
  providerInvoiceId?: string | null;
  userId?: string | null;
  amountMinor?: number;
  taxMinor?: number | null;
  currency?: string | null;
  purpose?: string;
  status?: "pending" | "succeeded" | "failed" | "canceled";
  providerUpdatedAt?: string;
  metadata?: Record<string, unknown> | null;
}

export interface BillingCreditGrantCreate {
  paymentId?: string | null;
  subscriptionId?: string | null;
  topupId?: string | null;
  configuredCredits: number;
  quantity?: number;
  billingEventId?: string | null;
}

export interface BillingRefundUpsert {
  provider: string;
  providerRefundId: string;
  providerPaymentId?: string | null;
  userId?: string | null;
  amountMinor?: number;
  currency?: string | null;
  reason?: string | null;
  status?: "pending" | "succeeded" | "failed" | "canceled";
  providerUpdatedAt?: string;
  metadata?: Record<string, unknown> | null;
}

export interface BillingInvoiceUpsert {
  provider: string;
  providerInvoiceId: string;
  providerSubscriptionId?: string | null;
  userId?: string | null;
  status?: string | null;
  amountPaidMinor?: number | null;
  amountDueMinor?: number | null;
  currency?: string | null;
  periodStart?: string | null;
  periodEnd?: string | null;
  metadata?: Record<string, unknown> | null;
  providerUpdatedAt?: string;
}

export interface BillingDisputeUpsert {
  provider: string;
  providerDisputeId: string;
  providerPaymentId?: string | null;
  userId?: string | null;
  status?: string;
  reason?: string | null;
  metadata?: Record<string, unknown> | null;
  providerUpdatedAt?: string;
}

export interface AutoRechargeAttemptClaim {
  userId: string;
  idempotencyKey: string;
}

export interface AutoRechargeAttemptUpdate {
  id: string;
  state: string;
  providerAttemptId?: string | null;
  failureCode?: string | null;
  failureMessage?: string | null;
  metadata?: Record<string, unknown>;
}

export interface AutoRechargeProviderPaymentUpdate {
  provider: string;
  providerPaymentId: string;
  state: string;
  failureCode?: string | null;
  failureMessage?: string | null;
}

/** Boundary used by payment providers to submit normalized lifecycle events. */
export interface BillingEventSink {
  ingestBillingEvent(event: BillingEvent): Promise<BillingEventResult>;
}

/** Application-facing billing capability exposed by Bursar. */
export interface BillingCapability extends BillingEventSink {
  readonly autoRecharge: AutoRechargeService;
  readonly hasProvisioning: boolean;
  createOrGetCheckoutIntent(input: CheckoutIntentCreate): Promise<CheckoutIntent>;
  updateCheckoutIntent(id: string, update: CheckoutIntentUpdate): Promise<void>;
  getCheckoutIntent(id: string, subjectId: string): Promise<CheckoutIntent | null>;
  getUserSubscription(userId: string): Promise<BillingSubscriptionState | null>;
  getActiveSubscription(userId: string): Promise<BillingSubscriptionState | null>;
  getBlockingSubscription(userId: string): Promise<BillingSubscriptionState | null>;
  getUserPreferences(userId: string): Promise<BillingPreferences | null>;
  getActiveBursarConfig(): Promise<Record<string, unknown> | null>;
  listCancellableProviderSubscriptionIds(userId: string): Promise<string[]>;
  listCancellableSubscriptions(userId: string): Promise<BillingSubscriptionState[]>;
  listBillingInvoices(userId: string): Promise<BillingInvoiceInfo[]>;
  createBillingSubscriptionChange(
    input: BillingSubscriptionChangeInput,
  ): Promise<BillingSubscriptionChange>;
  getOpenBillingSubscriptionChange(
    provider: string,
    providerSubscriptionId: string,
  ): Promise<BillingSubscriptionChange | null>;
  updateBillingSubscriptionChange(
    id: string,
    update: BillingSubscriptionChangeUpdate,
  ): Promise<void>;
  recordSubscriptionConflict(input: BillingSubscriptionConflictCreate): Promise<void>;
  upsertBillingSubscription(state: BillingSubscriptionState): Promise<void>;
  updateUserPreferences(preferences: BillingPreferences): Promise<void>;
  getAutoRechargeProfile(userId: string): Promise<BillingAutoRechargeProfile | null>;
  upsertAutoRechargeProfile(profile: BillingAutoRechargeProfile): Promise<void>;
  claimAutoRechargeAttempt(
    input: AutoRechargeAttemptClaim,
  ): Promise<BillingAutoRechargeAttempt | null>;
  updateAutoRechargeAttempt(input: AutoRechargeAttemptUpdate): Promise<void>;
  updateAutoRechargeAttemptByProviderPayment(
    input: AutoRechargeProviderPaymentUpdate,
  ): Promise<void>;
  countAutoRechargeAttempts(userId: string, since: string | Date | number): Promise<number>;
  expirePastDueGracePeriods(now?: Date): Promise<number>;
  invalidateOfferCache(): void;
  getCustomerByUserId(
    userId: string,
    provider?: string | null,
  ): Promise<BillingCustomerRecord | null>;
  resolveOffer(
    provider: string,
    productId?: string | null,
    priceId?: string | null,
  ): Promise<BillingOfferResult | null>;
  resolveOfferByLookup(provider: string, lookupKey: string): Promise<BillingOfferResult | null>;
  resolveTopup(
    provider: string,
    productId?: string | null,
    priceId?: string | null,
  ): Promise<BillingTopupResult | null>;
  resolveTopupByLookup(provider: string, lookupKey: string): Promise<BillingTopupResult | null>;
  upsertCustomer(
    provider: string,
    providerCustomerId: string,
    userId: string,
    email?: string | null,
  ): Promise<void>;
  pseudonymizeFinancialSubject(userId: string): Promise<void>;
}

// Keep document-record types reachable from the contract module for store
// implementers without forcing them to import the broad billing type barrel.
export type { BillingDisputeInfo, BillingInvoiceInfo, BillingPaymentInfo, BillingRefundInfo };
