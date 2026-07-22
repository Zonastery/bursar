import type {
  BillingConfig,
  BillingAutoRechargeAttempt,
  BillingAutoRechargeProfile,
  BillingCustomerRecord,
  BillingEventClaim,
  BillingOfferResult,
  BillingPreferences,
  BillingSubscriptionChange,
  BillingSubscriptionState,
  BillingSubscriptionStatus,
  CheckoutIntent,
  BillingTopupResult,
  ProviderRef,
} from "./billing-types.js";
import { BillingStore } from "./billing-store.js";
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
import { BillingAutoRechargeRepository } from "../repositories/billing/auto-recharge.js";

function toIso(value: unknown): string | null {
  if (!value) return null;
  if (typeof value === "string") return value;
  if (value instanceof Date) return value.toISOString();
  if (typeof (value as Record<string, unknown>)["toISOString"] === "function") {
    return (value as Date).toISOString();
  }
  return String(value);
}

function billingConfigToSnake(config: BillingConfig): Record<string, unknown> {
  const providerRefs = (refs: Record<string, ProviderRef> | undefined) =>
    refs
      ? Object.fromEntries(
          Object.entries(refs).map(([provider, ref]) => [
            provider,
            {
              ...(ref.productId != null ? { product_id: ref.productId } : {}),
              ...(ref.priceId != null ? { price_id: ref.priceId } : {}),
              ...(ref.variantId != null ? { variant_id: ref.variantId } : {}),
              ...(ref.lookupKey != null ? { lookup_key: ref.lookupKey } : {}),
            },
          ]),
        )
      : undefined;

  return {
    currency: config.currency,
    subscriptions: Object.fromEntries(
      Object.entries(config.subscriptions ?? {}).map(([key, offer]) => [
        key,
        {
          plan: offer.plan,
          interval: offer.interval,
          ...(offer.intervalCount != null ? { interval_count: offer.intervalCount } : {}),
          ...(offer.grant
            ? {
                grant:
                  offer.grant.mode === "cycle_grant"
                    ? {
                        mode: offer.grant.mode,
                        credits: offer.grant.credits,
                        bucket: offer.grant.bucket,
                        replace_prior: offer.grant.replacePrior,
                      }
                    : { mode: offer.grant.mode },
              }
            : {}),
          ...(providerRefs(offer.providers) ? { providers: providerRefs(offer.providers) } : {}),
          ...(offer.validFrom != null ? { valid_from: offer.validFrom } : {}),
          ...(offer.validTo != null ? { valid_to: offer.validTo } : {}),
        },
      ]),
    ),
    topups: Object.fromEntries(
      Object.entries(config.topups ?? {}).map(([key, topup]) => [
        key,
        {
          deposit_to: topup.depositTo,
          ...(topup.creditsPerUnit != null ? { credits_per_unit: topup.creditsPerUnit } : {}),
          ...(topup.minAmountMinor != null ? { min_amount_minor: topup.minAmountMinor } : {}),
          ...(topup.maxAmountMinor != null ? { max_amount_minor: topup.maxAmountMinor } : {}),
          ...(topup.taxBehavior != null ? { tax_behavior: topup.taxBehavior } : {}),
          ...(providerRefs(topup.providers) ? { providers: providerRefs(topup.providers) } : {}),
        },
      ]),
    ),
    ...(config.autoRecharge
      ? {
          auto_recharge: {
            enabled: config.autoRecharge.enabled,
            threshold_credits: config.autoRecharge.thresholdCredits,
            topup_key: config.autoRecharge.topupKey,
            quantity: config.autoRecharge.quantity,
            max_recharges: config.autoRecharge.maxRecharges,
            window_days: config.autoRecharge.windowDays,
          },
        }
      : {}),
  };
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
  private _autoRecharge: BillingAutoRechargeRepository | null = null;
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

  private get billingAutoRecharge(): BillingAutoRechargeRepository {
    if (!this._autoRecharge) this._autoRecharge = new BillingAutoRechargeRepository(this.queryFn);
    return this._autoRecharge;
  }

  async syncBillingFromConfig(config: BillingConfig): Promise<void> {
    await this.billingConfig.syncFromConfig(JSON.stringify(billingConfigToSnake(config)));
  }

  async createOrGetCheckoutIntent(input: {
    actorKey: string;
    provider: string;
    type: "subscription" | "credit_pack";
    productId: string;
    requestFingerprint: string;
    expiresAt: string;
  }): Promise<CheckoutIntent> {
    await this.queryFn(
      `UPDATE bursar.billing_checkout_intents
          SET status = 'expired', updated_at = now()
        WHERE actor_key = $1 AND status = 'open' AND expires_at <= now()
        RETURNING id`,
      [input.actorKey],
    );
    const result = await this.queryFn(
      `INSERT INTO bursar.billing_checkout_intents
        (actor_key, provider, type, product_id, request_fingerprint, expires_at)
       VALUES ($1, $2, $3, $4, $5, $6)
       ON CONFLICT (actor_key) WHERE status = 'open'
       DO UPDATE SET updated_at = now()
       RETURNING id, actor_key, provider, type, product_id, request_fingerprint, status,
                 provider_session_id, checkout_url, expires_at`,
      [
        input.actorKey,
        input.provider,
        input.type,
        input.productId,
        input.requestFingerprint,
        input.expiresAt,
      ],
    );
    const row = result[0] as Record<string, unknown>;
    return {
      id: String(row.id),
      actorKey: String(row.actor_key),
      provider: String(row.provider),
      type: row.type as "subscription" | "credit_pack",
      productId: String(row.product_id),
      requestFingerprint: String(row.request_fingerprint),
      status: row.status as CheckoutIntent["status"],
      providerSessionId: row.provider_session_id ? String(row.provider_session_id) : null,
      checkoutUrl: row.checkout_url ? String(row.checkout_url) : null,
      expiresAt: toIso(row.expires_at) ?? input.expiresAt,
    };
  }

  async updateCheckoutIntent(
    id: string,
    update: {
      status?: "open" | "completed" | "failed" | "expired";
      providerSessionId?: string | null;
      checkoutUrl?: string | null;
    },
  ): Promise<void> {
    await this.queryFn(
      `UPDATE bursar.billing_checkout_intents
          SET status = COALESCE($2, status),
              provider_session_id = COALESCE($3, provider_session_id),
              checkout_url = COALESCE($4, checkout_url),
              updated_at = now()
        WHERE id = $1`,
      [id, update.status ?? null, update.providerSessionId ?? null, update.checkoutUrl ?? null],
    );
  }

  async getCheckoutIntent(id: string, actorKey: string): Promise<CheckoutIntent | null> {
    const result = await this.queryFn(
      `SELECT id, actor_key, provider, type, product_id, request_fingerprint, status,
              provider_session_id, checkout_url, expires_at
         FROM bursar.billing_checkout_intents
        WHERE id = $1 AND actor_key = $2
        LIMIT 1`,
      [id, actorKey],
    );
    const row = result[0] as Record<string, unknown> | undefined;
    if (!row) return null;
    return {
      id: String(row.id),
      actorKey: String(row.actor_key),
      provider: String(row.provider),
      type: row.type as "subscription" | "credit_pack",
      productId: String(row.product_id),
      requestFingerprint: String(row.request_fingerprint),
      status: row.status as CheckoutIntent["status"],
      providerSessionId: row.provider_session_id ? String(row.provider_session_id) : null,
      checkoutUrl: row.checkout_url ? String(row.checkout_url) : null,
      expiresAt: toIso(row.expires_at) ?? new Date(0).toISOString(),
    };
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
    const r = result as Record<string, unknown>;
    const s = r.status as string;
    if (s === "claimed" && typeof r.claim_token === "string")
      return { status: "claimed" as const, claimToken: r.claim_token };
    if (s === "duplicate") return { status: "duplicate" as const };
    return { status: "retry" as const };
  }

  async completeBillingEvent(provider: string, eventId: string, claimToken: string): Promise<void> {
    await this.billingEvent.complete(provider, eventId, claimToken);
  }

  async failBillingEvent(
    provider: string,
    eventId: string,
    claimToken: string,
    error?: string,
  ): Promise<void> {
    await this.billingEvent.fail(provider, eventId, claimToken, error);
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

  async recordSubscriptionConflict(input: {
    userId?: string | null;
    provider: string;
    duplicateSubscriptionId: string;
    existingSubscriptionId?: string | null;
    eventId?: string | null;
    metadata?: Record<string, unknown>;
  }): Promise<void> {
    await this.queryFn(
      `INSERT INTO bursar.billing_subscription_conflicts
        (user_id, provider, duplicate_subscription_id, existing_subscription_id, event_id, metadata)
       VALUES ($1, $2, $3, $4, $5, $6)
       ON CONFLICT (provider, duplicate_subscription_id) DO NOTHING`,
      [
        input.userId ?? null,
        input.provider,
        input.duplicateSubscriptionId,
        input.existingSubscriptionId ?? null,
        input.eventId ?? null,
        JSON.stringify(input.metadata ?? {}),
      ],
    );
  }

  async getBillingSubscription(
    provider: string,
    providerSubscriptionId: string,
  ): Promise<BillingSubscriptionState | null> {
    const r = await this.billingSubscription.get(provider, providerSubscriptionId);
    if (!r) return null;
    return this.rowToSubscriptionState(r);
  }

  async createBillingSubscriptionChange(
    input: Omit<BillingSubscriptionChange, "id">,
  ): Promise<BillingSubscriptionChange> {
    const rows = await this.queryFn(
      `INSERT INTO bursar.billing_subscription_changes
       (user_id, provider, provider_subscription_id, from_plan, from_interval, to_plan, to_interval, effective_at, state, proration_billing_mode, quote, quote_hash, provider_operation_id, effective_date, expires_at)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15) RETURNING *`,
      [
        input.userId,
        input.provider,
        input.providerSubscriptionId,
        input.fromPlan ?? null,
        input.fromInterval ?? null,
        input.toPlan,
        input.toInterval,
        input.effectiveAt,
        input.state,
        input.prorationBillingMode,
        JSON.stringify(input.quote),
        input.quoteHash,
        input.providerOperationId ?? null,
        input.effectiveDate ?? null,
        input.expiresAt ?? null,
      ],
    );
    return this.rowToSubscriptionChange(rows[0] as Record<string, unknown>);
  }

  async getOpenBillingSubscriptionChange(
    provider: string,
    providerSubscriptionId: string,
  ): Promise<BillingSubscriptionChange | null> {
    const rows = await this.queryFn(
      `SELECT * FROM bursar.billing_subscription_changes WHERE provider = $1 AND provider_subscription_id = $2
       AND state IN ('awaiting_payment','scheduled') ORDER BY created_at DESC LIMIT 1`,
      [provider, providerSubscriptionId],
    );
    return rows[0] ? this.rowToSubscriptionChange(rows[0] as Record<string, unknown>) : null;
  }

  async listExpiredGraceSubscriptions(now: string): Promise<BillingSubscriptionState[]> {
    const rows = await this.queryFn(
      `SELECT * FROM bursar.billing_subscriptions WHERE status = 'past_due'
       AND grace_ends_at IS NOT NULL AND grace_ends_at <= $1`,
      [now],
    );
    return rows.map((row) => this.rowToSubscriptionState(row as Record<string, unknown>));
  }

  async updateBillingSubscriptionChange(
    id: string,
    update: Partial<
      Pick<BillingSubscriptionChange, "state" | "providerOperationId" | "effectiveDate">
    >,
  ): Promise<void> {
    await this.queryFn(
      `UPDATE bursar.billing_subscription_changes SET state = COALESCE($2, state),
       provider_operation_id = COALESCE($3, provider_operation_id), effective_date = COALESCE($4, effective_date),
       completed_at = CASE WHEN $2 = 'completed' THEN now() ELSE completed_at END, updated_at = now() WHERE id = $1`,
      [id, update.state ?? null, update.providerOperationId ?? null, update.effectiveDate ?? null],
    );
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

  async pseudonymizeFinancialSubject(userId: string): Promise<void> {
    await this.queryFn("SELECT bursar.pseudonymize_financial_subject($1::uuid)", [userId]);
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

  async listBillingInvoices(userId: string) {
    return this.billingInvoice.listForUser(userId);
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
    const row = result as Record<string, unknown>;
    return {
      purpose: row.purpose,
      metadata: row.metadata,
    };
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
      graceEndsAt: toIso(r.grace_ends_at),
      metadata:
        r.metadata && typeof r.metadata === "object"
          ? (r.metadata as Record<string, unknown>)
          : null,
    };
  }

  private rowToSubscriptionChange(r: Record<string, unknown>): BillingSubscriptionChange {
    return {
      id: String(r.id),
      userId: String(r.user_id),
      provider: String(r.provider),
      providerSubscriptionId: String(r.provider_subscription_id),
      fromPlan: r.from_plan ? String(r.from_plan) : null,
      fromInterval: r.from_interval ? String(r.from_interval) : null,
      toPlan: String(r.to_plan),
      toInterval: String(r.to_interval),
      effectiveAt: String(r.effective_at) as BillingSubscriptionChange["effectiveAt"],
      state: String(r.state) as BillingSubscriptionChange["state"],
      prorationBillingMode: String(r.proration_billing_mode),
      quote: (r.quote ?? {}) as Record<string, unknown>,
      quoteHash: String(r.quote_hash),
      providerOperationId: r.provider_operation_id ? String(r.provider_operation_id) : null,
      effectiveDate: toIso(r.effective_date),
      expiresAt: toIso(r.expires_at),
    };
  }

  async getActiveBursarConfig(): Promise<Record<string, unknown> | null> {
    const rows = await this.queryFn(
      "SELECT config FROM bursar.bursar_config WHERE active = TRUE LIMIT 1",
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

  async getAutoRechargeProfile(userId: string): Promise<BillingAutoRechargeProfile | null> {
    return this.billingAutoRecharge.getProfile(userId);
  }

  async upsertAutoRechargeProfile(profile: BillingAutoRechargeProfile): Promise<void> {
    return this.billingAutoRecharge.upsertProfile(profile);
  }

  async claimAutoRechargeAttempt(input: {
    userId: string;
    provider: string;
    topupKey: string;
    quantity: number;
    maxRecharges: number;
    windowDays: number;
  }): Promise<BillingAutoRechargeAttempt | null> {
    return this.billingAutoRecharge.claimAttempt(input);
  }

  async updateAutoRechargeAttempt(input: {
    id: string;
    state: string;
    providerPaymentId?: string | null;
    failureCode?: string | null;
    actionUrl?: string | null;
  }): Promise<void> {
    return this.billingAutoRecharge.updateAttempt(input);
  }

  async updateAutoRechargeAttemptByProviderPayment(input: {
    provider: string;
    providerPaymentId: string;
    state: string;
    failureCode?: string | null;
  }): Promise<void> {
    return this.billingAutoRecharge.updateAttemptByProviderPayment(input);
  }

  async countAutoRechargeAttempts(userId: string, windowDays: number): Promise<number> {
    return this.billingAutoRecharge.countAttempts(userId, windowDays);
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
