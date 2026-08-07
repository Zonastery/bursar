import Decimal from "decimal.js";
import { z } from "zod";
import type {
  BillingAutoRechargeAttempt,
  BillingAutoRechargeProfile,
  BillingCreditPostingResult,
  BillingCustomerRecord,
  BillingEventClaim,
  BillingOfferResult,
  BillingPaymentRecord,
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
import {
  PostgresClient,
  type PostgresConnectionOptions,
  type PostgresPool,
  type PostgresPoolConstructor,
} from "../../shared/postgres-client.js";
import { StoreClosedError, StoreError } from "../../errors.js";
import { optionalBoundedDiagnosticMessage } from "../../shared/diagnostics.js";
import {
  optionalRecordRow,
  pgBoolean,
  postgresUuid,
  requireRecordRow,
  requireResultField,
  safeParse,
} from "../../shared/postgres-validation.js";
import { BillingOfferRepository, type BillingOfferRow } from "./repositories/offer.js";
import { BillingTopupRepository, type BillingTopupRow } from "./repositories/topup.js";
import { BillingCustomerRepository } from "./repositories/customer.js";
import { BillingSubscriptionRepository } from "./repositories/subscription.js";
import { BillingEventRepository } from "./repositories/event.js";
import { BillingPaymentRepository } from "./repositories/payment.js";
import { BillingRefundRepository } from "./repositories/refund.js";
import { BillingInvoiceRepository } from "./repositories/invoice.js";
import { BillingDisputeRepository } from "./repositories/dispute.js";
import { BillingPreferencesRepository } from "./repositories/preferences.js";
import { BillingAutoRechargeRepository } from "./repositories/auto-recharge.js";

const PgSafeMinorUnitsSchema = z
  .union([z.string().regex(/^\d+$/), z.number().int().nonnegative().safe()])
  .transform(Number)
  .refine(Number.isSafeInteger, "minor-unit amount exceeds JavaScript's safe integer range");

const BillingCreditPostingRowSchema = z
  .object({
    ledger_entry_id: postgresUuid.nullable(),
    balance_after: z
      .union([z.string(), z.number(), z.instanceof(Decimal)])
      .nullable()
      .transform((value) => (value === null ? null : new Decimal(value))),
    replayed: pgBoolean,
    error_code: z.string().min(1).nullable(),
  })
  .passthrough()
  .superRefine((row, ctx) => {
    if (row.error_code === null && (row.ledger_entry_id === null || row.balance_after === null)) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: "successful posting requires ledger_entry_id and balance_after",
      });
    }
  });

const CheckoutIntentRowSchema = z
  .object({
    id: postgresUuid,
    subject_id: postgresUuid,
    provider: z.string().min(1),
    checkout_kind: z.enum(["subscription", "credit_topup"]),
    product_key: z.string().min(1),
    request_digest: z.instanceof(Uint8Array).refine((value) => value.byteLength === 32),
    status: z.enum(["open", "completed", "failed", "expired"]),
    provider_session_id: z.string().min(1).nullable(),
    checkout_url: z.string().min(1).nullable(),
    expires_at: z.union([z.string().datetime({ offset: true }), z.date()]),
  })
  .passthrough();

function billingCreditPostingResult(
  rows: readonly unknown[] | null | undefined,
  context: string,
): BillingCreditPostingResult {
  const row = safeParse(BillingCreditPostingRowSchema, requireRecordRow(rows, context), context, {
    indeterminate: true,
  });
  return {
    ledgerEntryId: row.ledger_entry_id,
    balanceAfter: row.balance_after,
    replayed: row.replayed,
    errorCode: row.error_code,
  };
}

function toIso(value: unknown): string | null {
  if (!value) return null;
  if (typeof value === "string") return value;
  if (value instanceof Date) return value.toISOString();
  if (typeof (value as Record<string, unknown>)["toISOString"] === "function") {
    return (value as Date).toISOString();
  }
  return String(value);
}

export interface PostgresBillingStoreOptions extends PostgresConnectionOptions {
  /** PostgreSQL connection string or an application-owned pool. */
  postgres: PostgresPool | string;
  /** Tenant UUID bound to every store transaction. */
  tenantId: string;
  /** Injectable `pg.Pool` constructor for custom runtimes and tests. */
  poolConstructor?: PostgresPoolConstructor;
  billingPayloadBackend?: "postgres" | "s3";
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
  constructor(options: PostgresBillingStoreOptions) {
    super();
    if (typeof options !== "object" || options === null) {
      throw new TypeError("PostgresBillingStore options are required");
    }
    if (typeof options.postgres !== "string" && options.poolConstructor !== undefined) {
      throw new TypeError("poolConstructor cannot be used with an existing PostgreSQL pool");
    }
    this.postgres = new PostgresClient(options.postgres, {
      tenantId: options.tenantId,
      poolConstructor: options.poolConstructor,
      billingPayloadBackend: options.billingPayloadBackend,
      connectionTimeoutMs: options.connectionTimeoutMs,
      statementTimeoutMs: options.statementTimeoutMs,
      idleTransactionTimeoutMs: options.idleTransactionTimeoutMs,
      idleTimeoutMs: options.idleTimeoutMs,
      maxConnections: options.maxConnections,
      applicationName: options.applicationName,
      onPoolError: options.onPoolError,
      closedError: () => new StoreClosedError("Billing store has been closed"),
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
      throw new TypeError("requestDigest must be a 32-byte hex string");
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
    const id = requireResultField(
      rows,
      "id",
      postgresUuid,
      "PostgresBillingStore.createOrGetCheckoutIntent",
    );
    const intent = await this.getCheckoutIntent(id, input.subjectId);
    if (!intent) {
      throw new StoreError("checkout intent could not be read after creation", {
        indeterminate: true,
        details: { checkoutIntentId: id },
      });
    }
    return intent;
  }

  async updateCheckoutIntent(id: string, update: CheckoutIntentUpdate): Promise<void> {
    const rows = await this.queryFn(
      `SELECT bursar.advance_checkout_intent($1::uuid, $2, $3, $4) AS advanced`,
      [id, update.status ?? null, update.providerSessionId ?? null, update.checkoutUrl ?? null],
    );
    if (
      !requireResultField(rows, "advanced", pgBoolean, "PostgresBillingStore.updateCheckoutIntent")
    ) {
      throw new StoreError(`checkout intent transition rejected: ${id}`, {
        details: { checkoutIntentId: id },
      });
    }
  }

  async getCheckoutIntent(id: string, subjectId: string): Promise<CheckoutIntent | null> {
    const rows = await this.queryFn(
      `SELECT * FROM bursar.get_checkout_intent($1::uuid, $2::uuid)`,
      [id, subjectId],
    );
    if (rows.length === 0) return null;
    if (rows.length !== 1) {
      throw new StoreError("getCheckoutIntent: expected at most one row", {
        details: { rowCount: rows.length, checkoutIntentId: id },
      });
    }
    return this.rowToCheckoutIntent(
      safeParse(CheckoutIntentRowSchema, rows[0], "PostgresBillingStore.getCheckoutIntent"),
    );
  }

  private rowToCheckoutIntent(row: z.infer<typeof CheckoutIntentRowSchema>): CheckoutIntent {
    return {
      id: row.id,
      subjectId: row.subject_id,
      provider: row.provider,
      checkoutKind: row.checkout_kind,
      productKey: row.product_key,
      requestDigest: Buffer.from(row.request_digest).toString("hex"),
      status: row.status,
      providerSessionId: row.provider_session_id,
      checkoutUrl: row.checkout_url,
      expiresAt: row.expires_at instanceof Date ? row.expires_at.toISOString() : row.expires_at,
    };
  }

  private rowToOffer(row: BillingOfferRow | null): BillingOfferResult | null {
    if (!row) return null;
    return {
      offerId: row.id,
      offerKey: row.offerKey,
      planId: row.planId,
      plan: row.plan,
      interval: row.interval,
      intervalCount: row.intervalCount,
      grant:
        row.grantCredits === null
          ? null
          : {
              mode: "cycle_grant",
              credits: row.grantCredits,
              bucket: row.grantBucket,
              replacePrior: row.grantReplacePrior,
            },
    };
  }

  async resolveBillingOffer(
    provider: string,
    productId?: string | null,
    priceId?: string | null,
  ): Promise<BillingOfferResult | null> {
    const r = await this.billingOffer.resolveByPrice(provider, priceId ?? null, productId ?? null);
    return this.rowToOffer(r);
  }

  async resolveBillingOfferByLookup(
    provider: string,
    lookupKey: string,
  ): Promise<BillingOfferResult | null> {
    const r = await this.billingOffer.resolveByLookup(provider, lookupKey);
    return this.rowToOffer(r);
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
    const r = result as Record<string, unknown>;
    const s = r.status as string;
    if (s === "claimed") {
      if (typeof r.claim_token !== "string" || typeof r.event_id !== "string") {
        throw new StoreError("Billing event claim returned no claim identifiers", {
          details: { provider, eventId },
        });
      }
      return {
        status: "claimed" as const,
        claimToken: r.claim_token,
        billingEventId: r.event_id,
      };
    }
    if (s === "duplicate") return { status: "duplicate" as const };
    if (s === "busy") return { status: "busy" as const };
    if (s === "invalid_request" || s === "idempotency_conflict" || s === "max_retries_exceeded") {
      return { status: "retry" as const };
    }
    throw new StoreError("Billing event claim returned an unsupported status", {
      details: { provider, eventId, status: s },
    });
  }

  async completeBillingEvent(
    provider: string,
    eventId: string,
    claimToken: string,
  ): Promise<boolean> {
    return this.billingEvent.complete(provider, eventId, claimToken);
  }

  async failBillingEvent(
    provider: string,
    eventId: string,
    claimToken: string,
    error?: string,
  ): Promise<boolean> {
    return this.billingEvent.fail(
      provider,
      eventId,
      claimToken,
      optionalBoundedDiagnosticMessage(error) ?? undefined,
    );
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
    await this.billingSubscription.upsert(state);
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
    if (!subscription?.id) {
      throw new StoreError("subscription change requires a persisted subscription", {
        retryable: true,
        details: {
          provider: input.provider,
          providerSubscriptionId: input.providerSubscriptionId,
        },
      });
    }
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
    const result = requireRecordRow(rows, "PostgresBillingStore.createBillingSubscriptionChange");
    if (result.error_code) {
      throw new StoreError(`subscription change: ${String(result.error_code)}`, {
        details: { errorCode: String(result.error_code) },
      });
    }
    const changeId = safeParse(
      z.union([z.string().min(1), z.number().int()]).transform(String),
      result.change_id,
      "PostgresBillingStore.createBillingSubscriptionChange.change_id",
      { indeterminate: true },
    );
    const changeRows = await this.queryFn(
      `SELECT * FROM bursar.get_billing_subscription_change($1::bigint)`,
      [changeId],
    );
    if ((changeRows[0] as Record<string, unknown> | undefined)?.id == null) {
      throw new StoreError("subscription change creation returned no row", {
        indeterminate: true,
        details: { changeId },
      });
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
      [
        id,
        update.state,
        update.providerOperationId ?? null,
        optionalBoundedDiagnosticMessage(update.errorMessage),
      ],
    );
    if (
      !requireResultField(
        rows,
        "advanced",
        pgBoolean,
        "PostgresBillingStore.updateBillingSubscriptionChange",
      )
    ) {
      throw new StoreError(`subscription change transition rejected: ${id}`, {
        details: { subscriptionChangeId: id },
      });
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

  private rowToTopup(r: BillingTopupRow | null): BillingTopupResult | null {
    if (!r) return null;
    const minAmountMinor = safeParse(
      PgSafeMinorUnitsSchema,
      r.amount_minor * r.min_quantity,
      "PostgresBillingStore.rowToTopup.minAmountMinor",
    );
    const maxAmountMinor = safeParse(
      PgSafeMinorUnitsSchema,
      r.amount_minor * r.max_quantity,
      "PostgresBillingStore.rowToTopup.maxAmountMinor",
    );
    return {
      topupId: r.id,
      topupKey: r.topup_key,
      creditsPerUnit: r.credits_per_unit,
      depositTo: r.bucket_key,
      amountMinor: r.amount_minor,
      currency: r.currency,
      minQuantity: r.min_quantity,
      maxQuantity: r.max_quantity,
      defaultQuantity: r.default_quantity,
      minAmountMinor,
      maxAmountMinor,
    };
  }

  async resolveCreditTopup(
    provider: string,
    productId?: string | null,
    priceId?: string | null,
  ): Promise<BillingTopupResult | null> {
    const r = await this.billingTopup.resolveByPrice(provider, priceId ?? null, productId ?? null);
    return this.rowToTopup(r);
  }

  async resolveCreditTopupByLookup(
    provider: string,
    lookupKey: string,
  ): Promise<BillingTopupResult | null> {
    const r = await this.billingTopup.resolveByLookup(provider, lookupKey);
    return this.rowToTopup(r);
  }

  async computeTopupCredits(
    amountMinor: number,
    topupConfig: BillingTopupResult,
  ): Promise<Decimal> {
    if (!Number.isSafeInteger(amountMinor) || amountMinor < 0) {
      throw new TypeError("amountMinor must be a non-negative safe integer");
    }
    const unitAmount = topupConfig.amountMinor;
    if (unitAmount <= 0 || amountMinor % unitAmount !== 0) return new Decimal(0);
    const quantity = amountMinor / unitAmount;
    if (quantity < topupConfig.minQuantity || quantity > topupConfig.maxQuantity) {
      return new Decimal(0);
    }
    return topupConfig.creditsPerUnit.mul(quantity);
  }

  async upsertBillingPayment(options: BillingPaymentUpsert): Promise<string> {
    return this.billingPayment.upsert(
      options.provider,
      options.providerPaymentId,
      options.providerInvoiceId ?? null,
      options.userId,
      options.amountMinor,
      options.taxMinor,
      options.currency,
      options.purpose,
      options.metadata ? JSON.stringify(options.metadata) : null,
      options.status,
      options.providerUpdatedAt,
    );
  }

  async createBillingCreditGrant(input: BillingCreditGrantCreate): Promise<string> {
    const rows = await this.queryFn(
      "SELECT bursar.create_billing_credit_grant($1::uuid, $2::uuid, $3::uuid, $4, $5, $6::uuid) AS id",
      [
        input.paymentId ?? null,
        input.subscriptionId ?? null,
        input.topupId ?? null,
        input.configuredCredits.toString(),
        input.quantity,
        input.billingEventId ?? null,
      ],
    );
    return requireResultField(
      rows,
      "id",
      postgresUuid,
      "PostgresBillingStore.createBillingCreditGrant",
    );
  }

  async grantBillingCredit(
    grantId: string,
    idempotencyKey: string,
  ): Promise<BillingCreditPostingResult> {
    const rows = await this.queryFn("SELECT * FROM bursar.grant_billing_credit($1::uuid, $2)", [
      grantId,
      idempotencyKey,
    ]);
    return billingCreditPostingResult(rows, "PostgresBillingStore.grantBillingCredit");
  }

  async getBillingCreditGrantByPayment(paymentId: string): Promise<string | null> {
    const rows = await this.queryFn(
      `SELECT * FROM bursar.get_billing_credit_grant_by_payment($1::uuid)`,
      [paymentId],
    );
    const row = optionalRecordRow(rows, "PostgresBillingStore.getBillingCreditGrantByPayment");
    return row === null
      ? null
      : safeParse(postgresUuid, row.id, "PostgresBillingStore.getBillingCreditGrantByPayment.id");
  }

  async postBillingRefund(
    refundId: string,
    grantId: string,
    amountMinor: number,
    idempotencyKey: string,
  ): Promise<BillingCreditPostingResult> {
    const rows = await this.queryFn(
      "SELECT * FROM bursar.post_billing_refund($1::uuid, $2::uuid, $3, $4)",
      [refundId, grantId, amountMinor, idempotencyKey],
    );
    return billingCreditPostingResult(rows, "PostgresBillingStore.postBillingRefund");
  }

  async upsertBillingRefund(options: BillingRefundUpsert): Promise<string> {
    return this.billingRefund.upsert(
      options.provider,
      options.providerRefundId,
      options.providerPaymentId,
      options.userId,
      options.amountMinor,
      options.currency,
      options.reason ?? null,
      options.metadata ? JSON.stringify(options.metadata) : null,
      options.status,
      options.providerUpdatedAt,
    );
  }

  async upsertBillingInvoice(options: BillingInvoiceUpsert): Promise<void> {
    const subscription = options.providerSubscriptionId
      ? await this.billingSubscription.get(options.provider, options.providerSubscriptionId)
      : null;
    if (options.providerSubscriptionId && !subscription) {
      throw new StoreError("invoice subscription is not available", {
        retryable: true,
        details: {
          provider: options.provider,
          providerInvoiceId: options.providerInvoiceId,
          providerSubscriptionId: options.providerSubscriptionId ?? null,
        },
      });
    }
    await this.billingInvoice.upsert(
      options.userId,
      options.provider,
      options.providerInvoiceId,
      subscription?.id ? String(subscription.id) : null,
      options.status,
      options.amountDueMinor,
      options.amountPaidMinor,
      options.currency,
      options.periodStart ?? null,
      options.periodEnd ?? null,
      options.metadata ?? {},
      options.providerUpdatedAt,
    );
  }

  async listBillingInvoices(userId: string) {
    return this.billingInvoice.listForUser(userId);
  }

  async upsertBillingDispute(options: BillingDisputeUpsert): Promise<void> {
    const payment = await this.billingPayment.getForRefund(
      options.provider,
      options.providerPaymentId,
    );
    if (!payment?.id) {
      throw new StoreError("dispute payment is required", {
        retryable: true,
        details: {
          provider: options.provider,
          providerDisputeId: options.providerDisputeId,
          providerPaymentId: options.providerPaymentId ?? null,
        },
      });
    }
    await this.billingDispute.upsert(
      options.provider,
      options.providerDisputeId,
      String(payment.id),
      options.status,
      options.reason ?? null,
      options.metadata ?? {},
      options.providerUpdatedAt,
    );
  }

  async getBillingPayment(
    provider: string,
    providerPaymentId: string,
  ): Promise<BillingPaymentRecord | null> {
    const row = await this.billingPayment.getForRefund(provider, providerPaymentId);
    if (!row) return null;
    const providerUpdatedAt = toIso(row.provider_updated_at);
    if (!providerUpdatedAt) {
      throw new StoreError("billing payment has no provider update timestamp", {
        details: { provider, providerPaymentId },
      });
    }
    return {
      id: row.id,
      provider: row.provider,
      providerPaymentId: row.provider_payment_id,
      providerInvoiceId: row.provider_invoice_id,
      userId: row.subject_id,
      amountMinor: safeParse(
        PgSafeMinorUnitsSchema,
        row.amount_minor,
        "PostgresBillingStore.getBillingPayment.amountMinor",
      ),
      taxMinor: safeParse(
        PgSafeMinorUnitsSchema,
        row.tax_minor,
        "PostgresBillingStore.getBillingPayment.taxMinor",
      ),
      currency: row.currency,
      purpose: row.purpose,
      status: row.status,
      providerUpdatedAt,
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
        throw new StoreError(`subscription change ${side}-offer context not found`, {
          details: { subscriptionChangeId: r.id, side },
        });
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

  async getActiveCatalogDocument(): Promise<Record<string, unknown> | null> {
    const rows = await this.queryFn("SELECT * FROM bursar.active_catalog_revision()", []);
    if (rows.length === 0) return null;
    if (rows.length !== 1 || typeof rows[0] !== "object" || rows[0] === null) {
      throw new StoreError("active catalog revision returned a malformed row", {
        details: { rowCount: rows.length },
      });
    }
    return safeParse(
      z.record(z.string(), z.unknown()),
      (rows[0] as Record<string, unknown>).source_document,
      "PostgresBillingStore.getActiveCatalogDocument",
    );
  }

  async pseudonymizeFinancialSubject(userId: string): Promise<boolean> {
    const rows = await this.queryFn(
      `SELECT bursar.pseudonymize_financial_subject($1::uuid) AS pseudonymized`,
      [userId],
    );
    return requireResultField(
      rows,
      "pseudonymized",
      pgBoolean,
      "PostgresBillingStore.pseudonymizeFinancialSubject",
    );
  }

  async getBillingPreferences(userId: string): Promise<BillingPreferences | null> {
    return this.billingPreferences.get(userId);
  }

  async upsertBillingPreferences(prefs: BillingPreferences): Promise<void> {
    await this.billingPreferences.upsert(prefs);
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
