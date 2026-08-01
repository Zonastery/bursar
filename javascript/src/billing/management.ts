import type { BillingStore } from "./billing-store.js";
import type {
  BillingAutoRechargeAttempt,
  BillingAutoRechargeProfile,
  BillingCustomerRecord,
  BillingOfferResult,
  BillingPreferences,
  BillingSubscriptionChange,
  BillingSubscriptionChangeInput,
  BillingSubscriptionState,
  BillingSubscriptionStatus,
  BillingTopupResult,
  CheckoutIntent,
} from "./types/index.js";
import { SUBSCRIPTION_STATUS } from "./types/index.js";

/**
 * Public billing reads and commands that delegate directly to BillingStore.
 *
 * Webhook lifecycle orchestration lives in BillingService; this class keeps
 * the stable facade surface separate from that state machine.
 */
export class BillingManagement {
  constructor(private readonly store: BillingStore) {}

  async createOrGetCheckoutIntent(input: Parameters<BillingStore["createOrGetCheckoutIntent"]>[0]) {
    return this.store.createOrGetCheckoutIntent(input);
  }

  async updateCheckoutIntent(
    id: string,
    update: Parameters<BillingStore["updateCheckoutIntent"]>[1],
  ): Promise<void> {
    await this.store.updateCheckoutIntent(id, update);
  }

  async getCheckoutIntent(id: string, subjectId: string): Promise<CheckoutIntent | null> {
    return this.store.getCheckoutIntent(id, subjectId);
  }

  async getActiveSubscription(userId: string): Promise<BillingSubscriptionState | null> {
    return this.store.getUserSubscription(userId, [
      SUBSCRIPTION_STATUS.ACTIVE,
      SUBSCRIPTION_STATUS.TRIALING,
    ]);
  }

  async getUserPreferences(userId: string): Promise<BillingPreferences | null> {
    return this.store.getBillingPreferences(userId);
  }

  async listBillingInvoices(userId: string) {
    return this.store.listBillingInvoices(userId);
  }

  async upsertBillingSubscription(state: BillingSubscriptionState): Promise<void> {
    await this.store.upsertBillingSubscription(state);
  }

  async createBillingSubscriptionChange(
    input: BillingSubscriptionChangeInput,
  ): Promise<BillingSubscriptionChange> {
    return this.store.createBillingSubscriptionChange(input);
  }

  async getOpenBillingSubscriptionChange(
    provider: string,
    providerSubscriptionId: string,
  ): Promise<BillingSubscriptionChange | null> {
    return this.store.getOpenBillingSubscriptionChange(provider, providerSubscriptionId);
  }

  async updateBillingSubscriptionChange(
    id: string,
    update: Parameters<BillingStore["updateBillingSubscriptionChange"]>[1],
  ): Promise<void> {
    await this.store.updateBillingSubscriptionChange(id, update);
  }

  async recordSubscriptionConflict(
    input: Parameters<BillingStore["recordSubscriptionConflict"]>[0],
  ): Promise<void> {
    await this.store.recordSubscriptionConflict(input);
  }

  async updateUserPreferences(preferences: BillingPreferences): Promise<void> {
    await this.store.upsertBillingPreferences(preferences);
  }

  async getAutoRechargeProfile(userId: string): Promise<BillingAutoRechargeProfile | null> {
    return this.store.getAutoRechargeProfile(userId);
  }

  async getActiveBursarConfig(): Promise<Record<string, unknown> | null> {
    return this.store.getActiveBursarConfig();
  }

  async listCancellableProviderSubscriptionIds(userId: string): Promise<string[]> {
    const subscriptions = await this.listCancellableSubscriptions(userId);
    return subscriptions.map((subscription) => subscription.providerSubscriptionId);
  }

  async listCancellableSubscriptions(userId: string): Promise<BillingSubscriptionState[]> {
    const cancellableStatuses = new Set<BillingSubscriptionStatus>([
      SUBSCRIPTION_STATUS.ACTIVE,
      SUBSCRIPTION_STATUS.TRIALING,
      SUBSCRIPTION_STATUS.PAST_DUE,
      SUBSCRIPTION_STATUS.INCOMPLETE,
      SUBSCRIPTION_STATUS.UNPAID,
      SUBSCRIPTION_STATUS.PAUSED,
    ]);
    const subscriptions = await this.store.getUserSubscriptions(userId);
    return subscriptions.filter(
      (subscription) =>
        subscription.status != null &&
        cancellableStatuses.has(subscription.status) &&
        Boolean(subscription.providerSubscriptionId),
    );
  }

  async pseudonymizeFinancialSubject(userId: string): Promise<void> {
    await this.store.pseudonymizeFinancialSubject(userId);
  }

  async upsertAutoRechargeProfile(
    profile: BillingAutoRechargeProfile,
    options?: { resetCooldown?: boolean },
  ): Promise<void> {
    return this.store.upsertAutoRechargeProfile(profile, options);
  }

  async claimAutoRechargeAttempt(input: {
    userId: string;
    idempotencyKey: string;
  }): Promise<BillingAutoRechargeAttempt | null> {
    return this.store.claimAutoRechargeAttempt(input);
  }

  async updateAutoRechargeAttempt(input: {
    id: string;
    state: string;
    providerAttemptId?: string | null;
    failureCode?: string | null;
    failureMessage?: string | null;
    metadata?: Record<string, unknown>;
  }): Promise<void> {
    return this.store.updateAutoRechargeAttempt(input);
  }

  async updateAutoRechargeAttemptByProviderPayment(input: {
    provider: string;
    providerPaymentId: string;
    state: string;
    failureCode?: string | null;
    failureMessage?: string | null;
  }): Promise<void> {
    return this.store.updateAutoRechargeAttemptByProviderPayment(input);
  }

  async countAutoRechargeAttempts(userId: string, since: string | Date): Promise<number> {
    return this.store.countAutoRechargeAttempts(userId, since);
  }

  async getCustomerByUserId(
    userId: string,
    provider?: string | null,
  ): Promise<BillingCustomerRecord | null> {
    return this.store.getBillingCustomerByUserId(userId, provider);
  }

  async resolveOffer(
    provider: string,
    productId?: string | null,
    priceId?: string | null,
  ): Promise<BillingOfferResult | null> {
    return this.store.resolveBillingOffer(provider, productId, priceId);
  }

  async resolveOfferByLookup(
    provider: string,
    lookupKey: string,
  ): Promise<BillingOfferResult | null> {
    return this.store.resolveBillingOfferByLookup(provider, lookupKey);
  }

  async resolveTopup(
    provider: string,
    productId?: string | null,
    priceId?: string | null,
  ): Promise<BillingTopupResult | null> {
    return this.store.resolveCreditTopup(provider, productId, priceId);
  }

  async resolveTopupByLookup(
    provider: string,
    lookupKey: string,
  ): Promise<BillingTopupResult | null> {
    return this.store.resolveCreditTopupByLookup(provider, lookupKey);
  }

  async upsertCustomer(
    provider: string,
    providerCustomerId: string,
    userId: string,
    email?: string | null,
  ): Promise<void> {
    await this.store.upsertBillingCustomer(provider, providerCustomerId, userId, email);
  }
}
