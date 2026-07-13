import type {
  BillingConfig,
  BillingCustomerRecord,
  BillingEventClaim,
  BillingOfferResult,
  BillingPreferences,
  BillingSubscriptionState,
  BillingSubscriptionStatus,
  BillingTopupResult,
} from "./billing-types.js";
import { BillingStore } from "./billing-store.js";
import { camelToSnakeKeys, snakeToCamelKeys } from "../case-utils.js";
import type { QueryFn } from "../repositories/types.js";
import { BillingOfferRepository } from "../repositories/billing/offer.js";
import { BillingTopupRepository } from "../repositories/billing/topup.js";
import { BillingCustomerRepository } from "../repositories/billing/customer.js";
import { BillingSubscriptionRepository } from "../repositories/billing/subscription.js";
import { BillingEventRepository } from "../repositories/billing/event.js";
import { BillingPaymentRepository } from "../repositories/billing/payment.js";
import { BillingRefundRepository } from "../repositories/billing/refund.js";
import { BillingInvoiceRepository } from "../repositories/billing/invoice.js";
import { BillingDisputeRepository } from "../repositories/billing/dispute.js";
import { BillingConfigRepository } from "../repositories/billing/config.js";
import { BillingPreferencesRepository } from "../repositories/billing/preferences.js";

function toIso(value: unknown): string | null {
  if (!value) return null;
  if (typeof value === "string") return value;
  if (value instanceof Date) return value.toISOString();
  if (typeof (value as Record<string, unknown>)["toISOString"] === "function") {
    return (value as Date).toISOString();
  }
  return String(value);
}

export class PostgresBillingStore extends BillingStore {
  private pool: import("pg").Pool | null = null;
  private databaseUrl: string | null = null;

  private _offer: BillingOfferRepository | null = null;
  private _topup: BillingTopupRepository | null = null;
  private _customer: BillingCustomerRepository | null = null;
  private _subscription: BillingSubscriptionRepository | null = null;
  private _event: BillingEventRepository | null = null;
  private _payment: BillingPaymentRepository | null = null;
  private _refund: BillingRefundRepository | null = null;
  private _invoice: BillingInvoiceRepository | null = null;
  private _dispute: BillingDisputeRepository | null = null;
  private _config: BillingConfigRepository | null = null;
  private _preferences: BillingPreferencesRepository | null = null;
  private ownsPool: boolean = false;

  constructor(poolOrUrl: import("pg").Pool | string) {
    super();
    if (typeof poolOrUrl === "string") {
      this.databaseUrl = poolOrUrl;
      this.ownsPool = true;
    } else {
      this.pool = poolOrUrl;
      this.ownsPool = false;
    }
  }

  async close(): Promise<void> {
    if (this.pool && this.ownsPool) {
      await this.pool.end();
      this.pool = null;
    }
  }

  private async getPool(): Promise<import("pg").Pool> {
    if (!this.pool) {
      if (!this.databaseUrl) {
        throw new Error(
          "PostgresBillingStore not initialized — no connection string or pool provided",
        );
      }
      const pg = await import("pg");
      this.pool = new pg.Pool({
        connectionString: this.databaseUrl,
      }) as unknown as import("pg").Pool;
    }
    return this.pool;
  }

  private get queryFn(): QueryFn {
    return async (text: string, params?: unknown[]) => {
      const result = await (await this.getPool()).query(text, params);
      return result.rows;
    };
  }

  private get billingOffer(): BillingOfferRepository {
    if (!this._offer) this._offer = new BillingOfferRepository(this.queryFn);
    return this._offer;
  }

  private get billingTopup(): BillingTopupRepository {
    if (!this._topup) this._topup = new BillingTopupRepository(this.queryFn);
    return this._topup;
  }

  private get billingCustomer(): BillingCustomerRepository {
    if (!this._customer) this._customer = new BillingCustomerRepository(this.queryFn);
    return this._customer;
  }

  private get billingSubscription(): BillingSubscriptionRepository {
    if (!this._subscription) this._subscription = new BillingSubscriptionRepository(this.queryFn);
    return this._subscription;
  }

  private get billingEvent(): BillingEventRepository {
    if (!this._event) this._event = new BillingEventRepository(this.queryFn);
    return this._event;
  }

  private get billingPayment(): BillingPaymentRepository {
    if (!this._payment) this._payment = new BillingPaymentRepository(this.queryFn);
    return this._payment;
  }

  private get billingRefund(): BillingRefundRepository {
    if (!this._refund) this._refund = new BillingRefundRepository(this.queryFn);
    return this._refund;
  }

  private get billingInvoice(): BillingInvoiceRepository {
    if (!this._invoice) this._invoice = new BillingInvoiceRepository(this.queryFn);
    return this._invoice;
  }

  private get billingDispute(): BillingDisputeRepository {
    if (!this._dispute) this._dispute = new BillingDisputeRepository(this.queryFn);
    return this._dispute;
  }

  private get billingConfig(): BillingConfigRepository {
    if (!this._config) this._config = new BillingConfigRepository(this.queryFn);
    return this._config;
  }

  private get billingPreferences(): BillingPreferencesRepository {
    if (!this._preferences) this._preferences = new BillingPreferencesRepository(this.queryFn);
    return this._preferences;
  }

  async syncBillingFromConfig(config: BillingConfig): Promise<void> {
    await this.billingConfig.syncFromConfig(JSON.stringify(camelToSnakeKeys(config)));
  }

  private rowToOffer(r: Record<string, unknown>): BillingOfferResult | null {
    if (!r?.offer_key) return null;
    return {
      offerKey: r.offer_key as string,
      plan: (r.plan as string | undefined) ?? null,
      interval: (r.interval as string | undefined) ?? "month",
      intervalCount: Number(r.interval_count ?? 1),
      grant: {
        mode: r.grant_mode as string | undefined,
        credits: r.grant_credits != null ? Number(r.grant_credits) : null,
        bucket: (r.grant_bucket as string | undefined) ?? undefined,
        replacePrior: r.grant_replace_prior === true,
      },
    };
  }

  async resolveBillingOffer(
    provider: string,
    productId?: string | null,
    priceId?: string | null,
  ): Promise<BillingOfferResult | null> {
    const r = await this.billingOffer.resolveByPrice(provider, priceId ?? null, productId ?? null);
    return this.rowToOffer(r as Record<string, unknown>);
  }

  async resolveBillingOfferByLookup(
    provider: string,
    lookupKey: string,
  ): Promise<BillingOfferResult | null> {
    const r = await this.billingOffer.resolveByLookup(provider, lookupKey);
    return this.rowToOffer(r as Record<string, unknown>);
  }

  async claimBillingEvent(
    provider: string,
    eventId: string,
    eventType: string,
  ): Promise<BillingEventClaim> {
    const result = await this.billingEvent.claim(
      provider,
      eventId,
      eventType,
      JSON.stringify({ eventType }),
    );
    if (!result) return { status: "retry" as const };
    const r = snakeToCamelKeys(result) as Record<string, unknown>;
    const s = r.status as string;
    if (s === "claimed") return { status: "claimed" as const };
    if (s === "duplicate") return { status: "duplicate" as const };
    return { status: "retry" as const };
  }

  async completeBillingEvent(provider: string, eventId: string): Promise<void> {
    await this.billingEvent.complete(provider, eventId);
  }

  async failBillingEvent(provider: string, eventId: string): Promise<void> {
    await this.billingEvent.fail(provider, eventId);
  }

  async upsertBillingCustomer(
    provider: string,
    providerCustomerId: string,
    userId: string,
    email?: string | null,
  ): Promise<void> {
    await this.billingCustomer.upsert(provider, providerCustomerId, userId, email ?? null);
  }

  async upsertBillingSubscription(state: BillingSubscriptionState): Promise<void> {
    await this.billingSubscription.upsert(state as unknown as Record<string, unknown>);
  }

  async getBillingCustomer(provider: string, providerCustomerId: string): Promise<string | null> {
    return this.billingCustomer.get(provider, providerCustomerId);
  }

  async getBillingSubscription(
    provider: string,
    providerSubscriptionId: string,
  ): Promise<BillingSubscriptionState | null> {
    const r = await this.billingSubscription.get(provider, providerSubscriptionId);
    if (!r) return null;
    return this.rowToSubscriptionState(r);
  }

  async getUserSubscription(
    userId: string,
    statuses?: string[],
  ): Promise<BillingSubscriptionState | null> {
    const r = await this.billingSubscription.getUserSubscription(userId, statuses);
    if (!r) return null;
    return this.rowToSubscriptionState(r);
  }

  async getUserSubscriptions(userId: string): Promise<BillingSubscriptionState[]> {
    const rows = await this.billingSubscription.getUserSubscriptions(userId);
    return rows.map((r) => this.rowToSubscriptionState(r));
  }

  async deactivateOtherProviderSubscriptions(
    userId: string,
    keepProvider: string,
  ): Promise<{ userId: string; keepProvider: string; deactivatedCount: number }> {
    const deactivatedCount = await this.billingSubscription.deactivateOtherProviderSubscriptions(
      userId,
      keepProvider,
    );
    return { userId, keepProvider, deactivatedCount };
  }

  private rowToTopup(r: Record<string, unknown>): BillingTopupResult | null {
    if (!r?.topup_key) return null;
    return {
      topupKey: r.topup_key as string,
      creditsPerUnit: Number(r.credits_per_unit ?? r.credits_per_major_unit ?? 1000),
      depositTo: (r.deposit_to as string | undefined) || "purchased",
      maxAmountMinor: r.max_amount_minor != null ? Number(r.max_amount_minor) : undefined,
    };
  }

  async resolveCreditTopup(
    provider: string,
    productId?: string | null,
    priceId?: string | null,
  ): Promise<BillingTopupResult | null> {
    const r = await this.billingTopup.resolveByPrice(provider, priceId ?? null, productId ?? null);
    return this.rowToTopup(r as Record<string, unknown>);
  }

  async computeTopupCredits(amountMinor: number, topupConfig: BillingTopupResult): Promise<number> {
    const creditsPer = topupConfig.creditsPerUnit ?? 1000;
    return Math.trunc((amountMinor * creditsPer) / 100);
  }

  async upsertBillingPayment(options: {
    provider: string;
    providerPaymentId: string;
    providerInvoiceId?: string | null;
    userId?: string | null;
    amountMinor?: number;
    taxMinor?: number | null;
    currency?: string | null;
    purpose?: string;
    metadata?: Record<string, unknown> | null;
  }): Promise<void> {
    await this.billingPayment.upsert(
      options.provider,
      options.providerPaymentId,
      options.providerInvoiceId ?? null,
      options.userId ?? null,
      options.amountMinor ?? 0,
      options.taxMinor ?? null,
      options.currency ?? "USD",
      options.purpose ?? null,
      options.metadata ? JSON.stringify(options.metadata) : null,
    );
  }

  async upsertBillingRefund(options: {
    provider: string;
    providerRefundId: string;
    providerPaymentId?: string | null;
    userId?: string | null;
    amountMinor?: number;
    currency?: string | null;
    reason?: string | null;
    metadata?: Record<string, unknown> | null;
  }): Promise<void> {
    await this.billingRefund.upsert(
      options.provider,
      options.providerRefundId,
      options.providerPaymentId ?? null,
      options.userId ?? null,
      options.amountMinor ?? 0,
      options.currency ?? "USD",
      options.reason ?? null,
      options.metadata ? JSON.stringify(options.metadata) : null,
    );
  }

  async upsertBillingInvoice(options: {
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
  }): Promise<void> {
    await this.billingInvoice.upsert(
      options.provider,
      options.providerInvoiceId,
      options.providerSubscriptionId ?? null,
      options.userId ?? null,
      options.status ?? null,
      options.amountPaidMinor ?? null,
      options.amountDueMinor ?? null,
      options.currency ?? "USD",
      options.periodStart ?? null,
      options.periodEnd ?? null,
      options.metadata ? JSON.stringify(options.metadata) : null,
    );
  }

  async upsertBillingDispute(options: {
    provider: string;
    providerDisputeId: string;
    providerPaymentId?: string | null;
    userId?: string | null;
    status?: string;
    reason?: string | null;
    metadata?: Record<string, unknown> | null;
  }): Promise<void> {
    await this.billingDispute.upsert(
      options.provider,
      options.providerDisputeId,
      options.providerPaymentId ?? null,
      options.userId ?? null,
      options.status ?? "needs_response",
      options.reason ?? null,
      options.metadata ? JSON.stringify(options.metadata) : null,
    );
  }

  async getBillingPayment(
    provider: string,
    providerPaymentId: string,
  ): Promise<Record<string, unknown> | null> {
    const result = await this.billingPayment.getForRefund(provider, providerPaymentId);
    if (!result) return null;
    return snakeToCamelKeys(result) as Record<string, unknown>;
  }

  private rowToSubscriptionState(r: Record<string, unknown>): BillingSubscriptionState {
    return {
      userId: String(r.user_id),
      provider: String(r.provider),
      providerSubscriptionId: String(r.provider_subscription_id),
      providerCustomerId: r.provider_customer_id ? String(r.provider_customer_id) : null,
      offerKey: r.offer_key ? String(r.offer_key) : null,
      plan: r.plan ? String(r.plan) : null,
      status: (r.status ? String(r.status) : "incomplete") as BillingSubscriptionStatus,
      currentPeriodStart: toIso(r.current_period_start),
      currentPeriodEnd: toIso(r.current_period_end),
      cancelAtPeriodEnd: Boolean(r.cancel_at_period_end),
      interval: r.interval ? String(r.interval) : null,
      intervalCount: r.interval_count ? Number(r.interval_count) : null,
      metadata:
        r.metadata && typeof r.metadata === "object"
          ? (r.metadata as Record<string, unknown>)
          : null,
    };
  }

  async getActivePricingConfig(): Promise<Record<string, unknown> | null> {
    const rows = await this.queryFn(
      "SELECT config FROM public.credit_pricing_config WHERE active = TRUE LIMIT 1",
      [],
    );
    if (!rows || rows.length === 0) return null;
    return ((rows[0] as Record<string, unknown>)?.config as Record<string, unknown> | null) ?? null;
  }

  async getBillingPreferences(userId: string): Promise<BillingPreferences | null> {
    const row = await this.billingPreferences.get(userId);
    if (!row) return null;
    return {
      userId: String(row.user_id),
      autoRecharge: Boolean(row.auto_recharge),
      overageProtection: Boolean(row.overage_protection),
      emailNotifications: Boolean(row.email_notifications),
      usageAlerts: Boolean(row.usage_alerts),
      invoiceReminders: Boolean(row.invoice_reminders),
      usageLimitAlerts: Boolean(row.usage_limit_alerts),
    };
  }

  async upsertBillingPreferences(prefs: BillingPreferences): Promise<void> {
    await this.billingPreferences.upsert({
      userId: prefs.userId,
      autoRecharge: prefs.autoRecharge,
      overageProtection: prefs.overageProtection,
      emailNotifications: prefs.emailNotifications,
      usageAlerts: prefs.usageAlerts,
      invoiceReminders: prefs.invoiceReminders,
      usageLimitAlerts: prefs.usageLimitAlerts,
    });
  }

  async getBillingCustomerByUserId(
    userId: string,
    provider?: string | null,
  ): Promise<BillingCustomerRecord | null> {
    const result = await this.billingCustomer.getByUserId(userId, provider ?? null);
    if (!result) return null;
    return {
      provider: result.provider,
      providerCustomerId: result.providerCustomerId,
    };
  }
}
