import { LRUCache } from "lru-cache";
import { z } from "zod";
import type { NormalizedLogger } from "../shared/logger.js";
import { normalizeLogger } from "../shared/logger.js";
import type { BillingStore } from "./billing-store.js";
import type {
  BillingEvent,
  BillingEventResult,
  BillingOfferResult,
  BillingSubscriptionState,
  BillingSubscriptionStatus,
} from "./types/index.js";
import { BillingEventType } from "./types/index.js";
import { BillingFinancialEventHandlers } from "./financial-event-handlers.js";
import { StoreError } from "../errors.js";
import type { BillingProvisioningPort, BillingServiceOptions } from "./service-types.js";
import type { JsonObject } from "../shared/json.js";

interface OfferCacheValue {
  offer: BillingOfferResult | null;
}

interface OfferContext {
  provider: string;
  productId: string | null;
  priceId: string | null;
}

export class BillingEventHandlers {
  private readonly provisioning: BillingProvisioningPort | null;
  private readonly autoSelectEntitlementSource: boolean;
  private readonly pastDueGracePeriodMs: number;
  private readonly terminalPlanKey: string | null;
  private readonly logger: NormalizedLogger;
  private readonly offerCache: LRUCache<string, OfferCacheValue, OfferContext>;
  private readonly financial: BillingFinancialEventHandlers;
  private readonly handlerMap: Record<string, (event: BillingEvent) => Promise<BillingEventResult>>;

  get hasProvisioning(): boolean {
    return this.provisioning !== null;
  }

  constructor(
    private readonly store: BillingStore,
    options?: BillingServiceOptions,
  ) {
    this.provisioning = options?.provisioning ?? null;
    this.autoSelectEntitlementSource = options?.autoSelectEntitlementSource ?? true;
    this.pastDueGracePeriodMs = options?.pastDueGracePeriodMs ?? 7 * 24 * 60 * 60 * 1000;
    if (!Number.isFinite(this.pastDueGracePeriodMs) || this.pastDueGracePeriodMs < 0) {
      throw new RangeError("pastDueGracePeriodMs must be a finite non-negative number");
    }
    this.terminalPlanKey = options?.terminalPlanKey ?? null;
    this.logger = normalizeLogger(options?.logger);
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
    this.financial = new BillingFinancialEventHandlers(
      store,
      this.logger,
      this.pastDueGracePeriodMs,
      (event) => this.resolveAccountId(event),
      (event) => this.handleSubscriptionRenewed(event),
      (event, status) => this.updateCheckoutIntentFromEvent(event, status),
      (event) => this.getExistingSubscription(event),
      (event, userId, existing, overrides) =>
        this.buildSubscriptionState(event, userId, existing, overrides),
    );
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
      [BillingEventType.INVOICE_PAID]: this.financial.handleInvoicePaid.bind(this.financial),
      [BillingEventType.PAYMENT_SUCCEEDED]: this.financial.handlePaymentSucceeded.bind(
        this.financial,
      ),
      [BillingEventType.PAYMENT_FAILED]: this.financial.handlePaymentFailed.bind(this.financial),
      [BillingEventType.REFUND_CREATED]: this.financial.handleRefundCreated.bind(this.financial),
      [BillingEventType.REFUND_UPDATED]: this.financial.handleRefundCreated.bind(this.financial),
      [BillingEventType.REFUND_FAILED]: this.financial.handleRefundCreated.bind(this.financial),
      [BillingEventType.DISPUTE_CREATED]: this.financial.handleDisputeCreated.bind(this.financial),
      [BillingEventType.DISPUTE_CLOSED]: this.financial.handleDisputeClosed.bind(this.financial),
    };
  }

  getHandler(
    eventType: BillingEventType,
  ): ((event: BillingEvent) => Promise<BillingEventResult>) | undefined {
    return this.handlerMap[eventType];
  }

  invalidateOfferCache(): void {
    this.offerCache.clear();
  }

  async updateCheckoutIntentFromEvent(
    event: BillingEvent,
    status: "completed" | "failed" | "expired",
  ): Promise<void> {
    const intentId = z.string().safeParse(event.metadata?.checkout_intent_id).data;
    if (!intentId) return;
    await this.store.updateCheckoutIntent(intentId, { status });
  }

  /**
   * Resolve accountId from the event, mutating event.accountId so that
   * routeEvent's blanket fireEventCallback can read it. Each ingestBillingEvent
   * call creates a fresh event object, so mutation is safe.
   */
  private async resolveAccountId(event: BillingEvent): Promise<string | null> {
    if (event.accountId) return event.accountId;
    if (event.customer?.providerCustomerId) {
      const uid = await this.store.getBillingCustomer(
        event.provider,
        event.customer.providerCustomerId,
      );
      if (uid) {
        event.accountId = uid;
        return uid;
      }
    }
    if (event.subscription?.providerSubscriptionId) {
      const existing = await this.store.getBillingSubscription(
        event.provider,
        event.subscription.providerSubscriptionId,
      );
      if (existing?.userId) {
        event.accountId = existing.userId;
        return existing.userId;
      }
    }
    const providerPaymentId =
      event.payment?.providerPaymentId ??
      event.refund?.providerPaymentId ??
      event.dispute?.providerPaymentId ??
      null;
    if (providerPaymentId) {
      const payment = await this.store.getBillingPayment(event.provider, providerPaymentId);
      if (payment?.userId) {
        event.accountId = payment.userId;
        return payment.userId;
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
      const uid = await this.resolveAccountId(event);
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
      const uid = await this.resolveAccountId(event);
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
      hasAccountId: Boolean(event.accountId),
    });
    if (event.customer?.providerCustomerId) {
      const uid = await this.resolveAccountId(event);
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
      offerId?: string | null;
      plan?: string | null;
      interval?: string | null;
      intervalCount?: number | null;
      metadata?: JsonObject | null;
      graceEndsAt?: string | null;
      graceExpiredAt?: string | null;
    },
  ): BillingSubscriptionState {
    if (!event.subscription) {
      throw new TypeError("billing subscription event requires subscription data");
    }
    const sub = event.subscription;
    const status = overrides?.status ?? sub.status ?? existing?.status ?? "incomplete";
    return {
      userId,
      provider: event.provider,
      providerSubscriptionId: sub.providerSubscriptionId,
      providerCustomerId:
        event.customer?.providerCustomerId ?? existing?.providerCustomerId ?? null,
      offerId: overrides?.offerId ?? existing?.offerId ?? null,
      offerKey: overrides?.offerKey ?? existing?.offerKey ?? null,
      plan: overrides?.plan ?? existing?.plan ?? null,
      status,
      currentPeriodStart: sub.periodStart ?? existing?.currentPeriodStart ?? null,
      currentPeriodEnd: sub.periodEnd ?? existing?.currentPeriodEnd ?? null,
      trialEnd: sub.trialEnd ?? existing?.trialEnd ?? null,
      cancelAt: sub.cancelAt ?? existing?.cancelAt ?? null,
      endedAt: sub.endedAt ?? existing?.endedAt ?? null,
      graceEndsAt:
        status === "past_due" ? (overrides?.graceEndsAt ?? existing?.graceEndsAt ?? null) : null,
      graceExpiredAt:
        status === "past_due"
          ? (overrides?.graceExpiredAt ?? existing?.graceExpiredAt ?? null)
          : null,
      providerUpdatedAt: event.occurredAt,
      cancelAtPeriodEnd:
        overrides?.cancelAtPeriodEnd ??
        sub.cancelAtPeriodEnd ??
        existing?.cancelAtPeriodEnd ??
        false,
      interval:
        overrides?.interval ??
        sub.interval ??
        z.string().safeParse(event.metadata?.billing_interval).data ??
        existing?.interval ??
        null,
      intervalCount:
        overrides?.intervalCount ?? sub.intervalCount ?? existing?.intervalCount ?? null,
      metadata:
        overrides?.metadata ??
        (event.metadata || existing?.metadata
          ? { ...(existing?.metadata ?? {}), ...(event.metadata ?? {}) }
          : null),
    };
  }

  private async resolveOfferAndKeys(event: BillingEvent): Promise<{
    offer: BillingOfferResult | null;
    offerId: string | null;
    offerKey: string | null;
    plan: string | null;
  }> {
    const refs = event.subscription?.refs;
    if (!refs) return { offer: null, offerId: null, offerKey: null, plan: null };

    // Tier 1: Resolve by price/product ID
    const offer = await this.resolveBillingOfferCached(
      event.provider,
      refs.productId ?? null,
      refs.priceId ?? null,
    );
    if (offer) {
      return {
        offer,
        offerId: offer.offerId,
        offerKey: offer.offerKey ?? null,
        plan: offer.plan ?? null,
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
          offerId: lookupOffer.offerId,
          offerKey: lookupOffer.offerKey ?? null,
          plan: lookupOffer.plan ?? null,
        };
      }

      this.logger.error(
        `[BillingService] resolveOfferAndKeys: no offer found for ${event.provider}/${refs.lookupKey}`,
      );
    }

    return { offer: null, offerId: null, offerKey: null, plan: null };
  }

  private async handleSubscriptionCreated(event: BillingEvent): Promise<BillingEventResult> {
    const uid = await this.resolveAccountId(event);
    this.logger.info("[BillingService] handleSubscriptionCreated", {
      eventId: event.eventId,
      provider: event.provider,
      hasAccountId: Boolean(uid),
    });
    if (!uid) return { handled: false, error: "account_not_found" };
    if (event.customer?.providerCustomerId) {
      await this.store.upsertBillingCustomer(
        event.provider,
        event.customer.providerCustomerId,
        uid,
        event.customer.email ?? null,
      );
    }
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
        metadata: event.metadata ?? {},
      });
      return { handled: false, error: "subscription_conflict" };
    }
    const { offer, offerId, offerKey, plan } = await this.resolveOfferAndKeys(event);
    this.logger.debug("[BillingService] resolveOfferAndKeys", {
      offerKey,
      plan,
      eventId: event.eventId,
    });
    const subscriptionState = this.buildSubscriptionState(event, uid, existing, {
      status: event.subscription.status ?? "incomplete",
      cancelAtPeriodEnd: event.subscription.cancelAtPeriodEnd ?? false,
      offerKey: offerKey ?? existing?.offerKey ?? null,
      offerId: offerId ?? existing?.offerId ?? null,
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
      const code = z.object({ code: z.string().optional() }).safeParse(error).data?.code;
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
        metadata: event.metadata ?? {},
      });
      return { handled: false, error: "subscription_conflict" };
    }
    if (
      this.provisioning &&
      event.subscription.status &&
      ["active", "trialing"].includes(event.subscription.status)
    ) {
      await this.provisionSubscription(uid, offer, event);
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
    const uid = await this.resolveAccountId(event);
    if (!uid) return { handled: false, error: "account_not_found" };
    if (!event.subscription?.providerSubscriptionId)
      return { handled: false, error: "no_subscription_data" };
    const existing = await this.getExistingSubscription(event);
    const { offer, offerId, offerKey, plan } = await this.resolveOfferAndKeys(event);
    await this.store.upsertBillingSubscription(
      this.buildSubscriptionState(event, uid, existing, {
        status: event.subscription.status ?? existing?.status ?? "incomplete",
        cancelAtPeriodEnd:
          event.subscription.cancelAtPeriodEnd ?? existing?.cancelAtPeriodEnd ?? false,
        offerKey: offerKey ?? existing?.offerKey ?? null,
        offerId: offerId ?? existing?.offerId ?? null,
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
    const uid = await this.resolveAccountId(event);
    if (!uid) return { handled: false, error: "account_not_found" };
    if (!event.subscription?.providerSubscriptionId)
      return { handled: false, error: "no_subscription_data" };
    const existing = await this.getExistingSubscription(event);
    const { offer, offerId, offerKey, plan } = await this.resolveOfferAndKeys(event);
    await this.store.upsertBillingSubscription(
      this.buildSubscriptionState(event, uid, existing, {
        status: "active",
        offerKey: offerKey ?? existing?.offerKey ?? null,
        offerId: offerId ?? existing?.offerId ?? null,
        plan: plan ?? existing?.plan ?? null,
        interval: offer?.interval,
        intervalCount: offer?.intervalCount,
      }),
    );
    if (this.provisioning) {
      await this.provisionSubscription(uid, offer, event);
    }
    return { handled: true, action: "subscription_activated" };
  }

  private async handleSubscriptionRenewed(event: BillingEvent): Promise<BillingEventResult> {
    const uid = await this.resolveAccountId(event);
    if (!uid) return { handled: false, error: "account_not_found" };
    if (!event.subscription?.providerSubscriptionId)
      return { handled: false, error: "no_subscription_data" };
    const existing = await this.getExistingSubscription(event);
    const { offer, offerId, offerKey, plan } = await this.resolveOfferAndKeys(event);
    const resolvedPlanKey = plan ?? existing?.plan ?? null;
    await this.store.upsertBillingSubscription(
      this.buildSubscriptionState(event, uid, existing, {
        status: "active",
        offerKey: offerKey ?? existing?.offerKey ?? null,
        offerId: offerId ?? existing?.offerId ?? null,
        plan: resolvedPlanKey,
        interval: offer?.interval,
        intervalCount: offer?.intervalCount,
      }),
    );
    if (this.provisioning && resolvedPlanKey) {
      await this.provisionSubscription(uid, offer, event);
    }
    await this.grantSubscriptionCycle(event, offer);
    return { handled: true, action: "subscription_renewed" };
  }

  private async handleSubscriptionPlanChanged(event: BillingEvent): Promise<BillingEventResult> {
    const uid = await this.resolveAccountId(event);
    if (!uid) return { handled: false, error: "account_not_found" };
    if (!event.subscription?.providerSubscriptionId)
      return { handled: false, error: "no_subscription_data" };
    const existing = await this.getExistingSubscription(event);
    const { offer, offerId, offerKey, plan } = await this.resolveOfferAndKeys(event);
    // Capture the current allowance anchor before advancing the durable
    // change. The Postgres transition updates the assignment atomically, so
    // reading it afterwards would return the new assignment's timestamp and
    // silently reset the learner's current allowance window.
    const preservedAllowanceAnchor = this.provisioning
      ? ((await this.provisioning.getUserPlan(uid))?.planAssignedAt ?? null)
      : undefined;
    const pending = await this.store.getOpenBillingSubscriptionChange(
      event.provider,
      event.subscription.providerSubscriptionId,
    );
    if (pending) {
      await this.store.updateBillingSubscriptionChange(pending.id, {
        state: "applied",
      });
    }
    await this.store.upsertBillingSubscription(
      this.buildSubscriptionState(event, uid, existing, {
        status: event.subscription.status ?? "active",
        offerKey: offerKey ?? existing?.offerKey ?? null,
        offerId: offerId ?? existing?.offerId ?? null,
        plan: plan ?? existing?.plan ?? null,
        interval: offer?.interval,
        intervalCount: offer?.intervalCount,
        metadata: {
          ...(existing?.metadata ?? {}),
          ...(event.metadata ?? {}),
          pendingPlanChange: null,
        },
      }),
    );
    if (this.provisioning && (plan ?? existing?.plan)) {
      // Plan-change: prefer new plan over existing (renewal at L422 correctly keeps existing).
      await this.provisionSubscription(
        uid,
        offer,
        event,
        plan ?? existing?.plan ?? undefined,
        true,
        preservedAllowanceAnchor,
      );
    }
    return { handled: true, action: "subscription_plan_changed" };
  }

  private async handleCancellationScheduled(event: BillingEvent): Promise<BillingEventResult> {
    const uid = await this.resolveAccountId(event);
    if (!uid) return { handled: false, error: "account_not_found" };
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
    const uid = await this.resolveAccountId(event);
    if (!uid) return { handled: false, error: "account_not_found" };
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
    const uid = await this.resolveAccountId(event);
    if (!uid) return { handled: false, error: "account_not_found" };
    if (!event.subscription?.providerSubscriptionId)
      return { handled: false, error: "no_subscription_data" };
    const existing = await this.getExistingSubscription(event);
    const resolved = existing ? null : await this.resolveOfferAndKeys(event);
    if (!existing && !resolved?.offerId) {
      throw new StoreError(
        `cannot persist cancellation for unknown subscription ${event.provider}/${event.subscription.providerSubscriptionId}: offer could not be resolved`,
        {
          retryable: true,
          details: {
            provider: event.provider,
            providerSubscriptionId: event.subscription.providerSubscriptionId,
          },
        },
      );
    }
    await this.store.upsertBillingSubscription(
      this.buildSubscriptionState(event, uid, existing, {
        status: "canceled",
        cancelAtPeriodEnd: event.subscription.cancelAtPeriodEnd ?? true,
        offerKey: resolved?.offerKey ?? existing?.offerKey ?? null,
        offerId: resolved?.offerId ?? existing?.offerId ?? null,
        plan: resolved?.plan ?? existing?.plan ?? null,
        interval: resolved?.offer?.interval,
        intervalCount: resolved?.offer?.intervalCount,
      }),
    );
    if (this.provisioning) {
      await this.revokeIfCurrentSubscription(uid, event.subscription.providerSubscriptionId);
    }
    return { handled: true, action: "subscription_canceled" };
  }

  private async handleSubscriptionExpired(event: BillingEvent): Promise<BillingEventResult> {
    const uid = await this.resolveAccountId(event);
    if (!uid) return { handled: false, error: "account_not_found" };
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
    const uid = await this.resolveAccountId(event);
    if (!uid) return { handled: false, error: "account_not_found" };
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
    const uid = await this.resolveAccountId(event);
    if (!uid) return { handled: false, error: "account_not_found" };
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
      await this.provisionSubscription(uid, offer, event);
    }
    return { handled: true, action: "subscription_resumed" };
  }

  private async handleTrialWillEnd(event: BillingEvent): Promise<BillingEventResult> {
    // Resolve userId so routeEvent's blanket fireEventCallback has a useful value.
    await this.resolveAccountId(event);
    return { handled: true, action: "trial_will_end_notified" };
  }

  private async provisionSubscription(
    uid: string,
    offer: BillingOfferResult | null,
    event: BillingEvent,
    planKeyOverride?: string,
    preserveAllowanceAnchor = false,
    preservedAllowanceAnchor?: Date | string | null,
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
    let periodStart: Date | string | null | undefined;
    if (preserveAllowanceAnchor) {
      const existingAnchor =
        preservedAllowanceAnchor !== undefined
          ? preservedAllowanceAnchor
          : (await this.provisioning.getUserPlan(uid))?.planAssignedAt;
      if (existingAnchor) {
        const anchor = new Date(existingAnchor);
        // A provider's subscription period start may actually be its next
        // renewal date. Never let a future anchor hide an already-active
        // entitlement after a plan change.
        if (!Number.isNaN(anchor.getTime()) && anchor.getTime() <= Date.now()) {
          periodStart = anchor;
        }
      }
    } else {
      periodStart = event.subscription?.periodStart;
    }
    const planAssignedAt = periodStart
      ? (() => {
          const d = new Date(periodStart);
          return Number.isNaN(d.getTime()) ? undefined : d;
        })()
      : preserveAllowanceAnchor
        ? new Date()
        : undefined;
    await this.provisioning.setUserPlan(uid, plan, planAssignedAt);

    if (this.autoSelectEntitlementSource && event.provider) {
      const selected = await this.store.selectSubscriptionEntitlementSource(
        uid,
        event.provider,
        event.subscription?.providerSubscriptionId,
      );
      if (selected) {
        this.logger.debug(
          `[BillingService] selected ${event.provider} subscription as entitlement source for user ${uid}`,
        );
      }
    }
  }

  private async grantSubscriptionCycle(
    event: BillingEvent,
    offer: BillingOfferResult | null,
  ): Promise<void> {
    const credits = offer?.grant?.mode === "cycle_grant" ? offer.grant.credits : null;
    if (!credits || credits.lte(0) || !event.billingEventId || !event.subscription) return;

    const subscription = await this.store.getBillingSubscription(
      event.provider,
      event.subscription.providerSubscriptionId,
    );
    if (!subscription?.subscriptionId) {
      throw new StoreError("subscription cycle grant requires a persisted subscription", {
        indeterminate: true,
        details: {
          provider: event.provider,
          providerSubscriptionId: event.subscription.providerSubscriptionId,
        },
      });
    }
    const payment =
      event.payment?.providerPaymentId != null
        ? await this.store.getBillingPayment(event.provider, event.payment.providerPaymentId)
        : null;
    const grantId = await this.store.createBillingCreditGrant({
      paymentId: payment?.id == null ? null : String(payment.id),
      subscriptionId: subscription.subscriptionId,
      configuredCredits: credits,
      quantity: 1,
      billingEventId: event.billingEventId,
    });
    await this.store.grantBillingCredit(grantId, `billing:${event.eventId}:subscription-cycle`);
  }

  private async revokeSubscription(uid: string): Promise<void> {
    if (!this.provisioning) return;
    if (this.terminalPlanKey) {
      await this.provisioning.setUserPlan(uid, this.terminalPlanKey);
      return;
    }
    await this.provisioning.unsetUserPlan(uid);
  }

  /**
   * Do not revoke access because a stale subscription record ended while a
   * newer subscription for the same user is still active.
   */
  async revokeIfCurrentSubscription(uid: string, subscriptionId: string): Promise<void> {
    const current = await this.store.getUserSubscription(uid, ["active", "trialing", "past_due"]);
    if (!current || current.providerSubscriptionId === subscriptionId) {
      await this.revokeSubscription(uid);
    }
  }

  private async reEvaluateAccess(uid: string, event: BillingEvent): Promise<void> {
    if (!this.provisioning || !event.subscription) return;
    const status = event.subscription.status;
    // Provider retries (Stripe past_due, Dodo on_hold mapped to past_due)
    // are a grace period. Keep the last paid entitlements until the provider
    // reaches a terminal state instead of revoking access on the first miss.
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
    } else if (status === "past_due") {
      const existing = await this.store.getBillingSubscription(
        event.provider,
        event.subscription.providerSubscriptionId,
      );
      const graceHasExpired =
        Boolean(existing?.graceExpiredAt) ||
        (Boolean(existing?.graceEndsAt) &&
          new Date(existing!.graceEndsAt!).getTime() <= Date.now());
      if (graceHasExpired) {
        await this.revokeIfCurrentSubscription(uid, event.subscription.providerSubscriptionId);
      } else {
        const offer = await this.resolveOfferFromEvent(event);
        if (offer) {
          await this.provisionSubscription(uid, offer, event);
        } else if (existing?.plan) {
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
