import type { BillingStore } from "./billing-store.js";
import type {
  BillingAutoRechargeAttempt,
  BillingAutoRechargeAttemptState,
  BillingAutoRechargeProfile,
  BillingCustomerRecord,
  BillingEvent,
  BillingEventResult,
  BillingOfferResult,
  BillingPreferences,
  BillingSubscriptionChange,
  BillingSubscriptionChangeInput,
  BillingSubscriptionState,
  BillingTopupResult,
  CheckoutIntent,
} from "./types/index.js";
import { SUBSCRIPTION_STATUS } from "./types/index.js";
import { AutoRechargeService } from "./auto-recharge-service.js";
import { BillingEventProcessor } from "./event-processor.js";
import { BillingManagement } from "./management.js";
import type { BillingServiceOptions } from "./service-types.js";
export type { BillingProvisioningPort, BillingServiceOptions } from "./service-types.js";

/**
 * Provider-agnostic billing lifecycle state machine.
 * Mirrors the Python billing service implementation.
 */
export class BillingService {
  readonly autoRecharge: AutoRechargeService;
  private readonly store: BillingStore;
  private readonly management: BillingManagement;
  private readonly events: BillingEventProcessor;

  get hasProvisioning(): boolean {
    return this.events.hasProvisioning;
  }

  constructor(store: BillingStore, options?: BillingServiceOptions) {
    this.store = store;
    this.management = new BillingManagement(store);
    this.events = new BillingEventProcessor(store, options);
    this.autoRecharge = new AutoRechargeService(this);
  }

  async createOrGetCheckoutIntent(
    input: Parameters<BillingStore["createOrGetCheckoutIntent"]>[0],
  ): Promise<CheckoutIntent> {
    return this.management.createOrGetCheckoutIntent(input);
  }

  async updateCheckoutIntent(
    id: string,
    update: Parameters<BillingStore["updateCheckoutIntent"]>[1],
  ): Promise<void> {
    await this.management.updateCheckoutIntent(id, update);
  }

  async getCheckoutIntent(id: string, subjectId: string): Promise<CheckoutIntent | null> {
    return this.management.getCheckoutIntent(id, subjectId);
  }

  async getActiveSubscription(userId: string): Promise<BillingSubscriptionState | null> {
    return this.management.getActiveSubscription(userId);
  }

  async getUserPreferences(userId: string): Promise<BillingPreferences | null> {
    return this.management.getUserPreferences(userId);
  }

  async listBillingInvoices(userId: string) {
    return this.management.listBillingInvoices(userId);
  }

  async upsertBillingSubscription(state: BillingSubscriptionState): Promise<void> {
    await this.management.upsertBillingSubscription(state);
  }

  async createBillingSubscriptionChange(
    input: BillingSubscriptionChangeInput,
  ): Promise<BillingSubscriptionChange> {
    return this.management.createBillingSubscriptionChange(input);
  }

  async getOpenBillingSubscriptionChange(
    provider: string,
    providerSubscriptionId: string,
  ): Promise<BillingSubscriptionChange | null> {
    return this.management.getOpenBillingSubscriptionChange(provider, providerSubscriptionId);
  }

  async updateBillingSubscriptionChange(
    id: string,
    update: Parameters<BillingStore["updateBillingSubscriptionChange"]>[1],
  ): Promise<void> {
    await this.management.updateBillingSubscriptionChange(id, update);
  }

  async recordSubscriptionConflict(
    input: Parameters<BillingStore["recordSubscriptionConflict"]>[0],
  ): Promise<void> {
    await this.management.recordSubscriptionConflict(input);
  }

  async updateUserPreferences(preferences: BillingPreferences): Promise<void> {
    await this.management.updateUserPreferences(preferences);
  }

  async getAutoRechargeProfile(userId: string): Promise<BillingAutoRechargeProfile | null> {
    return this.management.getAutoRechargeProfile(userId);
  }

  async getActiveCatalogDocument(): Promise<Record<string, unknown> | null> {
    return this.management.getActiveCatalogDocument();
  }

  async listCancellableProviderSubscriptionIds(userId: string): Promise<string[]> {
    return this.management.listCancellableProviderSubscriptionIds(userId);
  }

  async listCancellableSubscriptions(userId: string): Promise<BillingSubscriptionState[]> {
    return this.management.listCancellableSubscriptions(userId);
  }

  async pseudonymizeFinancialSubject(userId: string): Promise<void> {
    await this.management.pseudonymizeFinancialSubject(userId);
  }

  async upsertAutoRechargeProfile(
    profile: BillingAutoRechargeProfile,
    options?: { resetCooldown?: boolean },
  ): Promise<void> {
    await this.management.upsertAutoRechargeProfile(profile, options);
  }

  async claimAutoRechargeAttempt(input: {
    userId: string;
    idempotencyKey: string;
  }): Promise<BillingAutoRechargeAttempt | null> {
    return this.management.claimAutoRechargeAttempt(input);
  }

  async updateAutoRechargeAttempt(input: {
    id: string;
    state: BillingAutoRechargeAttemptState;
    providerAttemptId?: string | null;
    failureCode?: string | null;
    failureMessage?: string | null;
    metadata?: Record<string, unknown>;
  }): Promise<void> {
    await this.management.updateAutoRechargeAttempt(input);
  }

  async updateAutoRechargeAttemptByProviderPayment(input: {
    provider: string;
    providerPaymentId: string;
    state: BillingAutoRechargeAttemptState;
    failureCode?: string | null;
    failureMessage?: string | null;
  }): Promise<void> {
    await this.management.updateAutoRechargeAttemptByProviderPayment(input);
  }

  async countAutoRechargeAttempts(userId: string, since: string | Date): Promise<number> {
    return this.management.countAutoRechargeAttempts(userId, since);
  }

  async getCustomerByUserId(
    userId: string,
    provider?: string | null,
  ): Promise<BillingCustomerRecord | null> {
    return this.management.getCustomerByUserId(userId, provider);
  }

  async resolveOffer(
    provider: string,
    productId?: string | null,
    priceId?: string | null,
  ): Promise<BillingOfferResult | null> {
    return this.management.resolveOffer(provider, productId, priceId);
  }

  async resolveOfferByLookup(
    provider: string,
    lookupKey: string,
  ): Promise<BillingOfferResult | null> {
    return this.management.resolveOfferByLookup(provider, lookupKey);
  }

  async resolveTopup(
    provider: string,
    productId?: string | null,
    priceId?: string | null,
  ): Promise<BillingTopupResult | null> {
    return this.management.resolveTopup(provider, productId, priceId);
  }

  async resolveTopupByLookup(
    provider: string,
    lookupKey: string,
  ): Promise<BillingTopupResult | null> {
    return this.management.resolveTopupByLookup(provider, lookupKey);
  }

  async upsertCustomer(
    provider: string,
    providerCustomerId: string,
    userId: string,
    email?: string | null,
  ): Promise<void> {
    await this.management.upsertCustomer(provider, providerCustomerId, userId, email);
  }

  async getUserSubscription(userId: string): Promise<BillingSubscriptionState | null> {
    const subscription = await this.store.getUserSubscription(userId, [
      SUBSCRIPTION_STATUS.ACTIVE,
      SUBSCRIPTION_STATUS.TRIALING,
      SUBSCRIPTION_STATUS.CANCELED,
      SUBSCRIPTION_STATUS.PAST_DUE,
      SUBSCRIPTION_STATUS.INCOMPLETE,
      // EXPIRED excluded — expired subscriptions are not "current" for billing purposes.
    ]);
    return this.expireGraceIfNeeded(subscription);
  }

  async getBlockingSubscription(userId: string): Promise<BillingSubscriptionState | null> {
    const subscription = await this.store.getUserSubscription(userId, [
      SUBSCRIPTION_STATUS.ACTIVE,
      SUBSCRIPTION_STATUS.TRIALING,
      SUBSCRIPTION_STATUS.PAST_DUE,
      SUBSCRIPTION_STATUS.INCOMPLETE,
    ]);
    return this.expireGraceIfNeeded(subscription);
  }

  async expirePastDueGracePeriods(now = new Date()): Promise<number> {
    if (!this.events.hasProvisioning) return 0;
    const asOf = now.toISOString();
    const candidates = await this.store.listExpiredGraceSubscriptions(asOf);
    let expiredCount = 0;
    for (const candidate of candidates) {
      if (!candidate.subscriptionId || !candidate.graceEndsAt) continue;
      const current = await this.store.getBillingSubscription(
        candidate.provider,
        candidate.providerSubscriptionId,
      );
      if (
        current?.status !== "past_due" ||
        current.graceEndsAt !== candidate.graceEndsAt ||
        current.graceExpiredAt
      ) {
        continue;
      }
      await this.events.revokeIfCurrentSubscription(
        candidate.userId,
        candidate.providerSubscriptionId,
      );
      if (
        await this.store.markSubscriptionGraceExpired(
          candidate.subscriptionId,
          candidate.graceEndsAt,
          asOf,
        )
      ) {
        expiredCount += 1;
      }
    }
    return expiredCount;
  }

  private async expireGraceIfNeeded(
    subscription: BillingSubscriptionState | null,
  ): Promise<BillingSubscriptionState | null> {
    if (
      !subscription ||
      !this.events.hasProvisioning ||
      subscription.status !== "past_due" ||
      subscription.graceExpiredAt ||
      !subscription.graceEndsAt ||
      new Date(subscription.graceEndsAt).getTime() > Date.now() ||
      !subscription.subscriptionId
    ) {
      return subscription;
    }

    await this.events.revokeIfCurrentSubscription(
      subscription.userId,
      subscription.providerSubscriptionId,
    );
    const expiredAt = new Date().toISOString();
    const marked = await this.store.markSubscriptionGraceExpired(
      subscription.subscriptionId,
      subscription.graceEndsAt,
      expiredAt,
    );
    return marked ? { ...subscription, graceExpiredAt: expiredAt } : subscription;
  }

  /** Invalidate cached provider offer resolution. */
  invalidateOfferCache(): void {
    this.events.invalidateOfferCache();
  }

  async ingestBillingEvent(event: BillingEvent): Promise<BillingEventResult> {
    return this.events.ingestBillingEvent(event);
  }
}
