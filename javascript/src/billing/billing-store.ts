import type {
  BillingConfig,
  BillingEventClaim,
  BillingSubscriptionState,
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
  ): Promise<Record<string, unknown> | null>;

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

  abstract getUserSubscription(userId: string): Promise<BillingSubscriptionState | null>;

  abstract resolveCreditTopup(
    provider: string,
    productId?: string | null,
    priceId?: string | null,
  ): Promise<Record<string, unknown> | null>;

  abstract computeTopupCredits(
    amountMinor: number,
    topupConfig: Record<string, unknown>,
  ): Promise<number>;

  abstract upsertBillingPayment(
    provider: string,
    providerPaymentId: string,
    providerInvoiceId?: string | null,
    userId?: string | null,
    amountMinor?: number,
    taxMinor?: number | null,
    currency?: string | null,
    purpose?: string,
    metadata?: Record<string, unknown> | null,
  ): Promise<void>;

  abstract upsertBillingRefund(
    provider: string,
    providerRefundId: string,
    providerPaymentId?: string | null,
    userId?: string | null,
    amountMinor?: number,
    currency?: string | null,
    reason?: string | null,
    metadata?: Record<string, unknown> | null,
  ): Promise<void>;

  abstract upsertBillingInvoice(
    provider: string,
    providerInvoiceId: string,
    providerSubscriptionId?: string | null,
    userId?: string | null,
    status?: string | null,
    amountPaidMinor?: number | null,
    amountDueMinor?: number | null,
    currency?: string | null,
    periodStart?: string | null,
    periodEnd?: string | null,
    metadata?: Record<string, unknown> | null,
  ): Promise<void>;

  abstract upsertBillingDispute(
    provider: string,
    providerDisputeId: string,
    providerPaymentId?: string | null,
    userId?: string | null,
    status?: string,
    reason?: string | null,
    metadata?: Record<string, unknown> | null,
  ): Promise<void>;

  abstract getBillingPayment(
    provider: string,
    providerPaymentId: string,
  ): Promise<Record<string, unknown> | null>;

  /**
   * Fallback: query billing_payments directly (including metadata).
   * Some BillingStore implementations may reuse the same RPC as
   * getBillingPayment; those that use a dedicated function that omits
   * metadata should override this to include the metadata column.
   */
  abstract getBillingPaymentDirect(
    provider: string,
    providerPaymentId: string,
  ): Promise<Record<string, unknown> | null>;
}
