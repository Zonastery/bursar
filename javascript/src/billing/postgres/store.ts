import type {
  BillingAutoRechargeAttempt,
  BillingAutoRechargeProfile,
  BillingCustomerRecord,
  BillingEventClaim,
  BillingOfferResult,
  BillingPreferences,
  BillingSubscriptionChange,
  BillingSubscriptionChangeInput,
  BillingSubscriptionOfferContext,
  BillingSubscriptionState,
  BillingSubscriptionStatus,
  CheckoutIntent,
  BillingTopupResult,
} from "../types/index.js";
import { BillingStore } from "../billing-store.js";
import type {
  AutoRechargeAttemptClaim,
  AutoRechargeAttemptUpdate,
  AutoRechargeProviderPaymentUpdate,
  BillingCreditGrantCreate,
  BillingDisputeUpsert,
  BillingInvoiceUpsert,
  BillingPaymentUpsert,
  BillingRefundUpsert,
  BillingSubscriptionChangeUpdate,
  BillingSubscriptionConflictCreate,
  CheckoutIntentCreate,
  CheckoutIntentUpdate,
} from "../contracts.js";
import type { QueryFn } from "../../shared/postgres-types.js";
import { PostgresClient } from "../../shared/postgres-client.js";
import { BillingOfferRepository } from "./repositories/offer.js";
import { BillingTopupRepository } from "./repositories/topup.js";
import { BillingCustomerRepository } from "./repositories/customer.js";
import { BillingSubscriptionRepository } from "./repositories/subscription.js";
import { BillingEventRepository } from "./repositories/event.js";
import { BillingPaymentRepository } from "./repositories/payment.js";
import { BillingRefundRepository } from "./repositories/refund.js";
import { BillingInvoiceRepository } from "./repositories/invoice.js";
import { BillingDisputeRepository } from "./repositories/dispute.js";
import { BillingPreferencesRepository } from "./repositories/preferences.js";
import { BillingAutoRechargeRepository } from "./repositories/auto-recharge.js";

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
  private readonly postgres: PostgresClient;

  private _offer: BillingOfferRepository | null = null;
  private _topup: BillingTopupRepository | null = null;
  private _customer: BillingCustomerRepository | null = null;
  private _subscription: BillingSubscriptionRepository | null = null;
  private _event: BillingEventRepository | null = null;
  private _payment: BillingPaymentRepository | null = null;
  private _refund: BillingRefundRepository | null = null;
  private _invoice: BillingInvoiceRepository | null = null;
  private _dispute: BillingDisputeRepository | null = null;
  private _preferences: BillingPreferencesRepository | null = null;
  private _autoRecharge: BillingAutoRechargeRepository | null = null;
  constructor(
    poolOrUrl: import("pg").Pool | string,
    tenantId: string,
    storageOptions?: { billingPayloadBackend?: "postgres" | "s3" },
  ) {
    super();
    this.postgres = new PostgresClient(poolOrUrl, {
      tenantId,
      billingPayloadBackend: storageOptions?.billingPayloadBackend,
    });
  }

  async close(): Promise<void> {
    await this.postgres.close();
  }

  private get queryFn(): QueryFn {
    return this.postgres.query;
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

  private get billingPreferences(): BillingPreferencesRepository {
    if (!this._preferences) this._preferences = new BillingPreferencesRepository(this.queryFn);
    return this._preferences;
  }

  private get billingAutoRecharge(): BillingAutoRechargeRepository {
    if (!this._autoRecharge) this._autoRecharge = new BillingAutoRechargeRepository(this.queryFn);
    return this._autoRecharge;
  }

  async createOrGetCheckoutIntent(input: CheckoutIntentCreate): Promise<CheckoutIntent> {
    if (!/^[0-9a-fA-F]{64}$/.test(input.requestDigest)) {
      throw new Error("requestDigest must be a 32-byte hex string");
    }
    const rows = await this.queryFn(
      `SELECT bursar.create_checkout_intent($1::uuid, $2, $3, $4, decode($5, 'hex'), $6::timestamptz) AS id`,
      [
        input.subjectId,
        input.provider,
        input.checkoutKind,
        input.productKey,
        input.requestDigest,
        input.expiresAt,
      ],
    );
    const id = (rows[0] as Record<string, unknown> | undefined)?.id;
    if (!id) throw new Error("checkout intent creation returned no ID");
    const intent = await this.getCheckoutIntent(String(id), input.subjectId);
    if (!intent) throw new Error("checkout intent creation returned no row");
    return intent;
  }

  async updateCheckoutIntent(id: string, update: CheckoutIntentUpdate): Promise<void> {
    const rows = await this.queryFn(
      `SELECT bursar.advance_checkout_intent($1::uuid, $2, $3, $4) AS advanced`,
      [id, update.status ?? null, update.providerSessionId ?? null, update.checkoutUrl ?? null],
    );
    if (!(rows[0] as Record<string, unknown> | undefined)?.advanced) {
      throw new Error(`checkout intent transition rejected: ${id}`);
    }
  }

  async getCheckoutIntent(id: string, subjectId: string): Promise<CheckoutIntent | null> {
    const rows = await this.queryFn(
      `SELECT * FROM bursar.get_checkout_intent($1::uuid, $2::uuid)`,
      [id, subjectId],
    );
    const row = rows[0] as Record<string, unknown> | undefined;
    return row?.id == null ? null : this.rowToCheckoutIntent(row);
  }

  private rowToCheckoutIntent(row: Record<string, unknown>): CheckoutIntent {
    return {
      id: String(row.id),
      subjectId: String(row.subject_id),
      provider: String(row.provider),
      checkoutKind: row.checkout_kind as CheckoutIntent["checkoutKind"],
      productKey: String(row.product_key),
      requestDigest: Buffer.from(row.request_digest as Uint8Array).toString("hex"),
      status: row.status as CheckoutIntent["status"],
      providerSessionId: row.provider_session_id == null ? null : String(row.provider_session_id),
      checkoutUrl: row.checkout_url == null ? null : String(row.checkout_url),
      expiresAt: toIso(row.expires_at) ?? new Date(0).toISOString(),
    };
  }

  private rowToOffer(row: Record<string, unknown> | null): BillingOfferResult | null {
    if (!row?.offer_key || !row.id) return null;
    return {
      offerId: String(row.id),
      offerKey: String(row.offer_key),
      planId: row.plan_id == null ? null : String(row.plan_id),
      plan: row.plan == null ? null : String(row.plan),
      interval: row.interval == null ? "month" : String(row.interval),
      intervalCount: Number(row.interval_count ?? 1),
      grant: {
        mode: row.grant_mode == null ? undefined : String(row.grant_mode),
        credits: row.grant_credits == null ? null : Number(row.grant_credits),
        bucket: row.grant_bucket == null ? undefined : String(row.grant_bucket),
        replacePrior: row.grant_replace_prior === true,
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
    envelope?: Record<string, unknown>,
  ): Promise<BillingEventClaim> {
    const result = await this.billingEvent.claim(
      provider,
      eventId,
      eventType,
      JSON.stringify(envelope ?? { eventType }),
    );
    if (!result) return { status: "retry" as const };
    const r = result as Record<string, unknown>;
    const s = r.status as string;
    if (s === "claimed" && typeof r.claim_token === "string" && typeof r.event_id === "string") {
      return {
        status: "claimed" as const,
        claimToken: r.claim_token,
        billingEventId: r.event_id,
      };
    }
    if (s === "duplicate") return { status: "duplicate" as const };
    if (s === "busy") return { status: "busy" as const };
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

  async getBillingSubscription(
    provider: string,
    providerSubscriptionId: string,
  ): Promise<BillingSubscriptionState | null> {
    const r = await this.billingSubscription.get(provider, providerSubscriptionId);
    if (!r) return null;
    return this.rowToSubscriptionState(r);
  }

  async createBillingSubscriptionChange(
    input: BillingSubscriptionChangeInput,
  ): Promise<BillingSubscriptionChange> {
    const subscription = await this.billingSubscription.get(
      input.provider,
      input.providerSubscriptionId,
    );
    if (!subscription?.id) throw new Error("subscription change requires a persisted subscription");
    const rows = await this.queryFn(
      `SELECT * FROM bursar.open_subscription_change(
         $1::uuid, $2::uuid, $3::timestamptz, $4, $5, $6
       )`,
      [
        subscription.id,
        input.toOfferId,
        input.effectiveAt,
        input.effective,
        input.idempotencyKey,
        input.prorationBehavior ?? "provider_default",
      ],
    );
    const result = (rows[0] as Record<string, unknown> | undefined) ?? {};
    if (result.error_code) throw new Error(`subscription change: ${String(result.error_code)}`);
    const changeRows = await this.queryFn(
      `SELECT * FROM bursar.get_billing_subscription_change($1::bigint)`,
      [result.change_id],
    );
    if ((changeRows[0] as Record<string, unknown> | undefined)?.id == null) {
      throw new Error("subscription change creation returned no row");
    }
    return this.rowToSubscriptionChange(changeRows[0] as Record<string, unknown>);
  }

  async getOpenBillingSubscriptionChange(
    provider: string,
    providerSubscriptionId: string,
  ): Promise<BillingSubscriptionChange | null> {
    const rows = await this.queryFn(
      `SELECT * FROM bursar.get_open_billing_subscription_change($1, $2)`,
      [provider, providerSubscriptionId],
    );
    const row = rows[0] as Record<string, unknown> | undefined;
    return row?.id == null ? null : this.rowToSubscriptionChange(row);
  }

  async listExpiredGraceSubscriptions(
    now: string,
    limit = 100,
  ): Promise<BillingSubscriptionState[]> {
    const rows = await this.billingSubscription.listExpiredGraceSubscriptions(now, limit);
    return rows.map((row) => this.rowToSubscriptionState(row));
  }

  async markSubscriptionGraceExpired(
    subscriptionId: string,
    expectedGraceEndsAt: string,
    expiredAt = new Date().toISOString(),
  ): Promise<boolean> {
    return this.billingSubscription.markGraceExpired(
      subscriptionId,
      expectedGraceEndsAt,
      expiredAt,
    );
  }

  async updateBillingSubscriptionChange(
    id: string,
    update: BillingSubscriptionChangeUpdate,
  ): Promise<void> {
    if (!update.state) return;
    const rows = await this.queryFn(
      `SELECT bursar.advance_subscription_change($1::bigint, $2, $3, $4) AS advanced`,
      [id, update.state, update.providerOperationId ?? null, update.errorMessage ?? null],
    );
    if (!(rows[0] as Record<string, unknown> | undefined)?.advanced) {
      throw new Error(`subscription change transition rejected: ${id}`);
    }
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

  async recordSubscriptionConflict(input: BillingSubscriptionConflictCreate): Promise<void> {
    await this.billingSubscription.recordConflict(input);
  }

  async selectSubscriptionEntitlementSource(
    userId: string,
    provider: string,
    providerSubscriptionId?: string | null,
  ): Promise<boolean> {
    return this.billingSubscription.selectEntitlementSource(
      userId,
      provider,
      providerSubscriptionId,
    );
  }

  private rowToTopup(r: Record<string, unknown>): BillingTopupResult | null {
    if (!r?.topup_key || !r.id) return null;
    return {
      topupId: String(r.id),
      topupKey: r.topup_key as string,
      creditsPerUnit: Number(r.credits_per_unit ?? r.credits_per_major_unit ?? 1000),
      depositTo: (r.bucket_key as string | undefined) || "purchased",
      amountMinor: r.amount_minor == null ? undefined : Number(r.amount_minor),
      currency: r.currency == null ? undefined : String(r.currency),
      minQuantity: r.min_quantity == null ? undefined : Number(r.min_quantity),
      maxQuantity: r.max_quantity == null ? undefined : Number(r.max_quantity),
      defaultQuantity: r.default_quantity == null ? undefined : Number(r.default_quantity),
      minAmountMinor:
        r.amount_minor == null ? undefined : Number(r.amount_minor) * Number(r.min_quantity ?? 1),
      maxAmountMinor:
        r.amount_minor == null ? undefined : Number(r.amount_minor) * Number(r.max_quantity ?? 1),
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

  async resolveCreditTopupByLookup(
    provider: string,
    lookupKey: string,
  ): Promise<BillingTopupResult | null> {
    const r = await this.billingTopup.resolveByLookup(provider, lookupKey);
    return this.rowToTopup(r as Record<string, unknown>);
  }

  async computeTopupCredits(amountMinor: number, topupConfig: BillingTopupResult): Promise<number> {
    const unitAmount = topupConfig.amountMinor;
    const creditsPer = topupConfig.creditsPerUnit ?? 0;
    if (!unitAmount || amountMinor % unitAmount !== 0) return 0;
    const quantity = amountMinor / unitAmount;
    if (quantity < (topupConfig.minQuantity ?? 1) || quantity > (topupConfig.maxQuantity ?? 1)) {
      return 0;
    }
    return quantity * creditsPer;
  }

  async upsertBillingPayment(options: BillingPaymentUpsert): Promise<string> {
    return this.billingPayment.upsert(
      options.provider,
      options.providerPaymentId,
      options.providerInvoiceId ?? null,
      options.userId ?? null,
      options.amountMinor ?? 0,
      options.taxMinor ?? null,
      options.currency ?? "USD",
      options.purpose ?? null,
      options.metadata ? JSON.stringify(options.metadata) : null,
      options.status ?? "succeeded",
      options.providerUpdatedAt ?? new Date().toISOString(),
    );
  }

  async createBillingCreditGrant(input: BillingCreditGrantCreate): Promise<string> {
    const rows = await this.queryFn(
      "SELECT bursar.create_billing_credit_grant($1::uuid, $2::uuid, $3::uuid, $4, $5, $6::uuid) AS id",
      [
        input.paymentId ?? null,
        input.subscriptionId ?? null,
        input.topupId ?? null,
        input.configuredCredits,
        input.quantity ?? 1,
        input.billingEventId ?? null,
      ],
    );
    const id = (rows[0] as Record<string, unknown> | undefined)?.id;
    if (!id) throw new Error("billing credit grant creation returned no ID");
    return String(id);
  }

  async grantBillingCredit(
    grantId: string,
    idempotencyKey: string,
  ): Promise<Record<string, unknown>> {
    const rows = await this.queryFn("SELECT * FROM bursar.grant_billing_credit($1::uuid, $2)", [
      grantId,
      idempotencyKey,
    ]);
    return (rows[0] as Record<string, unknown> | undefined) ?? {};
  }

  async getBillingCreditGrantByPayment(paymentId: string): Promise<string | null> {
    const rows = await this.queryFn(
      `SELECT * FROM bursar.get_billing_credit_grant_by_payment($1::uuid)`,
      [paymentId],
    );
    const id = (rows[0] as Record<string, unknown> | undefined)?.id;
    return id ? String(id) : null;
  }

  async postBillingRefund(
    refundId: string,
    grantId: string,
    amountMinor: number,
    idempotencyKey: string,
  ): Promise<Record<string, unknown>> {
    const rows = await this.queryFn(
      "SELECT * FROM bursar.post_billing_refund($1::uuid, $2::uuid, $3, $4)",
      [refundId, grantId, amountMinor, idempotencyKey],
    );
    return (rows[0] as Record<string, unknown> | undefined) ?? {};
  }

  async upsertBillingRefund(options: BillingRefundUpsert): Promise<string> {
    return this.billingRefund.upsert(
      options.provider,
      options.providerRefundId,
      options.providerPaymentId ?? null,
      options.userId ?? null,
      options.amountMinor ?? 0,
      options.currency ?? "USD",
      options.reason ?? null,
      options.metadata ? JSON.stringify(options.metadata) : null,
      options.status ?? "pending",
      options.providerUpdatedAt ?? new Date().toISOString(),
    );
  }

  async upsertBillingInvoice(options: BillingInvoiceUpsert): Promise<void> {
    const subscription = options.providerSubscriptionId
      ? await this.billingSubscription.get(options.provider, options.providerSubscriptionId)
      : null;
    const subjectId =
      options.userId ?? (subscription?.subject_id ? String(subscription.subject_id) : null);
    if (!subjectId) throw new Error("invoice subject is required");
    await this.billingInvoice.upsert(
      subjectId,
      options.provider,
      options.providerInvoiceId,
      subscription?.id ? String(subscription.id) : null,
      options.status ?? null,
      options.amountDueMinor ?? null,
      options.amountPaidMinor ?? null,
      options.currency ?? "USD",
      options.periodStart ?? null,
      options.periodEnd ?? null,
      options.metadata ?? {},
      options.providerUpdatedAt ?? new Date().toISOString(),
    );
  }

  async listBillingInvoices(userId: string) {
    return this.billingInvoice.listForUser(userId);
  }

  async upsertBillingDispute(options: BillingDisputeUpsert): Promise<void> {
    const payment = options.providerPaymentId
      ? await this.billingPayment.getForRefund(options.provider, options.providerPaymentId)
      : null;
    if (!payment?.id) throw new Error("dispute payment is required");
    await this.billingDispute.upsert(
      options.provider,
      options.providerDisputeId,
      String(payment.id),
      options.status ?? "needs_response",
      options.reason ?? null,
      options.metadata ?? {},
      options.providerUpdatedAt ?? new Date().toISOString(),
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
      id: row.id,
      provider: row.provider,
      providerPaymentId: row.provider_payment_id,
      providerInvoiceId: row.provider_invoice_id,
      userId: row.subject_id,
      amountMinor: row.amount_minor == null ? null : Number(row.amount_minor),
      taxMinor: row.tax_minor == null ? null : Number(row.tax_minor),
      currency: row.currency,
      purpose: row.purpose,
      status: row.status,
      providerUpdatedAt: toIso(row.provider_updated_at),
      metadata: row.metadata,
    };
  }

  private rowToSubscriptionState(r: Record<string, unknown>): BillingSubscriptionState {
    return {
      subscriptionId: r.id ? String(r.id) : null,
      userId: String(r.user_id),
      provider: String(r.provider),
      providerSubscriptionId: String(r.provider_subscription_id),
      providerCustomerId: r.provider_customer_id ? String(r.provider_customer_id) : null,
      offerId: r.offer_id ? String(r.offer_id) : null,
      offerKey: r.offer_key ? String(r.offer_key) : null,
      plan: r.plan ? String(r.plan) : null,
      status: (r.status ? String(r.status) : "incomplete") as BillingSubscriptionStatus,
      currentPeriodStart: toIso(r.current_period_start),
      currentPeriodEnd: toIso(r.current_period_end),
      trialEnd: toIso(r.trial_end),
      cancelAt: toIso(r.cancel_at),
      endedAt: toIso(r.ended_at),
      graceEndsAt: toIso(r.grace_ends_at),
      graceExpiredAt: toIso(r.grace_expired_at),
      providerUpdatedAt: toIso(r.provider_updated_at),
      cancelAtPeriodEnd: Boolean(r.cancel_at_period_end),
      interval: r.interval ? String(r.interval) : null,
      intervalCount: r.interval_count ? Number(r.interval_count) : null,
      metadata:
        r.metadata && typeof r.metadata === "object"
          ? (r.metadata as Record<string, unknown>)
          : null,
    };
  }

  private async getSubscriptionOfferContexts(r: Record<string, unknown>): Promise<{
    fromOffer: BillingSubscriptionOfferContext;
    toOffer: BillingSubscriptionOfferContext;
  }> {
    const rows = await this.queryFn(
      `SELECT requested.side, requested.offer_id, context.*
       FROM (
         VALUES
           ('from', $1::uuid, $2::uuid),
           ('to', $3::uuid, $4::uuid)
       ) AS requested(side, offer_id, catalog_revision_id)
       CROSS JOIN LATERAL bursar.get_catalog_offer_context(
         requested.offer_id,
         requested.catalog_revision_id
       ) AS context`,
      [r.from_offer_id, r.from_catalog_revision_id, r.to_offer_id, r.to_catalog_revision_id],
    );
    const bySide = new Map(
      rows.map((row) => {
        const context = row as Record<string, unknown>;
        return [String(context.side), context] as const;
      }),
    );
    const mapContext = (side: "from" | "to"): BillingSubscriptionOfferContext => {
      const context = bySide.get(side);
      if (context?.offer_key == null) {
        throw new Error(`subscription change ${side}-offer context not found`);
      }
      return {
        offerId: String(context.offer_id),
        offerKey: String(context.offer_key),
        planId: context.plan_id ? String(context.plan_id) : null,
        plan: context.plan_key ? String(context.plan_key) : null,
        interval: context.billing_unit ? String(context.billing_unit) : null,
        intervalCount: context.billing_count == null ? null : Number(context.billing_count),
      };
    };
    return { fromOffer: mapContext("from"), toOffer: mapContext("to") };
  }

  private async rowToSubscriptionChange(
    r: Record<string, unknown>,
  ): Promise<BillingSubscriptionChange> {
    const { fromOffer, toOffer } = await this.getSubscriptionOfferContexts(r);
    return {
      id: String(r.id),
      subscriptionId: String(r.subscription_id),
      fromOfferId: String(r.from_offer_id),
      toOfferId: String(r.to_offer_id),
      fromOffer,
      toOffer,
      effectiveAt: toIso(r.effective_at),
      effective: String(r.effective_behavior) as BillingSubscriptionChange["effective"],
      state: String(r.state) as BillingSubscriptionChange["state"],
      prorationBehavior: String(
        r.proration_behavior,
      ) as BillingSubscriptionChange["prorationBehavior"],
      idempotencyKey: String(r.idempotency_key),
      providerOperationId: r.provider_operation_id ? String(r.provider_operation_id) : null,
      errorMessage: r.error_message ? String(r.error_message) : null,
    };
  }

  async getActiveBursarConfig(): Promise<Record<string, unknown> | null> {
    const rows = await this.queryFn("SELECT * FROM bursar.active_catalog_revision()", []);
    if (!rows || rows.length === 0) return null;
    return (
      ((rows[0] as Record<string, unknown>)?.source_document as Record<string, unknown> | null) ??
      null
    );
  }

  async pseudonymizeFinancialSubject(userId: string): Promise<boolean> {
    const rows = await this.queryFn(
      `SELECT bursar.pseudonymize_financial_subject($1::uuid) AS pseudonymized`,
      [userId],
    );
    return (rows[0] as Record<string, unknown> | undefined)?.pseudonymized === true;
  }

  async getBillingPreferences(userId: string): Promise<BillingPreferences | null> {
    const row = await this.billingPreferences.get(userId);
    if (!row || row.subject_id == null) return null;
    return {
      userId: String(row.subject_id),
      autoRecharge: Boolean(row.auto_recharge),
      overageProtection: Boolean(row.overage_protection),
      emailNotifications: Boolean(row.email_notifications),
      usageAlerts: Boolean(row.usage_alerts),
      invoiceReminders: Boolean(row.invoice_reminders),
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
    });
  }

  async getAutoRechargeProfile(userId: string): Promise<BillingAutoRechargeProfile | null> {
    return this.billingAutoRecharge.getProfile(userId);
  }

  async upsertAutoRechargeProfile(
    profile: BillingAutoRechargeProfile,
    options?: { resetCooldown?: boolean },
  ): Promise<void> {
    return this.billingAutoRecharge.upsertProfile(profile, options);
  }

  async claimAutoRechargeAttempt(
    input: AutoRechargeAttemptClaim,
  ): Promise<BillingAutoRechargeAttempt | null> {
    return this.billingAutoRecharge.claimAttempt(input);
  }

  async updateAutoRechargeAttempt(input: AutoRechargeAttemptUpdate): Promise<void> {
    return this.billingAutoRecharge.updateAttempt(input);
  }

  async updateAutoRechargeAttemptByProviderPayment(
    input: AutoRechargeProviderPaymentUpdate,
  ): Promise<void> {
    return this.billingAutoRecharge.updateAttemptByProviderPayment(input);
  }

  async countAutoRechargeAttempts(userId: string, since: string | Date): Promise<number> {
    return this.billingAutoRecharge.countAttempts(userId, since);
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
