import { z } from "zod";
import type {
  BillingSubscriptionState,
  BillingSubscriptionStatus,
  SubscriptionEntitlementOutcome,
} from "../../types/subscriptions.js";
import type { QueryFn } from "../../../shared/postgres-types.js";
import type { JsonObject, PostgresRow } from "../../../shared/json.js";
import { StoreError } from "../../../errors.js";
import {
  optionalRecordRow,
  pgBoolean,
  postgresUuid,
  requireResultField,
  safeParse,
} from "../../../shared/postgres-validation.js";

const timestamp = z
  .union([
    z.date().refine((value) => !Number.isNaN(value.getTime())),
    z.string().datetime({ offset: true }),
  ])
  .transform((value) => (value instanceof Date ? value : new Date(value)).toISOString());
const SubscriptionStatusSchema = z.enum([
  "incomplete",
  "incomplete_expired",
  "trialing",
  "active",
  "past_due",
  "canceled",
  "unpaid",
  "paused",
  "expired",
]);
const SubscriptionEntitlementOutcomeSchema = z.enum(["applied", "revoked", "preserved", "stale"]);
const PersistedSubscriptionRowSchema = z
  .object({
    id: postgresUuid,
    subject_id: postgresUuid,
    provider: z.string().min(1),
    provider_subscription_id: z.string().min(1),
    provider_customer_id: z.string().min(1).nullable(),
    offer_id: postgresUuid,
    catalog_revision_id: postgresUuid,
    status: SubscriptionStatusSchema,
    current_period_start: timestamp.nullable(),
    current_period_end: timestamp.nullable(),
    trial_end: timestamp.nullable(),
    cancel_at: timestamp.nullable(),
    ended_at: timestamp.nullable(),
    grace_ends_at: timestamp.nullable(),
    grace_expired_at: timestamp.nullable(),
    provider_updated_at: timestamp,
    cancel_at_period_end: pgBoolean,
    metadata: z.record(z.string(), z.json()),
  })
  .strict();
const CatalogReferenceSchema = z
  .object({
    offer_id: postgresUuid,
    catalog_revision_id: postgresUuid,
  })
  .strict();
const OfferContextSchema = z
  .object({
    offer_key: z.string().min(1),
    plan_id: postgresUuid,
    plan_key: z.string().min(1),
    billing_unit: z.enum(["day", "week", "month", "year"]),
    billing_count: z.number().int().positive(),
  })
  .strict();

const SubscriptionRowSchema = z
  .object({
    id: postgresUuid,
    user_id: postgresUuid,
    provider: z.string().min(1),
    provider_subscription_id: z.string().min(1),
    provider_customer_id: z.string().min(1).nullable(),
    offer_id: postgresUuid,
    offer_key: z.string().min(1),
    plan_id: postgresUuid,
    plan: z.string().min(1),
    status: SubscriptionStatusSchema,
    current_period_start: timestamp.nullable(),
    current_period_end: timestamp.nullable(),
    trial_end: timestamp.nullable(),
    cancel_at: timestamp.nullable(),
    ended_at: timestamp.nullable(),
    grace_ends_at: timestamp.nullable(),
    grace_expired_at: timestamp.nullable(),
    provider_updated_at: timestamp,
    cancel_at_period_end: pgBoolean,
    interval: z.enum(["day", "week", "month", "year"]),
    interval_count: z.number().int().positive(),
    metadata: z.record(z.string(), z.json()),
  })
  .strict();

export type SubscriptionRow = z.infer<typeof SubscriptionRowSchema>;
type PersistedSubscriptionRow = z.infer<typeof PersistedSubscriptionRowSchema>;
type EnrichedSubscriptionRow = PersistedSubscriptionRow & {
  offer_key: string;
  plan_id: string;
  plan_key: string;
  billing_unit: "day" | "week" | "month" | "year";
  billing_count: number;
  plan: string;
  interval: "day" | "week" | "month" | "year";
  interval_count: number;
};

function persistedSubscription(row: PostgresRow, context: string): PersistedSubscriptionRow {
  const value = row;
  return safeParse(
    PersistedSubscriptionRowSchema,
    {
      id: value.id,
      subject_id: value.subject_id,
      provider: value.provider,
      provider_subscription_id: value.provider_subscription_id,
      provider_customer_id: value.provider_customer_id,
      offer_id: value.offer_id,
      catalog_revision_id: value.catalog_revision_id,
      status: value.status,
      current_period_start: value.current_period_start,
      current_period_end: value.current_period_end,
      trial_end: value.trial_end,
      cancel_at: value.cancel_at,
      ended_at: value.ended_at,
      grace_ends_at: value.grace_ends_at,
      grace_expired_at: value.grace_expired_at,
      provider_updated_at: value.provider_updated_at,
      cancel_at_period_end: value.cancel_at_period_end,
      metadata: value.metadata,
    },
    context,
  );
}

export class BillingSubscriptionRepository {
  constructor(private query: QueryFn) {}

  async upsert(state: BillingSubscriptionState): Promise<void> {
    const provider = state.provider;
    const providerSubscriptionId = state.providerSubscriptionId;
    const userId = state.userId;
    let providerCustomerId = state.providerCustomerId ?? null;
    let offerId = state.offerId ?? null;

    if (!provider || !providerSubscriptionId) {
      throw new TypeError(
        "subscription.upsert: subject, provider, subscription, and offer are required",
      );
    }

    const existing = await this.query(
      `SELECT * FROM bursar.get_billing_subscription_by_provider($1, $2)`,
      [provider, providerSubscriptionId],
    );
    const rawExisting = optionalRecordRow(
      existing,
      "BillingSubscriptionRepository.upsert.existing",
    );
    const existingRow =
      rawExisting === null
        ? null
        : persistedSubscription(rawExisting, "BillingSubscriptionRepository.upsert.existing");
    if (providerCustomerId === null && existingRow !== null) {
      providerCustomerId = existingRow.provider_customer_id;
    }
    if (offerId === null && existingRow !== null) offerId = existingRow.offer_id;

    const offerKey = state.offerKey ?? null;
    if (offerId === null && offerKey !== null) {
      const offerRows = await this.query(`SELECT * FROM bursar.resolve_active_catalog_offer($1)`, [
        offerKey,
      ]);
      const offerRow = optionalRecordRow(offerRows, "BillingSubscriptionRepository.upsert.offer");
      if (offerRow !== null) {
        offerId = safeParse(
          postgresUuid,
          offerRow.id,
          "BillingSubscriptionRepository.upsert.offer.id",
        );
      }
    }

    if (!userId || !offerId) {
      throw new TypeError(
        "subscription.upsert: subject, provider, subscription, and offer are required",
      );
    }
    const providerUpdatedAt = safeParse(
      timestamp,
      state.providerUpdatedAt,
      "BillingSubscriptionRepository.upsert.providerUpdatedAt",
    );

    const rows = await this.query(
      "SELECT bursar.upsert_billing_subscription($1::uuid,$2,$3,$4,$5::uuid,$6::bursar.billing_subscription_status,$7,$8,$9,$10::jsonb,$11,$12,$13,$14,$15) AS id",
      [
        userId,
        provider,
        providerSubscriptionId,
        providerCustomerId,
        offerId,
        state.status,
        state.currentPeriodStart ?? null,
        state.currentPeriodEnd ?? null,
        state.cancelAtPeriodEnd,
        JSON.stringify(state.metadata ?? {}),
        state.trialEnd ?? null,
        state.cancelAt ?? null,
        state.endedAt ?? null,
        providerUpdatedAt,
        state.graceEndsAt ?? null,
      ],
    );
    requireResultField(rows, "id", postgresUuid, "BillingSubscriptionRepository.upsert");
  }

  private map(row: EnrichedSubscriptionRow): SubscriptionRow {
    const r = row;
    return safeParse(
      SubscriptionRowSchema,
      {
        id: r.id,
        user_id: r.subject_id,
        provider: r.provider,
        provider_subscription_id: r.provider_subscription_id,
        provider_customer_id: r.provider_customer_id,
        offer_id: r.offer_id,
        offer_key: r.offer_key,
        plan_id: r.plan_id,
        plan: r.plan,
        status: r.status,
        current_period_start: r.current_period_start,
        current_period_end: r.current_period_end,
        trial_end: r.trial_end,
        cancel_at: r.cancel_at,
        ended_at: r.ended_at,
        grace_ends_at: r.grace_ends_at,
        grace_expired_at: r.grace_expired_at,
        provider_updated_at: r.provider_updated_at,
        cancel_at_period_end: r.cancel_at_period_end,
        interval: r.interval,
        interval_count: r.interval_count,
        metadata: r.metadata,
      },
      "BillingSubscriptionRepository",
    );
  }

  private async withOfferContext(
    value: PersistedSubscriptionRow,
  ): Promise<EnrichedSubscriptionRow> {
    const reference = safeParse(
      CatalogReferenceSchema,
      { offer_id: value.offer_id, catalog_revision_id: value.catalog_revision_id },
      "BillingSubscriptionRepository.withOfferContext.reference",
    );
    const contextRows = await this.query(
      `SELECT * FROM bursar.get_catalog_offer_context($1::uuid, $2::uuid)`,
      [reference.offer_id, reference.catalog_revision_id],
    );
    const rawContext = optionalRecordRow(
      contextRows,
      "BillingSubscriptionRepository.withOfferContext",
    );
    if (rawContext === null) {
      throw new StoreError("Billing subscription offer context is missing", {
        details: {
          offerId: reference.offer_id,
          catalogRevisionId: reference.catalog_revision_id,
        },
      });
    }
    const context = safeParse(
      OfferContextSchema,
      {
        offer_key: rawContext.offer_key,
        plan_id: rawContext.plan_id,
        plan_key: rawContext.plan_key,
        billing_unit: rawContext.billing_unit,
        billing_count: rawContext.billing_count,
      },
      "BillingSubscriptionRepository.withOfferContext.context",
    );
    return {
      ...value,
      ...context,
      plan: context.plan_key,
      interval: context.billing_unit,
      interval_count: context.billing_count,
    };
  }

  async get(provider: string, providerSubscriptionId: string): Promise<SubscriptionRow | null> {
    const rows = await this.query(
      `SELECT * FROM bursar.get_billing_subscription_by_provider($1, $2)`,
      [provider, providerSubscriptionId],
    );
    const row = optionalRecordRow(rows, "BillingSubscriptionRepository.get");
    return row === null
      ? null
      : this.map(
          await this.withOfferContext(
            persistedSubscription(row, "BillingSubscriptionRepository.get"),
          ),
        );
  }
  async getUserSubscription(userId: string, statuses?: string[]): Promise<SubscriptionRow | null> {
    const allowed = new Set(
      safeParse(
        z.array(SubscriptionStatusSchema),
        statuses ?? ["active", "trialing"],
        "BillingSubscriptionRepository.getUserSubscription.statuses",
      ),
    );
    const rows = await this.query(`SELECT * FROM bursar.list_billing_subscriptions($1::uuid)`, [
      userId,
    ]);
    const candidates = rows
      .map((row) => persistedSubscription(row, "BillingSubscriptionRepository.getUserSubscription"))
      .filter((row) => allowed.has(row.status))
      .sort(
        (left, right) =>
          Date.parse(right.provider_updated_at) - Date.parse(left.provider_updated_at),
      );
    const selected = candidates[0];
    return selected === undefined ? null : this.map(await this.withOfferContext(selected));
  }
  async getUserSubscriptions(userId: string): Promise<SubscriptionRow[]> {
    const rows = await this.query(`SELECT * FROM bursar.list_billing_subscriptions($1::uuid)`, [
      userId,
    ]);
    const persisted = rows.map((row) =>
      persistedSubscription(row, "BillingSubscriptionRepository.getUserSubscriptions"),
    );
    const enriched = await Promise.all(persisted.map((row) => this.withOfferContext(row)));
    return enriched.map((row) => this.map(row));
  }

  async listExpiredGraceSubscriptions(now: string, limit = 100): Promise<SubscriptionRow[]> {
    const rows = await this.query(`SELECT * FROM bursar.list_expired_grace_subscriptions($1, $2)`, [
      now,
      limit,
    ]);
    const persisted = rows.map((row) =>
      persistedSubscription(row, "BillingSubscriptionRepository.listExpiredGraceSubscriptions"),
    );
    const enriched = await Promise.all(persisted.map((row) => this.withOfferContext(row)));
    return enriched.map((row) => this.map(row));
  }

  async reconcileEntitlement(
    subjectId: string,
    subscriptionId: string,
    billingEventId: string,
    expectedStatus: BillingSubscriptionStatus,
    expectedProviderUpdatedAt: string,
    planAssignedAt: Date | string | null,
    applyEntitlement: boolean,
    terminalPlanKey: string | null,
    reason: string,
  ): Promise<SubscriptionEntitlementOutcome> {
    const expectedTimestamp = safeParse(
      timestamp,
      expectedProviderUpdatedAt,
      "BillingSubscriptionRepository.reconcileEntitlement.expectedProviderUpdatedAt",
    );
    const assignmentTimestamp =
      planAssignedAt === null
        ? null
        : safeParse(
            timestamp,
            planAssignedAt,
            "BillingSubscriptionRepository.reconcileEntitlement.planAssignedAt",
          );
    const rows = await this.query(
      `SELECT bursar.reconcile_subscription_entitlement(
         $1::uuid, $2::uuid, $3::uuid, $4::bursar.billing_subscription_status,
         $5::timestamptz, $6::timestamptz, $7, $8, $9
       ) AS outcome`,
      [
        subjectId,
        subscriptionId,
        billingEventId,
        expectedStatus,
        expectedTimestamp,
        assignmentTimestamp,
        applyEntitlement,
        terminalPlanKey,
        reason,
      ],
    );
    return requireResultField(
      rows,
      "outcome",
      SubscriptionEntitlementOutcomeSchema,
      "BillingSubscriptionRepository.reconcileEntitlement",
    );
  }

  async expireGracePeriod(
    subjectId: string,
    subscriptionId: string,
    expectedGraceEndsAt: string,
    expiredAt: string,
    terminalPlanKey: string | null,
  ): Promise<boolean> {
    const rows = await this.query(
      `SELECT bursar.expire_subscription_grace_period($1::uuid, $2::uuid, $3, $4, $5) AS expired`,
      [subjectId, subscriptionId, expectedGraceEndsAt, expiredAt, terminalPlanKey],
    );
    return requireResultField(
      rows,
      "expired",
      pgBoolean,
      "BillingSubscriptionRepository.expireGracePeriod",
    );
  }

  async recordConflict(input: {
    userId?: string | null;
    provider: string;
    duplicateSubscriptionId: string;
    existingSubscriptionId?: string | null;
    eventId?: string | null;
    metadata?: JsonObject;
  }): Promise<string> {
    const rows = await this.query(
      `SELECT bursar.record_subscription_conflict(
         $1::uuid, $2, $3, $4, $5, $6::jsonb
       ) AS id`,
      [
        input.userId ?? null,
        input.provider,
        input.duplicateSubscriptionId,
        input.existingSubscriptionId ?? null,
        input.eventId ?? null,
        JSON.stringify(input.metadata ?? {}),
      ],
    );
    return requireResultField(
      rows,
      "id",
      z.union([z.string().min(1), z.number().int()]).transform(String),
      "BillingSubscriptionRepository.recordConflict",
    );
  }
}
