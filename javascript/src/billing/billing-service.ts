import Decimal from "decimal.js";
import { LRUCache } from "lru-cache";
import type { BillingStore } from "./billing-store.js";
import type {
  BillingEvent,
  BillingEventHandler,
  BillingEventResult,
  BillingCustomerRecord,
  BillingOfferResult,
  BillingPreferences,
  BillingSubscriptionState,
  BillingSubscriptionStatus,
  BillingTopupResult,
} from "./billing-types.js";
import { BillingEventType } from "./billing-types.js";
import {
  type NormalizedProviderLogger,
  type ProviderLogger,
  normalizeProviderLogger,
} from "../providers/types.js";
import { SUBSCRIPTION_STATUS } from "../repositories/billing/subscription.js";

interface OfferCacheValue {
  offer: BillingOfferResult | null;
}

interface OfferContext {
  provider: string;
  productId: string | null;
  priceId: string | null;
}

type ResolveUserFn = (
  provider: string,
  providerCustomerId: string | null,
  email: string | null,
) => string | null;

export interface BillingServiceOptions {
  /** Narrow credit capability used by subscription provisioning. */
  provisioning?: BillingProvisioningPort | null;
  resolveUser?: ResolveUserFn | null;
  eventHandlers?: Partial<Record<BillingEventType, BillingEventHandler>>;
  cancelPriorProviders?: boolean;
  logger?: ProviderLogger | null;
}

/**
 * Credit operations billing is allowed to request. Keeping this port narrow
 * prevents billing code from depending on the full credit manager surface.
 */
export interface BillingProvisioningPort {
  setUserPlan(userId: string, planKey: string, planAssignedAt?: Date | null): Promise<void>;
  unsetUserPlan(userId: string): Promise<void>;
  addCredits(
    userId: string,
    amount: Decimal | number,
    options?: { type?: string; bucket?: string | null },
  ): Promise<unknown>;
  deductCredits(
    userId: string,
    amount: Decimal | number,
    options?: { txType?: string; bucket?: string | null },
  ): Promise<unknown>;
  revokeCreditsByTxType(userId: string, txType: string): Promise<unknown>;
}

/**
 * Provider-agnostic billing lifecycle state machine.
 * Mirrors the Python billing service implementation.
 */
export class BillingService {
  private store: BillingStore;
  private provisioning: BillingProvisioningPort | null;
  private resolveUser: ResolveUserFn | null;
  private eventHandlers: Partial<Record<BillingEventType, BillingEventHandler>>;
  private cancelPriorProviders: boolean;
  private logger: NormalizedProviderLogger;
  private handlerMap: Record<string, (event: BillingEvent) => Promise<BillingEventResult>>;
  private offerCache: LRUCache<string, OfferCacheValue, OfferContext>;
  private readonly IGNORED_EVENT_TYPES: Set<BillingEventType> = new Set([
    BillingEventType.CHECKOUT_EXPIRED,
    BillingEventType.INVOICE_UPCOMING,
  ]);

  get hasProvisioning(): boolean {
    return this.provisioning !== null;
  }

  constructor(store: BillingStore, options?: BillingServiceOptions) {
    this.store = store;
    this.provisioning = options?.provisioning ?? null;
    this.resolveUser = options?.resolveUser ?? null;
    this.eventHandlers = options?.eventHandlers ?? {};
    this.cancelPriorProviders = options?.cancelPriorProviders ?? true;
    this.logger = normalizeProviderLogger(options?.logger);
    this.offerCache = new LRUCache<string, OfferCacheValue, OfferContext>({
      max: 100,
      ttl: 60_000,
      allowStale: false,
      fetchMethod: async (_, __, { context }) => {
        const offer = await this.store.resolveBillingOffer(
          context.provider,
          context.productId,
          context.priceId,
        );
        return { offer };
      },
    });
    this.handlerMap = {
      [BillingEventType.CUSTOMER_CREATED]: this.handleCustomerCreated.bind(this),
      [BillingEventType.CUSTOMER_UPDATED]: this.handleCustomerCreated.bind(this),
      [BillingEventType.CUSTOMER_DELETED]: this.handleCustomerDeleted.bind(this),
      [BillingEventType.CHECKOUT_COMPLETED]: this.handleCheckoutCompleted.bind(this),
      [BillingEventType.SUBSCRIPTION_CREATED]: this.handleSubscriptionCreated.bind(this),
      [BillingEventType.SUBSCRIPTION_UPDATED]: this.handleSubscriptionUpdated.bind(this),
      [BillingEventType.SUBSCRIPTION_ACTIVATED]: this.handleSubscriptionActivated.bind(this),
      [BillingEventType.SUBSCRIPTION_RENEWED]: this.handleSubscriptionRenewed.bind(this),
      [BillingEventType.SUBSCRIPTION_PLAN_CHANGED]: this.handleSubscriptionPlanChanged.bind(this),
      [BillingEventType.SUBSCRIPTION_CANCELLATION_SCHEDULED]:
        this.handleCancellationScheduled.bind(this),
      [BillingEventType.SUBSCRIPTION_CANCELLATION_UNSCHEDULED]:
        this.handleCancellationUnscheduled.bind(this),
      [BillingEventType.SUBSCRIPTION_CANCELED]: this.handleSubscriptionCanceled.bind(this),
      [BillingEventType.SUBSCRIPTION_EXPIRED]: this.handleSubscriptionExpired.bind(this),
      [BillingEventType.SUBSCRIPTION_PAUSED]: this.handleSubscriptionPaused.bind(this),
      [BillingEventType.SUBSCRIPTION_RESUMED]: this.handleSubscriptionResumed.bind(this),
      [BillingEventType.SUBSCRIPTION_TRIAL_WILL_END]: this.handleTrialWillEnd.bind(this),
      [BillingEventType.INVOICE_PAID]: this.handleInvoicePaid.bind(this),
      [BillingEventType.PAYMENT_SUCCEEDED]: this.handlePaymentSucceeded.bind(this),
      [BillingEventType.PAYMENT_FAILED]: this.handlePaymentFailed.bind(this),
      [BillingEventType.REFUND_CREATED]: this.handleRefundCreated.bind(this),
      [BillingEventType.DISPUTE_CREATED]: this.handleDisputeCreated.bind(this),
      [BillingEventType.DISPUTE_CLOSED]: this.handleDisputeClosed.bind(this),
    };
  }

  async getUserSubscription(userId: string): Promise<BillingSubscriptionState | null> {
    return this.store.getUserSubscription(userId, [
      SUBSCRIPTION_STATUS.ACTIVE,
      SUBSCRIPTION_STATUS.TRIALING,
      SUBSCRIPTION_STATUS.CANCELED,
      SUBSCRIPTION_STATUS.PAST_DUE,
      SUBSCRIPTION_STATUS.INCOMPLETE,
      // EXPIRED excluded — expired subscriptions are not "current" for billing purposes.
    ]);
  }

  async createOrGetCheckoutIntent(input: Parameters<BillingStore["createOrGetCheckoutIntent"]>[0]) {
    return this.store.createOrGetCheckoutIntent(input);
  }

  async updateCheckoutIntent(
    id: string,
    update: Parameters<BillingStore["updateCheckoutIntent"]>[1],
  ): Promise<void> {
    await this.store.updateCheckoutIntent(id, update);
  }

  async getActiveSubscription(userId: string): Promise<BillingSubscriptionState | null> {
    return this.store.getUserSubscription(userId, [
      SUBSCRIPTION_STATUS.ACTIVE,
      SUBSCRIPTION_STATUS.TRIALING,
    ]);
  }

  async getBlockingSubscription(userId: string): Promise<BillingSubscriptionState | null> {
    return this.store.getUserSubscription(userId, [
      SUBSCRIPTION_STATUS.ACTIVE,
      SUBSCRIPTION_STATUS.TRIALING,
      SUBSCRIPTION_STATUS.PAST_DUE,
      SUBSCRIPTION_STATUS.INCOMPLETE,
    ]);
  }

  async getUserPreferences(userId: string): Promise<BillingPreferences | null> {
    return this.store.getBillingPreferences(userId);
  }

  async recordSubscriptionConflict(
    input: Parameters<BillingStore["recordSubscriptionConflict"]>[0],
  ) {
    await this.store.recordSubscriptionConflict(input);
  }

  async updateUserPreferences(prefs: BillingPreferences): Promise<void> {
    await this.store.upsertBillingPreferences(prefs);
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

  async resolveTopup(
    provider: string,
    productId?: string | null,
    priceId?: string | null,
  ): Promise<BillingTopupResult | null> {
    return this.store.resolveCreditTopup(provider, productId, priceId);
  }

  async upsertCustomer(
    provider: string,
    providerCustomerId: string,
    userId: string,
    email?: string | null,
  ): Promise<void> {
    await this.store.upsertBillingCustomer(provider, providerCustomerId, userId, email);
  }

  /** Invalidate the offer cache so the next resolution call re-fetches fresh data. */
  invalidateOfferCache(): void {
    this.offerCache.clear();
  }

  async ingestBillingEvent(event: BillingEvent): Promise<BillingEventResult> {
    this.logger.debug("[BillingService] ingestBillingEvent", {
      eventId: event.eventId,
      provider: event.provider,
      eventType: event.eventType,
    });
    const claim = await this.store.claimBillingEvent(
      event.provider,
      event.eventId,
      event.eventType,
    );
    this.logger.debug("[BillingService] claim status", {
      status: claim.status,
      eventId: event.eventId,
    });

    if (claim.status === "duplicate") {
      this.logger.debug("[BillingService] duplicate event", { eventId: event.eventId });
      return { handled: true, action: "duplicate" };
    }
    if (claim.status === "retry") {
      this.logger.warn("[BillingService] claim retry", { eventId: event.eventId });
      return { handled: false, error: "claim_failed_retry" };
    }

    try {
      const result = await this.routeEvent(event);
      this.logger.debug("[BillingService] routeEvent result", { result, eventId: event.eventId });
      await this.store.completeBillingEvent(event.provider, event.eventId, claim.claimToken);
      return result;
    } catch (err) {
      this.logger.error(
        `[BillingService] failed to handle billing event ${event.provider}/${event.eventId}`,
        { error: err instanceof Error ? err.message : String(err) },
      );
      await this.store.failBillingEvent(
        event.provider,
        event.eventId,
        claim.claimToken,
        err instanceof Error ? err.message : String(err),
      );
      return {
        handled: false,
        error: err instanceof Error ? err.message : String(err),
      };
    }
  }

  private async routeEvent(event: BillingEvent): Promise<BillingEventResult> {
    const handler = this.handlerMap[event.eventType];
    if (!handler) {
      if (this.IGNORED_EVENT_TYPES.has(event.eventType)) {
        if (event.eventType === BillingEventType.CHECKOUT_EXPIRED) {
          await this.updateCheckoutIntentFromEvent(event, "expired");
        }
        return { handled: true, action: "ignored" };
      }
      return { handled: false, error: "unhandled_event_type" };
    }
    const result = await handler(event);
    if (result.handled) {
      await this.fireEventHandlers(event, event.userId ?? null);
    }
    return result;
  }

  private async fireEventHandlers(event: BillingEvent, userId: string | null): Promise<void> {
    if (!userId) return;
    const handler = this.eventHandlers[event.eventType];
    if (!handler) return;
    try {
      await handler(event, userId);
    } catch (err) {
      this.logger.error(
        `[BillingService] event handler failed for ${event.provider}/${event.eventId}`,
        { error: err instanceof Error ? err.message : String(err) },
      );
    }
  }

  private async updateCheckoutIntentFromEvent(
    event: BillingEvent,
    status: "completed" | "failed" | "expired",
  ): Promise<void> {
    const intentId = event.metadata?.checkout_intent_id;
    if (typeof intentId !== "string" || !intentId) return;
    await this.store.updateCheckoutIntent(intentId, { status });
  }

  /**
   * Resolve userId from the event, mutating event.userId so that
   * routeEvent's blanket fireEventCallback can read it. Each ingestBillingEvent
   * call creates a fresh event object, so mutation is safe.
   */
  private async resolveUserId(event: BillingEvent): Promise<string | null> {
    if (event.userId) return event.userId;
    if (event.customer?.providerCustomerId) {
      const uid = await this.store.getBillingCustomer(
        event.provider,
        event.customer.providerCustomerId,
      );
      if (uid) {
        event.userId = uid;
        return uid;
      }
    }
    if (this.resolveUser && event.customer?.providerCustomerId) {
      const uid = this.resolveUser(
        event.provider,
        event.customer.providerCustomerId,
        event.customer.email ?? null,
      );
      if (uid) {
        event.userId = uid;
        return uid;
      }
    }
    if (event.subscription?.providerSubscriptionId) {
      const existing = await this.store.getBillingSubscription(
        event.provider,
        event.subscription.providerSubscriptionId,
      );
      if (existing?.userId) {
        event.userId = existing.userId;
        return existing.userId;
      }
    }
    return null;
  }

  private async resolveBillingOfferCached(
    provider: string,
    productId: string | null,
    priceId: string | null,
  ): Promise<BillingOfferResult | null> {
    const cacheKey = `${provider}:${productId ?? ""}:${priceId ?? ""}`;
    const result = await this.offerCache.fetch(cacheKey, {
      context: { provider, productId, priceId },
    });
    return result?.offer ?? null;
  }

  private async handleCustomerCreated(event: BillingEvent): Promise<BillingEventResult> {
    this.logger.info("[BillingService] handleCustomerCreated", {
      provider: event.provider,
      customerId: event.customer?.providerCustomerId,
    });
    if (event.customer?.providerCustomerId) {
      const uid = await this.resolveUserId(event);
      if (uid) {
        await this.store.upsertBillingCustomer(
          event.provider,
          event.customer.providerCustomerId,
          uid,
          event.customer.email ?? null,
        );
      }
    }
    return { handled: true, action: "customer_created" };
  }

  private async handleCustomerDeleted(event: BillingEvent): Promise<BillingEventResult> {
    this.logger.info("[BillingService] handleCustomerDeleted", {
      provider: event.provider,
      customerId: event.customer?.providerCustomerId,
    });
    if (event.customer?.providerCustomerId) {
      const uid = await this.resolveUserId(event);
      if (uid && this.provisioning) {
        await this.revokeSubscription(uid);
      }
    }
    return { handled: true, action: "customer_deleted" };
  }

  private async handleCheckoutCompleted(event: BillingEvent): Promise<BillingEventResult> {
    this.logger.info("[BillingService] handleCheckoutCompleted", {
      provider: event.provider,
      eventId: event.eventId,
      hasUserId: Boolean(event.userId),
    });
    if (event.customer?.providerCustomerId) {
      const uid = await this.resolveUserId(event);
      if (uid) {
        await this.store.upsertBillingCustomer(
          event.provider,
          event.customer.providerCustomerId,
          uid,
          event.customer.email ?? null,
        );
      }
    }
    if (event.subscription?.providerSubscriptionId) {
      return this.handleSubscriptionCreated(event);
    }
    await this.updateCheckoutIntentFromEvent(event, "completed");
    return { handled: true, action: "checkout_completed" };
  }

  private async getExistingSubscription(
    event: BillingEvent,
  ): Promise<BillingSubscriptionState | null> {
    if (!event.subscription?.providerSubscriptionId) return null;
    return this.store.getBillingSubscription(
      event.provider,
      event.subscription.providerSubscriptionId,
    );
  }

  private buildSubscriptionState(
    event: BillingEvent,
    userId: string,
    existing: BillingSubscriptionState | null,
    overrides?: {
      status?: BillingSubscriptionStatus | null;
      cancelAtPeriodEnd?: boolean | null;
      offerKey?: string | null;
      plan?: string | null;
      interval?: string | null;
      intervalCount?: number | null;
    },
  ): BillingSubscriptionState {
    if (!event.subscription) {
      throw new Error("no_subscription_data");
    }
    const sub = event.subscription;
    return {
      userId,
      provider: event.provider,
      providerSubscriptionId: sub.providerSubscriptionId,
      providerCustomerId:
        event.customer?.providerCustomerId ?? existing?.providerCustomerId ?? null,
      offerKey: overrides?.offerKey ?? existing?.offerKey ?? null,
      plan: overrides?.plan ?? existing?.plan ?? null,
      status: overrides?.status ?? sub.status ?? existing?.status ?? "incomplete",
      currentPeriodStart: sub.periodStart ?? existing?.currentPeriodStart ?? null,
      currentPeriodEnd: sub.periodEnd ?? existing?.currentPeriodEnd ?? null,
      cancelAtPeriodEnd:
        overrides?.cancelAtPeriodEnd ??
        sub.cancelAtPeriodEnd ??
        existing?.cancelAtPeriodEnd ??
        false,
      interval:
        overrides?.interval ??
        sub.interval ??
        (event.metadata?.billing_interval as string | undefined) ??
        existing?.interval ??
        null,
      intervalCount:
        overrides?.intervalCount ?? sub.intervalCount ?? existing?.intervalCount ?? null,
      metadata: event.metadata ?? existing?.metadata ?? null,
    };
  }

  private async resolveOfferAndKeys(event: BillingEvent): Promise<{
    offer: BillingOfferResult | null;
    offerKey: string | null;
    plan: string | null;
  }> {
    const refs = event.subscription?.refs;
    if (!refs) return { offer: null, offerKey: null, plan: null };

    // Tier 1: Resolve by price/product ID
    const offer = await this.resolveBillingOfferCached(
      event.provider,
      refs.productId ?? null,
      refs.priceId ?? null,
    );
    if (offer) {
      return {
        offer,
        offerKey: (offer?.offerKey as string | null) ?? null,
        plan: (offer?.plan as string | null) ?? null,
      };
    }

    // Tier 2: Resolve by lookup_key
    if (refs.lookupKey) {
      const lookupOffer = await this.store.resolveBillingOfferByLookup(
        event.provider,
        refs.lookupKey,
      );
      if (lookupOffer) {
        return {
          offer: lookupOffer,
          offerKey: (lookupOffer?.offerKey as string | null) ?? null,
          plan: (lookupOffer?.plan as string | null) ?? null,
        };
      }

      this.logger.error(
        `[BillingService] resolveOfferAndKeys: no offer found for ${event.provider}/${refs.lookupKey}`,
      );
    }

    return { offer: null, offerKey: null, plan: null };
  }

  private async handleSubscriptionCreated(event: BillingEvent): Promise<BillingEventResult> {
    const uid = await this.resolveUserId(event);
    this.logger.info("[BillingService] handleSubscriptionCreated", {
      eventId: event.eventId,
      provider: event.provider,
      hasUserId: Boolean(uid),
    });
    if (!uid) return { handled: false, error: "user_not_found" };
    if (!event.subscription?.providerSubscriptionId)
      return { handled: false, error: "no_subscription_data" };
    const subscriptionId = event.subscription.providerSubscriptionId;
    const existing = await this.getExistingSubscription(event);
    const blockingStatuses = new Set(["active", "trialing", "past_due", "incomplete"]);
    const existingForProvider = (await this.store.getUserSubscriptions(uid)).find(
      (candidate) =>
        candidate.provider === event.provider &&
        candidate.providerSubscriptionId !== subscriptionId &&
        blockingStatuses.has(candidate.status ?? ""),
    );
    if (existingForProvider) {
      await this.store.recordSubscriptionConflict({
        userId: uid,
        provider: event.provider,
        duplicateSubscriptionId: subscriptionId,
        existingSubscriptionId: existingForProvider.providerSubscriptionId,
        eventId: event.eventId,
        metadata: event.metadata ?? undefined,
      });
      this.logger.warn("[BillingService] subscription conflict quarantined", {
        userId: uid,
        provider: event.provider,
        duplicateSubscriptionId: subscriptionId,
        existingSubscriptionId: existingForProvider.providerSubscriptionId,
      });
      await this.updateCheckoutIntentFromEvent(event, "completed");
      return { handled: true, action: "subscription_conflict" };
    }
    const { offer, offerKey, plan } = await this.resolveOfferAndKeys(event);
    this.logger.debug("[BillingService] resolveOfferAndKeys", {
      offerKey,
      plan,
      eventId: event.eventId,
    });
    const subscriptionState = this.buildSubscriptionState(event, uid, existing, {
      status: event.subscription.status ?? "incomplete",
      cancelAtPeriodEnd: event.subscription.cancelAtPeriodEnd ?? false,
      offerKey: offerKey ?? existing?.offerKey ?? null,
      plan: plan ?? existing?.plan ?? null,
      interval: offer?.interval,
      intervalCount: offer?.intervalCount,
    });
    try {
      this.logger.debug("[BillingService] upserting subscription", {
        plan: subscriptionState.plan,
        eventId: event.eventId,
      });
      await this.store.upsertBillingSubscription(subscriptionState);
    } catch (error) {
      // The partial unique index is the final arbiter under concurrent
      // webhooks. Convert its race loser into the same manual-review path as
      // the preflight check instead of retrying a permanently invalid event.
      const code = (error as { code?: string }).code;
      if (code !== "23505") throw error;
      const concurrent = (await this.store.getUserSubscriptions(uid)).find(
        (candidate) =>
          candidate.provider === event.provider &&
          candidate.providerSubscriptionId !== subscriptionId &&
          blockingStatuses.has(candidate.status ?? ""),
      );
      if (!concurrent) throw error;
      await this.store.recordSubscriptionConflict({
        userId: uid,
        provider: event.provider,
        duplicateSubscriptionId: subscriptionId,
        existingSubscriptionId: concurrent.providerSubscriptionId,
        eventId: event.eventId,
        metadata: event.metadata ?? undefined,
      });
      await this.updateCheckoutIntentFromEvent(event, "completed");
      return { handled: true, action: "subscription_conflict" };
    }
    if (
      this.provisioning &&
      event.subscription.status &&
      ["active", "trialing"].includes(event.subscription.status)
    ) {
      await this.provisionSubscription(uid, offer, event, existing?.plan ?? undefined);
    }
    if (["active", "trialing"].includes(event.subscription.status ?? "")) {
      await this.updateCheckoutIntentFromEvent(event, "completed");
    }
    return { handled: true, action: "subscription_created" };
  }

  private async handleSubscriptionUpdated(event: BillingEvent): Promise<BillingEventResult> {
    this.logger.info("[BillingService] handleSubscriptionUpdated", {
      provider: event.provider,
      eventId: event.eventId,
      subId: event.subscription?.providerSubscriptionId,
    });
    const uid = await this.resolveUserId(event);
    if (!uid) return { handled: false, error: "user_not_found" };
    if (!event.subscription?.providerSubscriptionId)
      return { handled: false, error: "no_subscription_data" };
    const existing = await this.getExistingSubscription(event);
    const { offer, offerKey, plan } = await this.resolveOfferAndKeys(event);
    await this.store.upsertBillingSubscription(
      this.buildSubscriptionState(event, uid, existing, {
        status: event.subscription.status ?? existing?.status ?? "incomplete",
        cancelAtPeriodEnd:
          event.subscription.cancelAtPeriodEnd ?? existing?.cancelAtPeriodEnd ?? false,
        offerKey: offerKey ?? existing?.offerKey ?? null,
        plan: plan ?? existing?.plan ?? null,
        interval: offer?.interval,
        intervalCount: offer?.intervalCount,
      }),
    );
    if (this.provisioning) {
      await this.reEvaluateAccess(uid, event);
    }
    return { handled: true, action: "subscription_updated" };
  }

  private async handleSubscriptionActivated(event: BillingEvent): Promise<BillingEventResult> {
    this.logger.info("[BillingService] handleSubscriptionActivated", {
      provider: event.provider,
      eventId: event.eventId,
    });
    const uid = await this.resolveUserId(event);
    if (!uid) return { handled: false, error: "user_not_found" };
    if (!event.subscription?.providerSubscriptionId)
      return { handled: false, error: "no_subscription_data" };
    const existing = await this.getExistingSubscription(event);
    const { offer, offerKey, plan } = await this.resolveOfferAndKeys(event);
    await this.store.upsertBillingSubscription(
      this.buildSubscriptionState(event, uid, existing, {
        status: "active",
        offerKey: offerKey ?? existing?.offerKey ?? null,
        plan: plan ?? existing?.plan ?? null,
        interval: offer?.interval,
        intervalCount: offer?.intervalCount,
      }),
    );
    if (this.provisioning) {
      await this.provisionSubscription(uid, offer, event, existing?.plan ?? undefined);
    }
    return { handled: true, action: "subscription_activated" };
  }

  private async handleSubscriptionRenewed(event: BillingEvent): Promise<BillingEventResult> {
    const uid = await this.resolveUserId(event);
    if (!uid) return { handled: false, error: "user_not_found" };
    if (!event.subscription?.providerSubscriptionId)
      return { handled: false, error: "no_subscription_data" };
    const existing = await this.getExistingSubscription(event);
    const { offer, offerKey, plan } = await this.resolveOfferAndKeys(event);
    const resolvedPlanKey = plan ?? existing?.plan ?? null;
    await this.store.upsertBillingSubscription(
      this.buildSubscriptionState(event, uid, existing, {
        status: "active",
        offerKey: offerKey ?? existing?.offerKey ?? null,
        plan: resolvedPlanKey,
        interval: offer?.interval,
        intervalCount: offer?.intervalCount,
      }),
    );
    if (this.provisioning && resolvedPlanKey) {
      await this.provisionSubscription(uid, offer, event, existing?.plan ?? undefined);
    }
    return { handled: true, action: "subscription_renewed" };
  }

  private async handleSubscriptionPlanChanged(event: BillingEvent): Promise<BillingEventResult> {
    const uid = await this.resolveUserId(event);
    if (!uid) return { handled: false, error: "user_not_found" };
    if (!event.subscription?.providerSubscriptionId)
      return { handled: false, error: "no_subscription_data" };
    const existing = await this.getExistingSubscription(event);
    const { offer, offerKey, plan } = await this.resolveOfferAndKeys(event);
    await this.store.upsertBillingSubscription(
      this.buildSubscriptionState(event, uid, existing, {
        status: event.subscription.status ?? "active",
        offerKey: offerKey ?? existing?.offerKey ?? null,
        plan: plan ?? existing?.plan ?? null,
        interval: offer?.interval,
        intervalCount: offer?.intervalCount,
      }),
    );
    if (this.provisioning && (plan ?? existing?.plan)) {
      // Plan-change: prefer new plan over existing (renewal at L422 correctly keeps existing).
      await this.provisionSubscription(uid, offer, event, plan ?? existing?.plan ?? undefined);
    }
    return { handled: true, action: "subscription_plan_changed" };
  }

  private async handleCancellationScheduled(event: BillingEvent): Promise<BillingEventResult> {
    const uid = await this.resolveUserId(event);
    if (!uid) return { handled: false, error: "user_not_found" };
    if (!event.subscription?.providerSubscriptionId)
      return { handled: false, error: "no_subscription_data" };
    const existing = await this.getExistingSubscription(event);
    await this.store.upsertBillingSubscription(
      this.buildSubscriptionState(event, uid, existing, {
        status: existing?.status ?? event.subscription.status ?? "active",
        cancelAtPeriodEnd: true,
      }),
    );
    return { handled: true, action: "cancellation_scheduled" };
  }

  private async handleCancellationUnscheduled(event: BillingEvent): Promise<BillingEventResult> {
    const uid = await this.resolveUserId(event);
    if (!uid) return { handled: false, error: "user_not_found" };
    if (!event.subscription?.providerSubscriptionId)
      return { handled: false, error: "no_subscription_data" };
    const existing = await this.getExistingSubscription(event);
    await this.store.upsertBillingSubscription(
      this.buildSubscriptionState(event, uid, existing, {
        status: existing?.status ?? event.subscription.status ?? "active",
        cancelAtPeriodEnd: false,
      }),
    );
    return { handled: true, action: "cancellation_unscheduled" };
  }

  private async handleSubscriptionCanceled(event: BillingEvent): Promise<BillingEventResult> {
    this.logger.info("[BillingService] handleSubscriptionCanceled", {
      provider: event.provider,
      eventId: event.eventId,
    });
    const uid = await this.resolveUserId(event);
    if (!uid) return { handled: false, error: "user_not_found" };
    if (!event.subscription?.providerSubscriptionId)
      return { handled: false, error: "no_subscription_data" };
    const existing = await this.getExistingSubscription(event);
    await this.store.upsertBillingSubscription(
      this.buildSubscriptionState(event, uid, existing, {
        status: "canceled",
        cancelAtPeriodEnd: event.subscription.cancelAtPeriodEnd ?? true,
      }),
    );
    if (this.provisioning) {
      await this.revokeIfCurrentSubscription(uid, event.subscription.providerSubscriptionId);
    }
    return { handled: true, action: "subscription_canceled" };
  }

  private async handleSubscriptionExpired(event: BillingEvent): Promise<BillingEventResult> {
    const uid = await this.resolveUserId(event);
    if (!uid) return { handled: false, error: "user_not_found" };
    if (!event.subscription?.providerSubscriptionId)
      return { handled: false, error: "no_subscription_data" };
    const existing = await this.getExistingSubscription(event);
    await this.store.upsertBillingSubscription(
      this.buildSubscriptionState(event, uid, existing, {
        status: "expired",
        cancelAtPeriodEnd: event.subscription.cancelAtPeriodEnd ?? true,
      }),
    );
    if (this.provisioning) {
      await this.revokeIfCurrentSubscription(uid, event.subscription.providerSubscriptionId);
    }
    return { handled: true, action: "subscription_expired" };
  }

  private async handleSubscriptionPaused(event: BillingEvent): Promise<BillingEventResult> {
    const uid = await this.resolveUserId(event);
    if (!uid) return { handled: false, error: "user_not_found" };
    if (!event.subscription?.providerSubscriptionId)
      return { handled: false, error: "no_subscription_data" };
    const existing = await this.getExistingSubscription(event);
    await this.store.upsertBillingSubscription(
      this.buildSubscriptionState(event, uid, existing, {
        status: "paused",
        cancelAtPeriodEnd: existing?.cancelAtPeriodEnd ?? false,
      }),
    );
    if (this.provisioning) {
      await this.revokeIfCurrentSubscription(uid, event.subscription.providerSubscriptionId);
    }
    return { handled: true, action: "subscription_paused" };
  }

  private async handleSubscriptionResumed(event: BillingEvent): Promise<BillingEventResult> {
    const uid = await this.resolveUserId(event);
    if (!uid) return { handled: false, error: "user_not_found" };
    if (!event.subscription?.providerSubscriptionId)
      return { handled: false, error: "no_subscription_data" };
    const existing = await this.getExistingSubscription(event);
    const { offer, offerKey, plan } = await this.resolveOfferAndKeys(event);
    await this.store.upsertBillingSubscription(
      this.buildSubscriptionState(event, uid, existing, {
        status: "active",
        cancelAtPeriodEnd: false,
        offerKey: offerKey ?? existing?.offerKey ?? null,
        plan: plan ?? existing?.plan ?? null,
      }),
    );
    if (this.provisioning) {
      await this.provisionSubscription(uid, offer, event, existing?.plan ?? undefined);
    }
    return { handled: true, action: "subscription_resumed" };
  }

  private async handleTrialWillEnd(event: BillingEvent): Promise<BillingEventResult> {
    // Resolve userId so routeEvent's blanket fireEventCallback has a useful value.
    await this.resolveUserId(event);
    return { handled: true, action: "trial_will_end_notified" };
  }

  private async handleInvoicePaid(event: BillingEvent): Promise<BillingEventResult> {
    this.logger.info("[BillingService] handleInvoicePaid", {
      provider: event.provider,
      eventId: event.eventId,
      invoiceId: event.invoice?.providerInvoiceId,
    });
    if (event.invoice) {
      const uid = await this.resolveUserId(event);
      if (uid) {
        await this.store.upsertBillingInvoice({
          provider: event.provider,
          providerInvoiceId: event.invoice.providerInvoiceId,
          providerSubscriptionId: event.subscription?.providerSubscriptionId,
          userId: uid,
          status: event.invoice.status,
          amountPaidMinor: event.invoice.amountPaidMinor,
          amountDueMinor: event.invoice.amountDueMinor,
          currency: event.invoice.currency,
          periodStart: event.invoice.periodStart,
          periodEnd: event.invoice.periodEnd,
        });
      }
    }
    if (event.subscription) return this.handleSubscriptionRenewed(event);
    return { handled: true, action: "invoice_paid" };
  }

  private async handlePaymentSucceeded(event: BillingEvent): Promise<BillingEventResult> {
    this.logger.info("[BillingService] handlePaymentSucceeded", {
      provider: event.provider,
      eventId: event.eventId,
      amountMinor: event.payment?.amountMinor,
    });
    if (!event.payment) return { handled: true, action: "payment_succeeded" };

    const uid = await this.resolveUserId(event);
    const refs = event.payment.refs;
    let topupConfig: BillingTopupResult | null = null;
    if (refs) {
      topupConfig = await this.store.resolveCreditTopup(
        event.provider,
        refs.productId ?? null,
        refs.priceId ?? null,
      );
    }

    if (uid) {
      const paymentMetadata: Record<string, unknown> | null =
        topupConfig && event.payment.purpose === "credit_topup"
          ? { credits_per_unit: Number(topupConfig.creditsPerUnit ?? 1000) }
          : null;
      await this.store.upsertBillingPayment({
        provider: event.provider,
        providerPaymentId: event.payment.providerPaymentId,
        userId: uid,
        amountMinor: event.payment.amountMinor,
        taxMinor: event.payment.taxMinor,
        currency: event.payment.currency,
        purpose: event.payment.purpose,
        metadata: paymentMetadata,
      });
    }

    if (topupConfig && this.provisioning && event.payment.purpose === "credit_topup" && uid) {
      if (
        topupConfig.maxAmountMinor != null &&
        event.payment.amountMinor > topupConfig.maxAmountMinor
      ) {
        this.logger.warn(
          `[BillingService] topup amount ${event.payment.amountMinor} exceeds cap ${topupConfig.maxAmountMinor} for topup key ${topupConfig.topupKey} (user ${uid})`,
        );
        return { handled: true, action: "payment_succeeded_out_of_bounds" };
      }
      const credits = await this.store.computeTopupCredits(event.payment.amountMinor, topupConfig);
      if (credits > 0) {
        await this.provisioning.addCredits(uid, new Decimal(credits), {
          type: "purchase",
          bucket: topupConfig.depositTo ?? "purchased",
        });
      }
    }

    await this.updateCheckoutIntentFromEvent(event, "completed");

    return { handled: true, action: "payment_succeeded" };
  }

  private async handlePaymentFailed(event: BillingEvent): Promise<BillingEventResult> {
    this.logger.info("[BillingService] handlePaymentFailed", {
      provider: event.provider,
      eventId: event.eventId,
    });
    const uid = await this.resolveUserId(event);
    if (uid && event.payment) {
      await this.store.upsertBillingPayment({
        provider: event.provider,
        providerPaymentId: event.payment.providerPaymentId,
        userId: uid,
        amountMinor: event.payment.amountMinor,
        currency: event.payment.currency,
        purpose: event.payment.purpose,
      });
    }
    if (uid && event.subscription && this.provisioning) {
      const existing = await this.getExistingSubscription(event);
      await this.store.upsertBillingSubscription(
        this.buildSubscriptionState(event, uid, existing, { status: "past_due" }),
      );
      await this.revokeIfCurrentSubscription(uid, event.subscription.providerSubscriptionId);
    }
    await this.updateCheckoutIntentFromEvent(event, "failed");
    return { handled: true, action: "payment_failed_recorded" };
  }

  private async handleRefundCreated(event: BillingEvent): Promise<BillingEventResult> {
    const uid = await this.resolveUserId(event);
    if (uid && event.refund) {
      await this.store.upsertBillingRefund({
        provider: event.provider,
        providerRefundId: event.refund.providerRefundId,
        providerPaymentId: event.refund.providerPaymentId,
        userId: uid,
        amountMinor: event.refund.amountMinor,
        currency: event.refund.currency,
        reason: event.refund.reason,
      });
      if (event.refund.providerPaymentId && this.provisioning) {
        const payment = await this.store.getBillingPayment(
          event.provider,
          event.refund.providerPaymentId,
        );

        if (payment?.purpose === "credit_topup") {
          const payMeta = (payment.metadata ?? {}) as Record<string, unknown>;
          const rawCpu = payMeta.credits_per_unit as string | number | null | undefined;
          const cpu = Number(rawCpu);
          if (!Number.isFinite(cpu) || cpu <= 0) {
            this.logger.warn(
              `[BillingService] cannot claw back credits for refund ${event.refund.providerRefundId}: no valid creditsPerUnit in payment metadata`,
            );
            return { handled: true, action: "refund_recorded_no_clawback" };
          }
          const credits = Math.trunc((event.refund.amountMinor * cpu) / 100);
          if (credits > 0) {
            await this.provisioning.deductCredits(uid, credits, {
              txType: "refund",
              bucket: "purchased",
            });
          }
        }
      }
    }
    return { handled: true, action: "refund_recorded" };
  }

  private async handleDisputeCreated(event: BillingEvent): Promise<BillingEventResult> {
    const uid = await this.resolveUserId(event);
    if (uid && event.dispute) {
      await this.store.upsertBillingDispute({
        provider: event.provider,
        providerDisputeId: event.dispute.providerDisputeId,
        providerPaymentId: event.dispute.providerPaymentId,
        userId: uid,
        status: "needs_response",
        reason: event.dispute.reason,
      });
    }
    return { handled: true, action: "dispute_recorded" };
  }

  private async handleDisputeClosed(event: BillingEvent): Promise<BillingEventResult> {
    const uid = await this.resolveUserId(event);
    if (uid && event.dispute) {
      await this.store.upsertBillingDispute({
        provider: event.provider,
        providerDisputeId: event.dispute.providerDisputeId,
        providerPaymentId: event.dispute.providerPaymentId,
        userId: uid,
        status: "closed",
        reason: event.dispute.reason,
      });
    }
    return { handled: true, action: "dispute_closed" };
  }

  private async provisionSubscription(
    uid: string,
    offer: BillingOfferResult | null,
    event: BillingEvent,
    planKeyOverride?: string,
  ): Promise<void> {
    if (!this.provisioning) {
      this.logger.debug(
        `[BillingService] provisionSubscription: no provisioning capability for user ${uid}`,
      );
      return;
    }
    const plan = planKeyOverride ?? offer?.plan;
    if (!plan) {
      this.logger.debug("[BillingService] provisionSubscription skipped (no plan)", { uid });
      return;
    }
    this.logger.debug("[BillingService] provisionSubscription setting plan", { uid, plan });
    const periodStart = event.subscription?.periodStart;
    const planAssignedAt = periodStart
      ? (() => {
          const d = new Date(periodStart);
          return isNaN(d.getTime()) ? undefined : d;
        })()
      : undefined;
    await this.provisioning.setUserPlan(uid, plan, planAssignedAt);

    // Cancel subscriptions from other providers (migration support)
    if (this.cancelPriorProviders && event.provider) {
      const result = await this.store.deactivateOtherProviderSubscriptions(uid, event.provider);
      if (result.deactivatedCount > 0) {
        this.logger.debug(
          `[BillingService] deactivated ${result.deactivatedCount} prior provider subscription(s) for user ${uid}`,
        );
      }
    }

    const g = offer?.grant;
    if (g?.mode === "cycle_grant" && this.provisioning) {
      const cycleCredits = g.credits;
      if (cycleCredits && cycleCredits > 0) {
        const cycleBucket = g.bucket ?? "purchased";
        if (g.replacePrior) {
          await this.provisioning.revokeCreditsByTxType(uid, "cycle_grant");
        }
        await this.provisioning.addCredits(uid, new Decimal(cycleCredits), {
          type: "cycle_grant",
          bucket: cycleBucket,
        });
      }
    }
  }

  private async revokeSubscription(uid: string): Promise<void> {
    if (!this.provisioning) return;
    await this.provisioning.unsetUserPlan(uid);
  }

  /**
   * Do not revoke access because a stale subscription record ended while a
   * newer subscription for the same user is still active.
   */
  private async revokeIfCurrentSubscription(uid: string, subscriptionId: string): Promise<void> {
    const current = await this.store.getUserSubscription(uid, ["active", "trialing"]);
    if (!current || current.providerSubscriptionId === subscriptionId) {
      await this.revokeSubscription(uid);
    }
  }

  private async reEvaluateAccess(uid: string, event: BillingEvent): Promise<void> {
    if (!this.provisioning || !event.subscription) return;
    const status = event.subscription.status;
    if (status && ["active", "trialing"].includes(status)) {
      const offer = await this.resolveOfferFromEvent(event);
      if (offer) {
        await this.provisionSubscription(uid, offer, event);
      } else {
        const existing = await this.store.getBillingSubscription(
          event.provider,
          event.subscription.providerSubscriptionId,
        );
        if (existing?.plan) {
          await this.provisionSubscription(uid, null, event, existing.plan);
        }
      }
    } else if (
      status &&
      ["canceled", "expired", "unpaid", "paused", "incomplete_expired"].includes(status)
    ) {
      await this.revokeIfCurrentSubscription(uid, event.subscription.providerSubscriptionId);
    }
  }

  private async resolveOfferFromEvent(event: BillingEvent): Promise<BillingOfferResult | null> {
    const refs = event.subscription?.refs;
    if (!refs) return null;
    return this.resolveBillingOfferCached(
      event.provider,
      refs.productId ?? null,
      refs.priceId ?? null,
    );
  }
}
