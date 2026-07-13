import type {
  BillingConfig,
  BillingCustomerRecord,
  BillingEventClaim,
  BillingOfferResult,
  BillingPreferences,
  BillingSubscriptionState,
  BillingTopupResult,
} from "./billing-types.js";

/**
 * Abstract billing store — provider-agnostic persistence layer for
 * subscription lifecycle state.
 *
 * Mirrors Python bursar/billing/store.py.
 */
export abstract class BillingStore {
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

  abstract completeBillingEvent(provider: string, eventId: string): Promise<void>;

  abstract failBillingEvent(provider: string, eventId: string): Promise<void>;

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

  abstract getActivePricingConfig(): Promise<Record<string, unknown> | null>;

  abstract getUserSubscriptions(userId: string): Promise<BillingSubscriptionState[]>;

  abstract deactivateOtherProviderSubscriptions(
    userId: string,
    keepProvider: string,
  ): Promise<{ userId: string; keepProvider: string; deactivatedCount: number }>;

  abstract getBillingPreferences(userId: string): Promise<BillingPreferences | null>;

  abstract upsertBillingPreferences(prefs: BillingPreferences): Promise<void>;

  abstract getBillingCustomerByUserId(
    userId: string,
    provider?: string | null,
  ): Promise<BillingCustomerRecord | null>;
}
