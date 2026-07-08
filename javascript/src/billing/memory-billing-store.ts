import type {
  BillingConfig,
  BillingEventClaim,
  BillingSubscriptionState,
} from "./billing-types.js";
import { BillingStore } from "./billing-store.js";

/**
 * In-memory billing store — reference implementation for testing.
 * Mirrors Python bursar/billing/memory.py.
 */
export class MemoryBillingStore extends BillingStore {
  private offers = new Map<string, Record<string, unknown>>();
  private topups = new Map<string, Record<string, unknown>>();
  private events = new Map<string, string>();
  private customers = new Map<string, string>();
  private subscriptions = new Map<string, BillingSubscriptionState>();
  private providerRefsBy = new Map<string, string>();
  private payments = new Map<string, Record<string, unknown>>();
  private refunds = new Map<string, Record<string, unknown>>();
  private invoices = new Map<string, Record<string, unknown>>();
  private disputes = new Map<string, Record<string, unknown>>();

  private refKey(provider: string, field: string, value: string): string {
    return `${provider}:${field}:${value}`;
  }

  async syncBillingFromConfig(config: BillingConfig): Promise<void> {
    this.offers.clear();
    this.topups.clear();
    this.providerRefsBy.clear();

    const subs = config.subscriptions ?? {};
    for (const [offerKey, offer] of Object.entries(subs)) {
      this.offers.set(offerKey, { ...offer, offerKey });
      const refs = offer.providerRefs ?? {};
      for (const [provider, ref] of Object.entries(refs)) {
        if (ref.priceId) {
          this.providerRefsBy.set(this.refKey(provider, "price_id", ref.priceId), offerKey);
        }
        if (ref.productId) {
          this.providerRefsBy.set(this.refKey(provider, "product_id", ref.productId), offerKey);
        }
      }
    }

    const topupConfigs = config.creditTopups ?? {};
    for (const [topupKey, topup] of Object.entries(topupConfigs)) {
      this.topups.set(topupKey, { ...topup, topupKey });
      const refs = topup.providerRefs ?? {};
      for (const [provider, ref] of Object.entries(refs)) {
        if (ref.priceId) {
          this.providerRefsBy.set(this.refKey(provider, "price_id", ref.priceId), topupKey);
        }
        if (ref.productId) {
          this.providerRefsBy.set(this.refKey(provider, "product_id", ref.productId), topupKey);
        }
      }
    }
  }

  async resolveBillingOffer(
    provider: string,
    productId?: string | null,
    priceId?: string | null,
  ): Promise<Record<string, unknown> | null> {
    let resourceKey: string | undefined;
    if (priceId) {
      resourceKey = this.providerRefsBy.get(this.refKey(provider, "price_id", priceId));
    }
    if (!resourceKey && productId) {
      resourceKey = this.providerRefsBy.get(this.refKey(provider, "product_id", productId));
    }
    if (!resourceKey) return null;

    const raw = this.offers.get(resourceKey);
    if (!raw) return null;
    return { ...raw, offerKey: resourceKey };
  }

  async claimBillingEvent(
    provider: string,
    eventId: string,
    _eventType: string,
  ): Promise<BillingEventClaim> {
    const key = this.refKey(provider, "event", eventId);
    const existing = this.events.get(key);
    if (!existing) {
      this.events.set(key, "processing");
      return { status: "claimed" };
    }
    if (existing === "failed") {
      this.events.set(key, "processing");
      return { status: "retry" };
    }
    return { status: "duplicate" };
  }

  async completeBillingEvent(provider: string, eventId: string): Promise<void> {
    const key = this.refKey(provider, "event", eventId);
    if (this.events.has(key)) {
      this.events.set(key, "completed");
    }
  }

  async failBillingEvent(provider: string, eventId: string): Promise<void> {
    const key = this.refKey(provider, "event", eventId);
    if (this.events.has(key)) {
      this.events.set(key, "failed");
    }
  }

  async upsertBillingCustomer(
    provider: string,
    providerCustomerId: string,
    userId: string,
    _email?: string | null,
  ): Promise<void> {
    this.customers.set(this.refKey(provider, "customer", providerCustomerId), userId);
  }

  async upsertBillingSubscription(state: BillingSubscriptionState): Promise<void> {
    const key = this.refKey(state.provider, "subscription", state.providerSubscriptionId);
    this.subscriptions.set(key, state);
  }

  async getBillingCustomer(provider: string, providerCustomerId: string): Promise<string | null> {
    return this.customers.get(this.refKey(provider, "customer", providerCustomerId)) ?? null;
  }

  async getBillingSubscription(
    provider: string,
    providerSubscriptionId: string,
  ): Promise<BillingSubscriptionState | null> {
    const key = this.refKey(provider, "subscription", providerSubscriptionId);
    return this.subscriptions.get(key) ?? null;
  }

  async getUserSubscription(userId: string): Promise<BillingSubscriptionState | null> {
    let latest: BillingSubscriptionState | null = null;
    for (const sub of this.subscriptions.values()) {
      if (sub.userId === userId) {
        if (
          !latest ||
          (sub.currentPeriodStart &&
            (!latest.currentPeriodStart || sub.currentPeriodStart > latest.currentPeriodStart))
        ) {
          latest = sub;
        }
      }
    }
    return latest;
  }

  async resolveCreditTopup(
    provider: string,
    productId?: string | null,
    priceId?: string | null,
  ): Promise<Record<string, unknown> | null> {
    let resourceKey: string | undefined;
    if (priceId) {
      resourceKey = this.providerRefsBy.get(this.refKey(provider, "price_id", priceId));
    }
    if (!resourceKey && productId) {
      resourceKey = this.providerRefsBy.get(this.refKey(provider, "product_id", productId));
    }
    if (!resourceKey) return null;

    const raw = this.topups.get(resourceKey);
    if (!raw) return null;
    return { ...raw, topupKey: resourceKey };
  }

  async computeTopupCredits(
    amountMinor: number,
    topupConfig: Record<string, unknown>,
  ): Promise<number> {
    const creditsPer = (topupConfig.creditsPerMajorUnit as number) ?? 1000;
    return Math.trunc((amountMinor * creditsPer) / 100);
  }

  async upsertBillingPayment(
    provider: string,
    providerPaymentId: string,
    providerInvoiceId?: string | null,
    userId?: string | null,
    amountMinor?: number,
    taxMinor?: number | null,
    currency?: string,
    purpose?: string,
    metadata?: Record<string, unknown> | null,
  ): Promise<void> {
    const key = this.refKey(provider, "payment", providerPaymentId);
    this.payments.set(key, {
      provider,
      providerPaymentId,
      providerInvoiceId: providerInvoiceId ?? null,
      userId: userId ?? null,
      amountMinor: amountMinor ?? 0,
      taxMinor: taxMinor ?? null,
      currency: currency ?? "USD",
      purpose: purpose ?? null,
      metadata: metadata ?? null,
    });
  }

  async upsertBillingRefund(
    provider: string,
    providerRefundId: string,
    providerPaymentId?: string | null,
    userId?: string | null,
    amountMinor?: number,
    currency?: string,
    reason?: string | null,
    metadata?: Record<string, unknown> | null,
  ): Promise<void> {
    const key = this.refKey(provider, "refund", providerRefundId);
    this.refunds.set(key, {
      provider,
      providerRefundId,
      providerPaymentId: providerPaymentId ?? null,
      userId: userId ?? null,
      amountMinor: amountMinor ?? 0,
      currency: currency ?? "USD",
      reason: reason ?? null,
      metadata: metadata ?? null,
    });
  }

  async upsertBillingInvoice(
    provider: string,
    providerInvoiceId: string,
    providerSubscriptionId?: string | null,
    userId?: string | null,
    status?: string | null,
    amountPaidMinor?: number | null,
    amountDueMinor?: number | null,
    currency?: string,
    periodStart?: string | null,
    periodEnd?: string | null,
    metadata?: Record<string, unknown> | null,
  ): Promise<void> {
    const key = this.refKey(provider, "invoice", providerInvoiceId);
    this.invoices.set(key, {
      provider,
      providerInvoiceId,
      providerSubscriptionId: providerSubscriptionId ?? null,
      userId: userId ?? null,
      status: status ?? null,
      amountPaidMinor: amountPaidMinor ?? null,
      amountDueMinor: amountDueMinor ?? null,
      currency: currency ?? "USD",
      periodStart: periodStart ?? null,
      periodEnd: periodEnd ?? null,
      metadata: metadata ?? null,
    });
  }

  async upsertBillingDispute(
    provider: string,
    providerDisputeId: string,
    providerPaymentId?: string | null,
    userId?: string | null,
    status?: string,
    reason?: string | null,
    metadata?: Record<string, unknown> | null,
  ): Promise<void> {
    const key = this.refKey(provider, "dispute", providerDisputeId);
    this.disputes.set(key, {
      provider,
      providerDisputeId,
      providerPaymentId: providerPaymentId ?? null,
      userId: userId ?? null,
      status: status ?? "open",
      reason: reason ?? null,
      metadata: metadata ?? null,
    });
  }

  async getBillingPayment(
    provider: string,
    providerPaymentId: string,
  ): Promise<Record<string, unknown> | null> {
    const key = this.refKey(provider, "payment", providerPaymentId);
    return this.payments.get(key) ?? null;
  }
}
