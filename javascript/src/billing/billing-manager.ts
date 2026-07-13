import Decimal from "decimal.js";
import { LRUCache } from "lru-cache";
import type { CreditManager } from "../manager.js";
import type { BillingStore } from "./billing-store.js";
import type {
  BillingConfig,
  BillingEvent,
  BillingEventResult,
  BillingOfferResult,
  BillingSubscriptionState,
  BillingSubscriptionStatus,
  BillingTopupResult,
} from "./billing-types.js";
import type { ProviderLogger } from "../providers/types.js";
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

export interface BillingManagerOptions {
  creditManager?: CreditManager | null;
  resolveUser?: ResolveUserFn | null;
  onTrialWillEnd?: (event: BillingEvent) => void | Promise<void>;
  cancelPriorProviders?: boolean;
  logger?: ProviderLogger | null;
}

/**
 * Provider-agnostic billing lifecycle state machine.
 * Mirrors Python bursar/billing/manager.py.
 */
export class BillingManager {
  private store: BillingStore;
  private cm: CreditManager | null;
  private resolveUser: ResolveUserFn | null;
  private onTrialWillEnd: ((event: BillingEvent) => void | Promise<void>) | null;
  private cancelPriorProviders: boolean;
  private logger: ProviderLogger | null;
  private handlerMap: Record<string, (event: BillingEvent) => Promise<BillingEventResult>>;
  private offerCache: LRUCache<string, OfferCacheValue, OfferContext>;
  private readonly IGNORED_EVENT_TYPES = new Set(["checkout.expired", "invoice.upcoming"]);
  private billingConfigSynced = false;

  constructor(store: BillingStore, options?: BillingManagerOptions) {
    this.store = store;
    this.cm = options?.creditManager ?? null;
    this.resolveUser = options?.resolveUser ?? null;
    this.onTrialWillEnd = options?.onTrialWillEnd ?? null;
    this.cancelPriorProviders = options?.cancelPriorProviders ?? true;
    this.logger = options?.logger ?? null;
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
      "customer.created": this.handleCustomerCreated.bind(this),
      "customer.updated": this.handleCustomerCreated.bind(this),
      "customer.deleted": this.handleCustomerDeleted.bind(this),
      "checkout.completed": this.handleCheckoutCompleted.bind(this),
      "subscription.created": this.handleSubscriptionCreated.bind(this),
      "subscription.updated": this.handleSubscriptionUpdated.bind(this),
      "subscription.activated": this.handleSubscriptionActivated.bind(this),
      "subscription.renewed": this.handleSubscriptionRenewed.bind(this),
      "subscription.plan_changed": this.handleSubscriptionPlanChanged.bind(this),
      "subscription.cancellation_scheduled": this.handleCancellationScheduled.bind(this),
      "subscription.cancellation_unscheduled": this.handleCancellationUnscheduled.bind(this),
      "subscription.canceled": this.handleSubscriptionCanceled.bind(this),
      "subscription.expired": this.handleSubscriptionExpired.bind(this),
      "subscription.paused": this.handleSubscriptionPaused.bind(this),
      "subscription.resumed": this.handleSubscriptionResumed.bind(this),
      "subscription.trial_will_end": this.handleTrialWillEnd.bind(this),
      "invoice.paid": this.handleInvoicePaid.bind(this),
      "payment.succeeded": this.handlePaymentSucceeded.bind(this),
      "payment.failed": this.handlePaymentFailed.bind(this),
      "refund.created": this.handleRefundCreated.bind(this),
      "dispute.created": this.handleDisputeCreated.bind(this),
      "dispute.closed": this.handleDisputeClosed.bind(this),
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

  /**
   * Sync billing configuration (offers, topups, provider refs) from a config object.
   * Delegates to the store's sync method. Idempotent — safe to call on every
   * BillingManager initialization.
   */
  async syncBillingFromConfig(config: BillingConfig): Promise<void> {
    await this.store.syncBillingFromConfig(config);
  }

  private async ensureBillingConfigSynced(): Promise<void> {
    if (this.billingConfigSynced) return;
    try {
      const config = await this.store.getActivePricingConfig();
      if (config?.billing) {
        await this.syncBillingFromConfig(config.billing as Record<string, unknown>);
      }
    } catch (err) {
      this.logger?.warn?.(
        `[BillingManager] failed to sync billing config from active pricing: ${err instanceof Error ? err.message : String(err)}`,
      );
    }
    this.billingConfigSynced = true;
  }

  /** Invalidate the offer cache so the next resolution call re-fetches fresh data. */
  invalidateOfferCache(): void {
    this.offerCache.clear();
  }

  async handleEvent(event: BillingEvent): Promise<BillingEventResult> {
    const claim = await this.store.claimBillingEvent(
      event.provider,
      event.eventId,
      event.eventType,
    );

    if (claim.status === "duplicate") {
      return { handled: true, action: "duplicate" };
    }
    if (claim.status === "retry") {
      return { handled: false, error: "claim_failed_retry" };
    }

    try {
      const result = await this.routeEvent(event);
      await this.store.completeBillingEvent(event.provider, event.eventId);
      return result;
    } catch (err) {
      this.logger?.error?.(
        `[BillingManager] failed to handle billing event ${event.provider}/${event.eventId}`,
        { error: err instanceof Error ? err.message : String(err) },
      );
      await this.store.failBillingEvent(event.provider, event.eventId);
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
        return { handled: true, action: "ignored" };
      }
      return { handled: false, error: "unhandled_event_type" };
    }
    return handler(event);
  }

  private async resolveUserId(event: BillingEvent): Promise<string | null> {
    if (event.userId) return event.userId;
    if (event.customer?.providerCustomerId) {
      const uid = await this.store.getBillingCustomer(
        event.provider,
        event.customer.providerCustomerId,
      );
      if (uid) return uid;
    }
    if (this.resolveUser && event.customer?.providerCustomerId) {
      return this.resolveUser(
        event.provider,
        event.customer.providerCustomerId,
        event.customer.email ?? null,
      );
    }
    if (event.subscription?.providerSubscriptionId) {
      const existing = await this.store.getBillingSubscription(
        event.provider,
        event.subscription.providerSubscriptionId,
      );
      if (existing?.userId) return existing.userId;
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
    if (event.customer?.providerCustomerId) {
      const uid = await this.resolveUserId(event);
      if (uid && this.cm) {
        await this.revokeSubscription(uid);
      }
    }
    return { handled: true, action: "customer_deleted" };
  }

  private async handleCheckoutCompleted(event: BillingEvent): Promise<BillingEventResult> {
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
      interval: sub.interval ?? existing?.interval ?? null,
      intervalCount: sub.intervalCount ?? existing?.intervalCount ?? null,
      metadata: event.metadata ?? existing?.metadata ?? null,
    };
  }

  private async resolveOfferAndKeys(event: BillingEvent): Promise<{
    offer: BillingOfferResult | null;
    offerKey: string | null;
    plan: string | null;
  }> {
    await this.ensureBillingConfigSynced();
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

      this.logger?.error?.(
        `[BillingManager] resolveOfferAndKeys: no offer found for ${event.provider}/${refs.lookupKey}`,
      );
    }

    return { offer: null, offerKey: null, plan: null };
  }

  private async handleSubscriptionCreated(event: BillingEvent): Promise<BillingEventResult> {
    const uid = await this.resolveUserId(event);
    if (!uid) return { handled: false, error: "user_not_found" };
    if (!event.subscription?.providerSubscriptionId)
      return { handled: false, error: "no_subscription_data" };
    const existing = await this.getExistingSubscription(event);
    const { offer, offerKey, plan } = await this.resolveOfferAndKeys(event);
    await this.store.upsertBillingSubscription(
      this.buildSubscriptionState(event, uid, existing, {
        status: event.subscription.status ?? "incomplete",
        cancelAtPeriodEnd: event.subscription.cancelAtPeriodEnd ?? false,
        offerKey: offerKey ?? existing?.offerKey ?? null,
        plan: plan ?? existing?.plan ?? null,
      }),
    );
    if (
      this.cm &&
      event.subscription.status &&
      ["active", "trialing"].includes(event.subscription.status)
    ) {
      await this.provisionSubscription(uid, offer, event, existing?.plan ?? undefined);
    }
    return { handled: true, action: "subscription_created" };
  }

  private async handleSubscriptionUpdated(event: BillingEvent): Promise<BillingEventResult> {
    const uid = await this.resolveUserId(event);
    if (!uid) return { handled: false, error: "user_not_found" };
    if (!event.subscription?.providerSubscriptionId)
      return { handled: false, error: "no_subscription_data" };
    const existing = await this.getExistingSubscription(event);
    const { offerKey, plan } = await this.resolveOfferAndKeys(event);
    await this.store.upsertBillingSubscription(
      this.buildSubscriptionState(event, uid, existing, {
        status: event.subscription.status ?? existing?.status ?? "incomplete",
        cancelAtPeriodEnd:
          event.subscription.cancelAtPeriodEnd ?? existing?.cancelAtPeriodEnd ?? false,
        offerKey: offerKey ?? existing?.offerKey ?? null,
        plan: plan ?? existing?.plan ?? null,
      }),
    );
    if (this.cm) {
      await this.reEvaluateAccess(uid, event);
    }
    return { handled: true, action: "subscription_updated" };
  }

  private async handleSubscriptionActivated(event: BillingEvent): Promise<BillingEventResult> {
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
      }),
    );
    if (this.cm) {
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
      }),
    );
    if (this.cm && resolvedPlanKey) {
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
      }),
    );
    if (this.cm && (plan ?? existing?.plan)) {
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
    if (this.cm) {
      await this.revokeSubscription(uid);
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
    if (this.cm) {
      await this.revokeSubscription(uid);
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
    if (this.cm) {
      await this.revokeSubscription(uid);
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
    if (this.cm) {
      await this.provisionSubscription(uid, offer, event, existing?.plan ?? undefined);
    }
    return { handled: true, action: "subscription_resumed" };
  }

  private async handleTrialWillEnd(event: BillingEvent): Promise<BillingEventResult> {
    if (this.onTrialWillEnd) {
      try {
        await this.onTrialWillEnd(event);
      } catch (err) {
        this.logger?.error?.(
          `[BillingManager] onTrialWillEnd callback failed for ${event.provider}/${event.eventId}`,
          { error: err instanceof Error ? err.message : String(err) },
        );
      }
    }
    return { handled: true, action: "trial_will_end_notified" };
  }

  private async handleInvoicePaid(event: BillingEvent): Promise<BillingEventResult> {
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

    if (topupConfig && this.cm && event.payment.purpose === "credit_topup" && uid) {
      if (
        topupConfig.maxAmountMinor != null &&
        event.payment.amountMinor > topupConfig.maxAmountMinor
      ) {
        this.logger?.warn?.(
          `[BillingManager] topup amount ${event.payment.amountMinor} exceeds cap ${topupConfig.maxAmountMinor} for topup key ${topupConfig.topupKey} (user ${uid})`,
        );
        return { handled: true, action: "payment_succeeded_out_of_bounds" };
      }
      const credits = await this.store.computeTopupCredits(event.payment.amountMinor, topupConfig);
      if (credits > 0) {
        await this.cm.addCredits(uid, new Decimal(credits), {
          type: "purchase",
          bucket: topupConfig.depositTo ?? "purchased",
        });
      }
    }

    return { handled: true, action: "payment_succeeded" };
  }

  private async handlePaymentFailed(event: BillingEvent): Promise<BillingEventResult> {
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
    if (uid && event.subscription && this.cm) {
      const existing = await this.getExistingSubscription(event);
      await this.store.upsertBillingSubscription(
        this.buildSubscriptionState(event, uid, existing, { status: "past_due" }),
      );
      await this.revokeSubscription(uid);
    }
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
      if (event.refund.providerPaymentId && this.cm) {
        const payment = await this.store.getBillingPayment(
          event.provider,
          event.refund.providerPaymentId,
        );
        if (payment?.purpose === "credit_topup") {
          const payMeta = (payment.metadata ?? {}) as Record<string, unknown>;
          const rawCpu =
            (payment.credits_per_unit as string | number | null | undefined) ??
            (payMeta.credits_per_unit as string | number | null | undefined);
          const cpu = Number(rawCpu);
          if (!Number.isFinite(cpu) || cpu <= 0) {
            this.logger?.warn?.(
              `[BillingManager] cannot claw back credits for refund ${event.refund.providerRefundId}: no valid creditsPerUnit in payment metadata`,
            );
            return { handled: true, action: "refund_recorded_no_clawback" };
          }
          const credits = Math.trunc((event.refund.amountMinor * cpu) / 100);
          if (credits > 0) {
            await this.cm.deductCredits(uid, credits, {
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
    if (!this.cm) {
      this.logger?.debug?.(
        `[BillingManager] provisionSubscription: no creditManager for user ${uid}`,
      );
      return;
    }
    const plan = planKeyOverride ?? offer?.plan;
    if (!plan) {
      this.logger?.debug?.(`[BillingManager] provisionSubscription: no plan for user ${uid}`);
      return;
    }
    const periodStart = event.subscription?.periodStart;
    const planAssignedAt = periodStart
      ? (() => {
          const d = new Date(periodStart);
          return isNaN(d.getTime()) ? undefined : d;
        })()
      : undefined;
    await this.cm.setUserPlan(uid, plan, planAssignedAt);

    // Cancel subscriptions from other providers (migration support)
    if (this.cancelPriorProviders && event.provider) {
      const result = await this.store.deactivateOtherProviderSubscriptions(uid, event.provider);
      if (result.deactivatedCount > 0) {
        this.logger?.debug?.(
          `[BillingManager] deactivated ${result.deactivatedCount} prior provider subscription(s) for user ${uid}`,
        );
      }
    }

    const g = offer?.grant;
    if (g?.mode === "cycle_grant" && this.cm) {
      const cycleCredits = g.credits;
      if (cycleCredits && cycleCredits > 0) {
        const cycleBucket = g.bucket ?? "purchased";
        if (g.replacePrior) {
          await this.cm.revokeCreditsByTxType(uid, "cycle_grant");
        }
        await this.cm.addCredits(uid, new Decimal(cycleCredits), {
          type: "cycle_grant",
          bucket: cycleBucket,
        });
      }
    }
  }

  private async revokeSubscription(uid: string): Promise<void> {
    if (!this.cm) return;
    await this.cm.unsetUserPlan(uid);
  }

  private async reEvaluateAccess(uid: string, event: BillingEvent): Promise<void> {
    if (!this.cm || !event.subscription) return;
    const status = event.subscription.status;
    if (status && ["active", "trialing"].includes(status)) {
      const offer = await this.resolveOffer(event);
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
      await this.revokeSubscription(uid);
    }
  }

  private async resolveOffer(event: BillingEvent): Promise<BillingOfferResult | null> {
    const refs = event.subscription?.refs;
    if (!refs) return null;
    return this.resolveBillingOfferCached(
      event.provider,
      refs.productId ?? null,
      refs.priceId ?? null,
    );
  }
}
