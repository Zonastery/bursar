import type {
  BillingConfig,
  BillingAutoRechargeAttempt,
  BillingAutoRechargeProfile,
  BillingCustomerRecord,
  BillingEventClaim,
  BillingOfferResult,
  BillingPreferences,
  BillingSubscriptionChange,
  BillingSubscriptionState,
  CheckoutIntent,
  BillingTopupResult,
  BillingInvoiceInfo,
} from "./billing-types.js";

/**
 * Abstract billing store — provider-agnostic persistence layer for
 * subscription lifecycle state.
 *
 * Mirrors Python bursar/billing/store.py.
 */
export abstract class BillingStore {
  abstract createOrGetCheckoutIntent(input: {
    actorKey: string;
    provider: string;
    type: "subscription" | "credit_pack";
    productId: string;
    requestFingerprint: string;
    expiresAt: string;
  }): Promise<CheckoutIntent>;

  abstract updateCheckoutIntent(
    id: string,
    update: {
      status?: "open" | "completed" | "failed" | "expired";
      providerSessionId?: string | null;
      checkoutUrl?: string | null;
    },
  ): Promise<void>;

  abstract getCheckoutIntent(id: string, actorKey: string): Promise<CheckoutIntent | null>;

  abstract syncBillingFromConfig(config: BillingConfig): Promise<void>;

  abstract resolveBillingOffer(
    provider: string,
    productId?: string | null,
    priceId?: string | null,
  ): Promise<BillingOfferResult | null>;

  abstract claimBillingEvent(
    provider: string,
    eventId: string,
    eventType: string,
  ): Promise<BillingEventClaim>;

  abstract completeBillingEvent(
    provider: string,
    eventId: string,
    claimToken: string,
  ): Promise<void>;

  abstract failBillingEvent(
    provider: string,
    eventId: string,
    claimToken: string,
    error?: string,
  ): Promise<void>;

  abstract upsertBillingCustomer(
    provider: string,
    providerCustomerId: string,
    userId: string,
    email?: string | null,
  ): Promise<void>;

  abstract upsertBillingSubscription(state: BillingSubscriptionState): Promise<void>;

  abstract getBillingCustomer(provider: string, providerCustomerId: string): Promise<string | null>;

  abstract getBillingSubscription(
    provider: string,
    providerSubscriptionId: string,
  ): Promise<BillingSubscriptionState | null>;

  abstract createBillingSubscriptionChange(
    input: Omit<BillingSubscriptionChange, "id">,
  ): Promise<BillingSubscriptionChange>;

  abstract getOpenBillingSubscriptionChange(
    provider: string,
    providerSubscriptionId: string,
  ): Promise<BillingSubscriptionChange | null>;

  abstract listExpiredGraceSubscriptions(now: string): Promise<BillingSubscriptionState[]>;

  abstract updateBillingSubscriptionChange(
    id: string,
    update: Partial<
      Pick<BillingSubscriptionChange, "state" | "providerOperationId" | "effectiveDate">
    >,
  ): Promise<void>;

  abstract getUserSubscription(
    userId: string,
    statuses?: string[],
  ): Promise<BillingSubscriptionState | null>;

  abstract resolveCreditTopup(
    provider: string,
    productId?: string | null,
    priceId?: string | null,
  ): Promise<BillingTopupResult | null>;

  abstract resolveBillingOfferByLookup(
    provider: string,
    lookupKey: string,
  ): Promise<BillingOfferResult | null>;

  abstract computeTopupCredits(
    amountMinor: number,
    topupConfig: BillingTopupResult,
  ): Promise<number>;

  abstract upsertBillingPayment(options: {
    provider: string;
    providerPaymentId: string;
    providerInvoiceId?: string | null;
    userId?: string | null;
    amountMinor?: number;
    taxMinor?: number | null;
    currency?: string | null;
    purpose?: string;
    metadata?: Record<string, unknown> | null;
  }): Promise<void>;

  abstract listBillingInvoices(userId: string): Promise<BillingInvoiceInfo[]>;

  abstract upsertBillingRefund(options: {
    provider: string;
    providerRefundId: string;
    providerPaymentId?: string | null;
    userId?: string | null;
    amountMinor?: number;
    currency?: string | null;
    reason?: string | null;
    metadata?: Record<string, unknown> | null;
  }): Promise<void>;

  abstract upsertBillingInvoice(options: {
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
  }): Promise<void>;

  abstract upsertBillingDispute(options: {
    provider: string;
    providerDisputeId: string;
    providerPaymentId?: string | null;
    userId?: string | null;
    status?: string;
    reason?: string | null;
    metadata?: Record<string, unknown> | null;
  }): Promise<void>;

  abstract getBillingPayment(
    provider: string,
    providerPaymentId: string,
  ): Promise<Record<string, unknown> | null>;

  abstract getActiveBursarConfig(): Promise<Record<string, unknown> | null>;

  abstract pseudonymizeFinancialSubject(userId: string): Promise<void>;

  abstract getUserSubscriptions(userId: string): Promise<BillingSubscriptionState[]>;

  abstract recordSubscriptionConflict(input: {
    userId?: string | null;
    provider: string;
    duplicateSubscriptionId: string;
    existingSubscriptionId?: string | null;
    eventId?: string | null;
    metadata?: Record<string, unknown>;
  }): Promise<void>;

  abstract deactivateOtherProviderSubscriptions(
    userId: string,
    keepProvider: string,
  ): Promise<{ userId: string; keepProvider: string; deactivatedCount: number }>;

  abstract getBillingPreferences(userId: string): Promise<BillingPreferences | null>;

  abstract upsertBillingPreferences(prefs: BillingPreferences): Promise<void>;
  abstract getAutoRechargeProfile(userId: string): Promise<BillingAutoRechargeProfile | null>;
  abstract upsertAutoRechargeProfile(profile: BillingAutoRechargeProfile): Promise<void>;
  abstract claimAutoRechargeAttempt(input: {
    userId: string;
    provider: string;
    topupKey: string;
    quantity: number;
    maxRecharges: number;
    windowDays: number;
  }): Promise<BillingAutoRechargeAttempt | null>;
  abstract updateAutoRechargeAttempt(input: {
    id: string;
    state: string;
    providerPaymentId?: string | null;
    failureCode?: string | null;
    actionUrl?: string | null;
  }): Promise<void>;
  abstract updateAutoRechargeAttemptByProviderPayment(input: {
    provider: string;
    providerPaymentId: string;
    state: string;
    failureCode?: string | null;
  }): Promise<void>;
  abstract countAutoRechargeAttempts(userId: string, windowDays: number): Promise<number>;

  abstract getBillingCustomerByUserId(
    userId: string,
    provider?: string | null,
  ): Promise<BillingCustomerRecord | null>;
}
