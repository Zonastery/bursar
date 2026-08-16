import type { AutoRechargeService } from "./auto-recharge-service.js";
import type { Decimal } from "decimal.js";
import type { JsonObject } from "../shared/json.js";
import type {
  BillingAutoRechargeAttempt,
  BillingAutoRechargeAttemptState,
  BillingAutoRechargeProfile,
  BillingCustomerRecord,
  BillingDisputeInfo,
  BillingEvent,
  BillingEventResult,
  BillingInvoiceInfo,
  BillingInvoiceRecord,
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
  operationKey: string;
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
  metadata?: JsonObject;
}

export interface BillingPaymentUpsert {
  provider: string;
  providerPaymentId: string;
  providerInvoiceId?: string | null;
  userId: string;
  amountMinor: number;
  taxMinor: number;
  currency: string;
  purpose: "subscription" | "credit_topup";
  status: "pending" | "succeeded" | "failed" | "canceled";
  providerUpdatedAt: string;
  metadata?: JsonObject | null;
}

export interface BillingCreditGrantCreate {
  paymentId?: string | null;
  subscriptionId?: string | null;
  topupId?: string | null;
  configuredCredits: Decimal;
  quantity: number;
  billingEventId?: string | null;
}

export interface BillingRefundUpsert {
  provider: string;
  providerRefundId: string;
  providerPaymentId: string;
  userId: string;
  amountMinor: number;
  currency: string;
  reason?: string | null;
  status: "pending" | "succeeded" | "failed" | "canceled";
  providerUpdatedAt: string;
  metadata?: JsonObject | null;
}

export interface BillingInvoiceUpsert {
  provider: string;
  providerInvoiceId: string;
  providerSubscriptionId?: string | null;
  userId: string;
  status: "draft" | "open" | "paid" | "void" | "uncollectible";
  amountPaidMinor: number;
  amountDueMinor: number;
  currency: string;
  periodStart?: string | null;
  periodEnd?: string | null;
  metadata?: JsonObject | null;
  providerUpdatedAt: string;
}

export interface BillingDisputeUpsert {
  provider: string;
  providerDisputeId: string;
  providerPaymentId: string;
  status: "needs_response" | "under_review" | "won" | "lost" | "closed";
  reason?: string | null;
  metadata?: JsonObject | null;
  providerUpdatedAt: string;
}

export interface AutoRechargeAttemptClaim {
  userId: string;
  idempotencyKey: string;
}

export interface AutoRechargeAttemptUpdate {
  id: string;
  state: BillingAutoRechargeAttemptState;
  providerAttemptId?: string | null;
  failureCode?: string | null;
  failureMessage?: string | null;
  metadata?: JsonObject;
}

export interface AutoRechargeProviderPaymentUpdate {
  provider: string;
  providerPaymentId: string;
  state: BillingAutoRechargeAttemptState;
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
  getActiveCatalogDocument(): Promise<JsonObject | null>;
  listCancellableProviderSubscriptionIds(userId: string): Promise<string[]>;
  listCancellableSubscriptions(userId: string): Promise<BillingSubscriptionState[]>;
  listBillingInvoices(userId: string): Promise<BillingInvoiceRecord[]>;
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
  upsertAutoRechargeProfile(
    profile: BillingAutoRechargeProfile,
    options?: { resetCooldown?: boolean },
  ): Promise<void>;
  claimAutoRechargeAttempt(
    input: AutoRechargeAttemptClaim,
  ): Promise<BillingAutoRechargeAttempt | null>;
  updateAutoRechargeAttempt(input: AutoRechargeAttemptUpdate): Promise<void>;
  updateAutoRechargeAttemptByProviderPayment(
    input: AutoRechargeProviderPaymentUpdate,
  ): Promise<void>;
  countAutoRechargeAttempts(userId: string, since: string | Date): Promise<number>;
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
export type {
  BillingDisputeInfo,
  BillingInvoiceInfo,
  BillingInvoiceRecord,
  BillingPaymentInfo,
  BillingRefundInfo,
};
