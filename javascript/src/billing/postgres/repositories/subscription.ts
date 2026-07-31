import { z } from "zod";
import type { QueryFn } from "../../../shared/postgres-types.js";
import { pgBoolean, safeParse } from "../../../shared/postgres-validation.js";

const SubscriptionRowSchema = z
  .object({
    user_id: z.string().optional(),
    subject_id: z.string().optional(),
    provider: z.string(),
    provider_subscription_id: z.string(),
    provider_customer_id: z.string().nullable().optional(),
    offer_id: z.string().nullable().optional(),
    status: z.string().optional(),
    current_period_start: z.unknown().optional(),
    current_period_end: z.unknown().optional(),
    trial_end: z.unknown().optional(),
    cancel_at: z.unknown().optional(),
    ended_at: z.unknown().optional(),
    grace_ends_at: z.unknown().optional(),
    grace_expired_at: z.unknown().optional(),
    provider_updated_at: z.unknown().optional(),
    cancel_at_period_end: pgBoolean.nullable().optional(),
    metadata: z.record(z.string(), z.unknown()).nullable().optional(),
  })
  .passthrough();

function timestampValue(value: unknown): number | null {
  if (value instanceof Date) {
    const timestamp = value.getTime();
    return Number.isNaN(timestamp) ? null : timestamp;
  }
  const timestamp = Date.parse(String(value ?? ""));
  return Number.isNaN(timestamp) ? null : timestamp;
}

function compareProviderTimestampsDescending(
  left: Record<string, unknown>,
  right: Record<string, unknown>,
): number {
  const leftTimestamp = timestampValue(left.provider_updated_at);
  const rightTimestamp = timestampValue(right.provider_updated_at);
  if (leftTimestamp === null) return rightTimestamp === null ? 0 : 1;
  if (rightTimestamp === null) return -1;
  return rightTimestamp - leftTimestamp;
}
export type SubscriptionRow = z.infer<typeof SubscriptionRowSchema>;

export class BillingSubscriptionRepository {
  constructor(private query: QueryFn) {}

  async upsert(state: Record<string, unknown>): Promise<void> {
    const provider = String(state.provider ?? "");
    const providerSubscriptionId = String(state.providerSubscriptionId ?? "");
    let userId = String(state.userId ?? state.subjectId ?? "");
    let providerCustomerId = state.providerCustomerId ?? null;
    let offerId = String(state.offerId ?? state.offer_id ?? "");

    if (!provider || !providerSubscriptionId) {
      throw new Error(
        "subscription.upsert: subject, provider, subscription, and offer are required",
      );
    }

    const existing = await this.query(
      `SELECT * FROM bursar.get_billing_subscription_by_provider($1, $2)`,
      [provider, providerSubscriptionId],
    );
    const existingRow = existing[0] as Record<string, unknown> | undefined;
    if (!userId && existingRow?.subject_id != null) userId = String(existingRow.subject_id);
    if (providerCustomerId == null && existingRow?.provider_customer_id != null) {
      providerCustomerId = String(existingRow.provider_customer_id);
    }
    if (!offerId && existingRow?.offer_id != null) offerId = String(existingRow.offer_id);

    const offerKey = String(state.offerKey ?? state.offer_key ?? "");
    if (!offerId && offerKey) {
      const offerRows = await this.query(`SELECT * FROM bursar.resolve_active_catalog_offer($1)`, [
        offerKey,
      ]);
      const offerRow = offerRows[0] as Record<string, unknown> | undefined;
      if (offerRow?.id != null) offerId = String(offerRow.id);
    }

    if (!userId || !offerId) {
      throw new Error(
        "subscription.upsert: subject, provider, subscription, and offer are required",
      );
    }

    await this.query(
      "SELECT bursar.upsert_billing_subscription($1::uuid,$2,$3,$4,$5::uuid,$6::bursar.billing_subscription_status,$7,$8,$9,$10::jsonb,$11,$12,$13,$14,$15)",
      [
        userId,
        provider,
        providerSubscriptionId,
        providerCustomerId,
        offerId,
        state.status ?? "incomplete",
        state.currentPeriodStart ?? null,
        state.currentPeriodEnd ?? null,
        state.cancelAtPeriodEnd ?? false,
        JSON.stringify(state.metadata ?? {}),
        state.trialEnd ?? null,
        state.cancelAt ?? null,
        state.endedAt ?? null,
        state.providerUpdatedAt ?? new Date().toISOString(),
        state.graceEndsAt ?? null,
      ],
    );
  }

  private map(row: unknown): SubscriptionRow | null {
    const r = row as Record<string, unknown>;
    if (r.subject_id == null && r.provider == null) return null;
    return safeParse(
      SubscriptionRowSchema,
      { ...r, user_id: r.subject_id },
      "BillingSubscriptionRepository",
    );
  }

  private async withOfferContext(row: unknown): Promise<Record<string, unknown>> {
    const value = row as Record<string, unknown>;
    if (value.offer_id == null || value.catalog_revision_id == null) return value;
    const contextRows = await this.query(
      `SELECT * FROM bursar.get_catalog_offer_context($1::uuid, $2::uuid)`,
      [value.offer_id, value.catalog_revision_id],
    );
    const context = (contextRows[0] as Record<string, unknown> | undefined) ?? {};
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
    return rows[0] ? this.map(await this.withOfferContext(rows[0])) : null;
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
    return enriched
      .map((row) => this.map(row))
      .filter((row): row is SubscriptionRow => row !== null);
  }

  async listExpiredGraceSubscriptions(now: string, limit = 100): Promise<SubscriptionRow[]> {
    const rows = await this.query(`SELECT * FROM bursar.list_expired_grace_subscriptions($1, $2)`, [
      now,
      limit,
    ]);
    const enriched = await Promise.all(rows.map((row) => this.withOfferContext(row)));
    return enriched
      .map((row) => this.map(row))
      .filter((row): row is SubscriptionRow => row !== null);
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
    return (rows[0] as Record<string, unknown> | undefined)?.marked === true;
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
    const id = (rows[0] as Record<string, unknown> | undefined)?.id;
    if (id == null) throw new Error("subscription conflict audit returned no ID");
    return String(id);
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
    if ((selectedRows[0] as Record<string, unknown> | undefined)?.selected !== true) {
      throw new Error("subscription entitlement source selection was rejected");
    }

    return true;
  }
}
