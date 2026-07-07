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
    variantId?: string | null,
    interval?: string | null,
    intervalCount?: number | null,
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

  abstract resolveCreditTopup(
    provider: string,
    productId?: string | null,
    priceId?: string | null,
  ): Promise<Record<string, unknown> | null>;

  abstract computeTopupCredits(
    amountMinor: number,
    topupConfig: Record<string, unknown>,
  ): Promise<number>;
}
