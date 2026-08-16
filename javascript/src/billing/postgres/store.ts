import { Decimal } from "decimal.js";
import { z } from "zod";
import type { JsonObject, PostgresValue } from "../../shared/json.js";
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
  BillingSubscriptionState,
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
import {
  normalizeProviderEnvironment,
  type ProviderEnvironment,
} from "../../providers/environment.js";
import { StoreClosedError, StoreError } from "../../errors.js";
import { optionalBoundedDiagnosticMessage } from "../../shared/diagnostics.js";
import { requireStableKey } from "../../shared/idempotency.js";
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
import {
  BillingSubscriptionRepository,
  type SubscriptionRow,
} from "./repositories/subscription.js";
import { BillingEventRepository } from "./repositories/event.js";
import { BillingPaymentRepository } from "./repositories/payment.js";
import { BillingRefundRepository } from "./repositories/refund.js";
import { BillingInvoiceRepository } from "./repositories/invoice.js";
import { BillingDisputeRepository } from "./repositories/dispute.js";
import { BillingPreferencesRepository } from "./repositories/preferences.js";
import { BillingAutoRechargeRepository } from "./repositories/auto-recharge.js";
import { BillingSubscriptionChangeRepository } from "./repositories/subscription-change.js";

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
  .strict()
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
  .strict();

function billingCreditPostingResult(
  rows: readonly PostgresValue[] | null | undefined,
  context: string,
): BillingCreditPostingResult {
  const raw = requireRecordRow(rows, context);
  const row = safeParse(
    BillingCreditPostingRowSchema,
    {
      ledger_entry_id: raw.ledger_entry_id,
      balance_after: raw.balance_after,
      replayed: raw.replayed,
      error_code: raw.error_code,
    },
    context,
    { indeterminate: true },
  );
  return {
    ledgerEntryId: row.ledger_entry_id,
    balanceAfter: row.balance_after,
    replayed: row.replayed,
    errorCode: row.error_code,
  };
}

export interface PostgresBillingStoreOptions extends Omit<
  PostgresConnectionOptions,
  "providerEnvironment"
> {
  /** PostgreSQL connection string or an application-owned pool. */
  postgres: PostgresPool | string;
  /** Tenant UUID bound to every store transaction. */
  tenantId: string;
  /** Explicit financial namespace; billing stores never guess live vs test. */
  providerEnvironment: ProviderEnvironment;
  /** Injectable `pg.Pool` constructor for custom runtimes and tests. */
  poolConstructor?: PostgresPoolConstructor;
  billingPayloadBackend?: "postgres" | "s3";
}

export class PostgresBillingStore extends BillingStore {
  private readonly postgres: PostgresClient;
  readonly providerEnvironment: ProviderEnvironment;

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
  private _subscriptionChange: BillingSubscriptionChangeRepository | null = null;
  constructor(options: PostgresBillingStoreOptions) {
    super();
    if (!z.object({}).safeParse(options).success) {
      throw new TypeError("PostgresBillingStore options are required");
    }
    if (!z.string().safeParse(options.postgres).success && options.poolConstructor !== undefined) {
      throw new TypeError("poolConstructor cannot be used with an existing PostgreSQL pool");
    }
    const providerEnvironment = normalizeProviderEnvironment(options.providerEnvironment);
    this.providerEnvironment = providerEnvironment;
    this.postgres = new PostgresClient(options.postgres, {
      tenantId: options.tenantId,
      providerEnvironment,
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

  private get billingSubscriptionChange(): BillingSubscriptionChangeRepository {
    if (!this._subscriptionChange) {
      this._subscriptionChange = new BillingSubscriptionChangeRepository(this.queryFn);
    }
    return this._subscriptionChange;
  }

  async createOrGetCheckoutIntent(input: CheckoutIntentCreate): Promise<CheckoutIntent> {
    const operationKey = requireStableKey(input.operationKey, "operationKey");
    if (!/^[0-9a-fA-F]{64}$/.test(input.requestDigest)) {
      throw new TypeError("requestDigest must be a 32-byte hex string");
    }
    const rows = await this.queryFn(
      `SELECT bursar.create_checkout_intent(
         $1::uuid, $2, $3, $4, $5, decode($6, 'hex'), $7::timestamptz
       ) AS id`,
      [
        input.subjectId,
        input.provider,
        operationKey,
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
    const row = rows[0];
    if (!row) throw new StoreError("getCheckoutIntent: expected one row");
    return this.rowToCheckoutIntent(
      safeParse(
        CheckoutIntentRowSchema,
        {
          id: row.id,
          subject_id: row.subject_id,
          provider: row.provider,
          checkout_kind: row.checkout_kind,
          product_key: row.product_key,
          request_digest: row.request_digest,
          status: row.status,
          provider_session_id: row.provider_session_id,
          checkout_url: row.checkout_url,
          expires_at: row.expires_at,
        },
        "PostgresBillingStore.getCheckoutIntent",
      ),
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
    envelope?: JsonObject,
  ): Promise<BillingEventClaim> {
    const result = await this.billingEvent.claim(
      provider,
      eventId,
      eventType,
      JSON.stringify(envelope ?? { eventType }),
    );
    if (result.status === "claimed") {
      if (result.claim_token === null || result.event_id === null) {
        throw new StoreError("Billing event claim returned no claim identifiers", {
          details: { provider, eventId },
        });
      }
      return {
        status: "claimed" as const,
        claimToken: result.claim_token,
        billingEventId: result.event_id,
      };
    }
    if (result.status === "duplicate") return { status: "duplicate" as const };
    if (result.status === "busy") return { status: "busy" as const };
    if (result.status === "invalid_request") return { status: "invalid_request" as const };
    if (result.status === "idempotency_conflict" || result.status === "max_retries_exceeded") {
      if (result.event_id === null) {
        throw new StoreError("Billing event terminal claim returned no event identifier", {
          details: { provider, eventId, status: result.status },
        });
      }
      return { status: result.status, billingEventId: result.event_id };
    }
    throw new StoreError("Billing event claim returned an unsupported status", {
      details: { provider, eventId, status: result.status },
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
    return this.billingSubscriptionChange.create(subscription.id, input);
  }

  async getOpenBillingSubscriptionChange(
    provider: string,
    providerSubscriptionId: string,
  ): Promise<BillingSubscriptionChange | null> {
    return this.billingSubscriptionChange.getOpen(provider, providerSubscriptionId);
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
    await this.billingSubscriptionChange.update(id, update);
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
      providerUpdatedAt:
        row.provider_updated_at instanceof Date
          ? row.provider_updated_at.toISOString()
          : row.provider_updated_at,
      metadata: row.metadata,
    };
  }

  private rowToSubscriptionState(r: SubscriptionRow): BillingSubscriptionState {
    return {
      subscriptionId: r.id,
      userId: r.user_id,
      provider: r.provider,
      providerSubscriptionId: r.provider_subscription_id,
      providerCustomerId: r.provider_customer_id,
      offerId: r.offer_id,
      offerKey: r.offer_key,
      planId: r.plan_id,
      plan: r.plan,
      status: r.status,
      currentPeriodStart: r.current_period_start,
      currentPeriodEnd: r.current_period_end,
      trialEnd: r.trial_end,
      cancelAt: r.cancel_at,
      endedAt: r.ended_at,
      graceEndsAt: r.grace_ends_at,
      graceExpiredAt: r.grace_expired_at,
      providerUpdatedAt: r.provider_updated_at,
      cancelAtPeriodEnd: r.cancel_at_period_end,
      interval: r.interval,
      intervalCount: r.interval_count,
      metadata: r.metadata,
    };
  }

  async getActiveCatalogDocument(): Promise<JsonObject | null> {
    const rows = await this.queryFn("SELECT * FROM bursar.active_catalog_revision()", []);
    const row = optionalRecordRow(rows, "PostgresBillingStore.getActiveCatalogDocument");
    if (row === null) return null;
    const sourceDocument = row.source_document;
    if (sourceDocument === undefined) {
      throw new StoreError("active catalog revision returned no source document");
    }
    return safeParse(
      z.record(z.string(), z.json()),
      sourceDocument,
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
