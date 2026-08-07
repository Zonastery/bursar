import { z } from "zod";
import type { BillingSubscriptionState } from "../../types/subscriptions.js";
import type { QueryFn } from "../../../shared/postgres-types.js";
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
    metadata: z.record(z.string(), z.unknown()),
  })
  .passthrough();

function providerTimestampValue(row: Record<string, unknown>): number {
  return Date.parse(
    safeParse(
      timestamp,
      row.provider_updated_at,
      "BillingSubscriptionRepository.providerUpdatedAt",
    ),
  );
}

function compareProviderTimestampsDescending(
  left: Record<string, unknown>,
  right: Record<string, unknown>,
): number {
  return providerTimestampValue(right) - providerTimestampValue(left);
}
export type SubscriptionRow = z.infer<typeof SubscriptionRowSchema>;

export class BillingSubscriptionRepository {
  constructor(private query: QueryFn) {}

  async upsert(state: BillingSubscriptionState): Promise<void> {
    const provider = state.provider;
    const providerSubscriptionId = state.providerSubscriptionId;
    let userId = state.userId;
    let providerCustomerId = state.providerCustomerId ?? null;
    let offerId = state.offerId ?? "";

    if (!provider || !providerSubscriptionId) {
      throw new TypeError(
        "subscription.upsert: subject, provider, subscription, and offer are required",
      );
    }

    const existing = await this.query(
      `SELECT * FROM bursar.get_billing_subscription_by_provider($1, $2)`,
      [provider, providerSubscriptionId],
    );
    const existingRow =
      optionalRecordRow(existing, "BillingSubscriptionRepository.upsert.existing") ?? undefined;
    if (!userId && existingRow?.subject_id != null) userId = String(existingRow.subject_id);
    if (providerCustomerId == null && existingRow?.provider_customer_id != null) {
      providerCustomerId = String(existingRow.provider_customer_id);
    }
    if (!offerId && existingRow?.offer_id != null) offerId = String(existingRow.offer_id);

    const offerKey = state.offerKey ?? "";
    if (!offerId && offerKey) {
      const offerRows = await this.query(`SELECT * FROM bursar.resolve_active_catalog_offer($1)`, [
        offerKey,
      ]);
      const offerRow =
        optionalRecordRow(offerRows, "BillingSubscriptionRepository.upsert.offer") ?? undefined;
      if (offerRow?.id != null) offerId = String(offerRow.id);
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

  private map(row: unknown): SubscriptionRow {
    const r = row as Record<string, unknown>;
    return safeParse(
      SubscriptionRowSchema,
      { ...r, user_id: r.subject_id },
      "BillingSubscriptionRepository",
    );
  }

  private async withOfferContext(row: unknown): Promise<Record<string, unknown>> {
    const value = row as Record<string, unknown>;
    if (value.offer_id == null || value.catalog_revision_id == null) {
      throw new StoreError("Billing subscription is missing its catalog reference");
    }
    const contextRows = await this.query(
      `SELECT * FROM bursar.get_catalog_offer_context($1::uuid, $2::uuid)`,
      [value.offer_id, value.catalog_revision_id],
    );
    const context =
      optionalRecordRow(contextRows, "BillingSubscriptionRepository.withOfferContext") ?? undefined;
    if (context?.offer_key == null) {
      throw new StoreError("Billing subscription offer context is missing", {
        details: {
          offerId: value.offer_id,
          catalogRevisionId: value.catalog_revision_id,
        },
      });
    }
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
    return row === null ? null : this.map(await this.withOfferContext(row));
  }
  async getUserSubscription(userId: string, statuses?: string[]): Promise<SubscriptionRow | null> {
    const rows = await this.query(`SELECT * FROM bursar.list_billing_subscriptions($1::uuid)`, [
      userId,
    ]);
    const allowed = statuses ?? ["active", "trialing"];
    const candidates = (rows as Array<Record<string, unknown>>)
      .filter((row) => allowed.includes(String(row.status)))
      .sort(compareProviderTimestampsDescending);
    return candidates[0] ? this.map(await this.withOfferContext(candidates[0])) : null;
  }
  async getUserSubscriptions(userId: string): Promise<SubscriptionRow[]> {
    const rows = await this.query(`SELECT * FROM bursar.list_billing_subscriptions($1::uuid)`, [
      userId,
    ]);
    const enriched = await Promise.all(rows.map((row) => this.withOfferContext(row)));
    return enriched.map((row) => this.map(row));
  }

  async listExpiredGraceSubscriptions(now: string, limit = 100): Promise<SubscriptionRow[]> {
    const rows = await this.query(`SELECT * FROM bursar.list_expired_grace_subscriptions($1, $2)`, [
      now,
      limit,
    ]);
    const enriched = await Promise.all(rows.map((row) => this.withOfferContext(row)));
    return enriched.map((row) => this.map(row));
  }

  async markGraceExpired(
    subscriptionId: string,
    expectedGraceEndsAt: string,
    expiredAt: string,
  ): Promise<boolean> {
    const rows = await this.query(
      `SELECT bursar.mark_subscription_grace_expired($1::uuid, $2, $3) AS marked`,
      [subscriptionId, expectedGraceEndsAt, expiredAt],
    );
    return requireResultField(
      rows,
      "marked",
      pgBoolean,
      "BillingSubscriptionRepository.markGraceExpired",
    );
  }

  async recordConflict(input: {
    userId?: string | null;
    provider: string;
    duplicateSubscriptionId: string;
    existingSubscriptionId?: string | null;
    eventId?: string | null;
    metadata?: Record<string, unknown>;
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

  /** Select a provider subscription without modifying provider-reported state. */
  async selectEntitlementSource(
    userId: string,
    provider: string,
    providerSubscriptionId?: string | null,
  ): Promise<boolean> {
    const rows = (await this.query(`SELECT * FROM bursar.list_billing_subscriptions($1::uuid)`, [
      userId,
    ])) as Array<Record<string, unknown>>;
    const eligibleStatuses = new Set(["trialing", "active", "past_due", "paused"]);
    const replacement = rows
      .filter(
        (row) =>
          row.provider === provider &&
          eligibleStatuses.has(String(row.status)) &&
          (!providerSubscriptionId || row.provider_subscription_id === providerSubscriptionId),
      )
      .sort(compareProviderTimestampsDescending)[0];

    if (replacement?.id == null) return false;

    const selectedRows = await this.query(
      `SELECT bursar.select_entitlement_source($1::uuid, $2::uuid) AS selected`,
      [userId, replacement.id],
    );
    if (
      !requireResultField(
        selectedRows,
        "selected",
        pgBoolean,
        "BillingSubscriptionRepository.selectEntitlementSource",
      )
    ) {
      throw new StoreError("subscription entitlement source selection was rejected");
    }

    return true;
  }
}
