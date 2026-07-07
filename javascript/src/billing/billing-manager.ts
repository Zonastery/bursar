import Decimal from "decimal.js";
import type { CreditManager } from "../manager.js";
import type { BillingStore } from "./billing-store.js";
import type {
  BillingEvent,
  BillingEventResult,
  BillingSubscriptionState,
} from "./billing-types.js";

type ResolveUserFn = (
  provider: string,
  providerCustomerId: string | null,
  email: string | null,
) => string | null;

/**
 * Provider-agnostic billing lifecycle state machine.
 * Mirrors Python bursar/billing/manager.py.
 */
export class BillingManager {
  private store: BillingStore;
  private cm: CreditManager | null;
  private resolveUser: ResolveUserFn | null;

  constructor(
    store: BillingStore,
    options?: {
      creditManager?: CreditManager | null;
      resolveUser?: ResolveUserFn | null;
    },
  ) {
    this.store = store;
    this.cm = options?.creditManager ?? null;
    this.resolveUser = options?.resolveUser ?? null;
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

    try {
      const result = await this.routeEvent(event);
      await this.store.completeBillingEvent(event.provider, event.eventId);
      return result;
    } catch (err) {
      await this.store.failBillingEvent(event.provider, event.eventId);
      return { handled: false, error: String(err) };
    }
  }

  private async routeEvent(event: BillingEvent): Promise<BillingEventResult> {
    const handlerMap: Record<string, (e: BillingEvent) => Promise<BillingEventResult>> = {
      "customer.created": this.handleCustomerCreated,
      "customer.updated": this.handleCustomerUpdated,
      "customer.deleted": this.handleCustomerDeleted,
      "checkout.completed": this.handleCheckoutCompleted,
      "subscription.created": this.handleSubscriptionCreated,
      "subscription.updated": this.handleSubscriptionUpdated,
      "subscription.activated": this.handleSubscriptionActivated,
      "subscription.renewed": this.handleSubscriptionRenewed,
      "subscription.plan_changed": this.handleSubscriptionPlanChanged,
      "subscription.cancellation_scheduled": this.handleCancellationScheduled,
      "subscription.cancellation_unscheduled": this.handleCancellationUnscheduled,
      "subscription.canceled": this.handleSubscriptionCanceled,
      "subscription.expired": this.handleSubscriptionExpired,
      "subscription.paused": this.handleSubscriptionPaused,
      "subscription.resumed": this.handleSubscriptionResumed,
      "subscription.trial_will_end": this.handleTrialWillEnd,
      "invoice.paid": this.handleInvoicePaid,
      "payment.succeeded": this.handlePaymentSucceeded,
      "payment.failed": this.handlePaymentFailed,
      "refund.created": this.handleRefundCreated,
      "dispute.created": this.handleDisputeCreated,
      "dispute.closed": this.handleDisputeClosed,
    };

    const handler = handlerMap[event.eventType];
    if (!handler) {
      return { handled: true, action: "ignored" };
    }
    return handler.call(this, event);
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
    return null;
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

  private async handleCustomerUpdated(_event: BillingEvent): Promise<BillingEventResult> {
    return { handled: true, action: "customer_updated" };
  }

  private async handleCustomerDeleted(_event: BillingEvent): Promise<BillingEventResult> {
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
      status?: string | null;
      cancelAtPeriodEnd?: boolean | null;
      offerKey?: string | null;
      planKey?: string | null;
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
      planKey: overrides?.planKey ?? existing?.planKey ?? null,
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

  private async resolveOfferAndKeys(
    event: BillingEvent,
  ): Promise<{
    offer: Record<string, unknown> | null;
    offerKey: string | null;
    planKey: string | null;
  }> {
    const refs = event.subscription?.refs;
    if (!refs) return { offer: null, offerKey: null, planKey: null };
    const offer = await this.store.resolveBillingOffer(
      event.provider,
      refs.productId ?? null,
      refs.priceId ?? null,
    );
    return {
      offer,
      offerKey: (offer?.offerKey as string | null) ?? null,
      planKey: (offer?.planKey as string | null) ?? null,
    };
  }

  private async handleSubscriptionCreated(event: BillingEvent): Promise<BillingEventResult> {
    const uid = await this.resolveUserId(event);
    if (!uid) return { handled: false, error: "user_not_found" };
    if (!event.subscription?.providerSubscriptionId)
      return { handled: false, error: "no_subscription_data" };
    const existing = await this.getExistingSubscription(event);
    const { offer, offerKey, planKey } = await this.resolveOfferAndKeys(event);
    await this.store.upsertBillingSubscription(
      this.buildSubscriptionState(event, uid, existing, {
        status: event.subscription.status ?? "incomplete",
        cancelAtPeriodEnd: event.subscription.cancelAtPeriodEnd ?? false,
        offerKey: offerKey ?? existing?.offerKey ?? null,
        planKey: planKey ?? existing?.planKey ?? null,
      }),
    );
    if (
      this.cm &&
      event.subscription.status &&
      ["active", "trialing"].includes(event.subscription.status)
    ) {
      await this.provisionSubscription(
        uid,
        offer ?? (existing?.planKey ? { planKey: existing.planKey } : null),
        event,
      );
    }
    return { handled: true, action: "subscription_created" };
  }

  private async handleSubscriptionUpdated(event: BillingEvent): Promise<BillingEventResult> {
    const uid = await this.resolveUserId(event);
    if (!uid) return { handled: false, error: "user_not_found" };
    if (!event.subscription?.providerSubscriptionId)
      return { handled: false, error: "no_subscription_data" };
    const existing = await this.getExistingSubscription(event);
    const { offerKey, planKey } = await this.resolveOfferAndKeys(event);
    await this.store.upsertBillingSubscription(
      this.buildSubscriptionState(event, uid, existing, {
        status: event.subscription.status ?? existing?.status ?? "incomplete",
        cancelAtPeriodEnd:
          event.subscription.cancelAtPeriodEnd ?? existing?.cancelAtPeriodEnd ?? false,
        offerKey: offerKey ?? existing?.offerKey ?? null,
        planKey: planKey ?? existing?.planKey ?? null,
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
    const { offer, offerKey, planKey } = await this.resolveOfferAndKeys(event);
    await this.store.upsertBillingSubscription(
      this.buildSubscriptionState(event, uid, existing, {
        status: "active",
        offerKey: offerKey ?? existing?.offerKey ?? null,
        planKey: planKey ?? existing?.planKey ?? null,
      }),
    );
    if (this.cm) {
      await this.provisionSubscription(
        uid,
        offer ?? (existing?.planKey ? { planKey: existing.planKey } : null),
        event,
      );
    }
    return { handled: true, action: "subscription_activated" };
  }

  private async handleSubscriptionRenewed(event: BillingEvent): Promise<BillingEventResult> {
    const uid = await this.resolveUserId(event);
    if (!uid) return { handled: false, error: "user_not_found" };
    if (!event.subscription?.providerSubscriptionId)
      return { handled: false, error: "no_subscription_data" };
    const existing = await this.getExistingSubscription(event);
    const { offerKey, planKey } = await this.resolveOfferAndKeys(event);
    const resolvedPlanKey = planKey ?? existing?.planKey ?? null;
    await this.store.upsertBillingSubscription(
      this.buildSubscriptionState(event, uid, existing, {
        status: "active",
        offerKey: offerKey ?? existing?.offerKey ?? null,
        planKey: resolvedPlanKey,
      }),
    );
    if (this.cm && resolvedPlanKey) {
      const periodStart = event.subscription.periodStart;
      await this.cm.setUserPlan(
        uid,
        resolvedPlanKey,
        periodStart ? new Date(periodStart) : undefined,
      );
    }
    return { handled: true, action: "subscription_renewed" };
  }

  private async handleSubscriptionPlanChanged(event: BillingEvent): Promise<BillingEventResult> {
    const uid = await this.resolveUserId(event);
    if (!uid) return { handled: false, error: "user_not_found" };
    if (!event.subscription?.providerSubscriptionId)
      return { handled: false, error: "no_subscription_data" };
    const existing = await this.getExistingSubscription(event);
    const { offer, offerKey, planKey } = await this.resolveOfferAndKeys(event);
    await this.store.upsertBillingSubscription(
      this.buildSubscriptionState(event, uid, existing, {
        status: event.subscription.status ?? "active",
        offerKey: offerKey ?? existing?.offerKey ?? null,
        planKey: planKey ?? existing?.planKey ?? null,
      }),
    );
    if (this.cm && (planKey ?? existing?.planKey)) {
      await this.provisionSubscription(
        uid,
        offer ?? (existing?.planKey ? { planKey: existing.planKey } : null),
        event,
      );
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
    const { offer, offerKey, planKey } = await this.resolveOfferAndKeys(event);
    await this.store.upsertBillingSubscription(
      this.buildSubscriptionState(event, uid, existing, {
        status: "active",
        cancelAtPeriodEnd: false,
        offerKey: offerKey ?? existing?.offerKey ?? null,
        planKey: planKey ?? existing?.planKey ?? null,
      }),
    );
    if (this.cm) {
      await this.provisionSubscription(
        uid,
        offer ?? (existing?.planKey ? { planKey: existing.planKey } : null),
        event,
      );
    }
    return { handled: true, action: "subscription_resumed" };
  }

  private async handleTrialWillEnd(_event: BillingEvent): Promise<BillingEventResult> {
    return { handled: true, action: "trial_will_end" };
  }

  private async handleInvoicePaid(event: BillingEvent): Promise<BillingEventResult> {
    if (event.subscription) {
      return this.handleSubscriptionRenewed(event);
    }
    return { handled: true, action: "invoice_paid" };
  }

  private async handlePaymentSucceeded(event: BillingEvent): Promise<BillingEventResult> {
    if (!event.payment) return { handled: true, action: "payment_succeeded" };

    const refs = event.payment.refs;
    let topupConfig: Record<string, unknown> | null = null;
    if (refs) {
      topupConfig = await this.store.resolveCreditTopup(
        event.provider,
        refs.productId ?? null,
        refs.priceId ?? null,
      );
    }

    if (topupConfig && this.cm && event.payment.purpose === "credit_topup") {
      const uid = await this.resolveUserId(event);
      if (uid) {
        const currency = String(topupConfig.currency ?? "USD");
        if (event.payment.currency.toUpperCase() === currency.toUpperCase()) {
          const minAmount = Number(topupConfig.minAmountMinor ?? 0);
          const maxAmount = Number(topupConfig.maxAmountMinor ?? Number.MAX_SAFE_INTEGER);
          if (event.payment.amountMinor >= minAmount && event.payment.amountMinor <= maxAmount) {
            const credits = await this.store.computeTopupCredits(
              event.payment.amountMinor,
              topupConfig,
            );
            if (credits > 0) {
              await this.cm.addCredits(uid, new Decimal(credits), {
                type: "purchase",
                tier: (topupConfig.tier as string) ?? "purchased",
              });
            }
          }
        }
      }
    }

    return { handled: true, action: "payment_succeeded" };
  }

  private async handlePaymentFailed(_event: BillingEvent): Promise<BillingEventResult> {
    return { handled: true, action: "payment_failed_recorded" };
  }

  private async handleRefundCreated(_event: BillingEvent): Promise<BillingEventResult> {
    return { handled: true, action: "refund_recorded" };
  }

  private async handleDisputeCreated(_event: BillingEvent): Promise<BillingEventResult> {
    return { handled: true, action: "dispute_recorded" };
  }

  private async handleDisputeClosed(_event: BillingEvent): Promise<BillingEventResult> {
    return { handled: true, action: "dispute_closed" };
  }

  private async provisionSubscription(
    uid: string,
    offer: Record<string, unknown> | null,
    event: BillingEvent,
  ): Promise<void> {
    if (!offer || !this.cm) return;
    const planKey = offer.planKey as string | undefined;
    if (!planKey) return;
    const planAssignedAt = event.subscription?.periodStart
      ? new Date(event.subscription.periodStart)
      : undefined;
    await this.cm.setUserPlan(uid, planKey, planAssignedAt);
  }

  private async revokeSubscription(uid: string): Promise<void> {
    if (!this.cm) return;
    await this.cm.unsetUserPlan(uid);
  }

  private async reEvaluateAccess(uid: string, event: BillingEvent): Promise<void> {
    if (!this.cm || !event.subscription) return;
    const status = event.subscription.status;
    if (status && ["active", "trialing"].includes(status)) {
      const existing = await this.store.getBillingSubscription(
        event.provider,
        event.subscription.providerSubscriptionId,
      );
      const offer = await this.resolveOffer(event);
      if (offer) {
        await this.provisionSubscription(uid, offer, event);
      } else if (existing?.planKey) {
        await this.provisionSubscription(uid, { planKey: existing.planKey }, event);
      }
    } else if (
      status &&
      ["canceled", "expired", "unpaid", "paused", "incomplete_expired"].includes(status)
    ) {
      await this.revokeSubscription(uid);
    }
  }

  private async resolveOffer(event: BillingEvent): Promise<Record<string, unknown> | null> {
    if (!event.subscription) return null;
    const refs = event.subscription.refs;
    if (refs?.priceId) {
      const offer = await this.store.resolveBillingOffer(event.provider, null, refs.priceId);
      if (offer) return offer;
    }
    if (refs?.productId) {
      const offer = await this.store.resolveBillingOffer(event.provider, refs.productId);
      if (offer) return offer;
    }
    return null;
  }
}
