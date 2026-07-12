import { z } from "zod";
import type { QueryFn } from "../types.js";
import { pgBoolean, safeParse } from "../_shared.js";

const SUBSCRIPTION_COLS = `user_id, provider, provider_subscription_id, provider_customer_id,
    offer_key, plan, status, current_period_start,
    current_period_end, cancel_at_period_end, interval, interval_count, metadata`;

const SUBSCRIPTION_STATUS = {
  ACTIVE: "active",
  TRIALING: "trialing",
  CANCELED: "canceled",
  INCOMPLETE: "incomplete",
} as const;

export const SubscriptionRowSchema = z
  .object({
    user_id: z.string(),
    provider: z.string(),
    provider_subscription_id: z.string(),
    provider_customer_id: z.string().nullable().optional(),
    offer_key: z.string().nullable().optional(),
    plan: z.string().nullable().optional(),
    status: z.string().optional(),
    current_period_start: z.unknown().optional(),
    current_period_end: z.unknown().optional(),
    cancel_at_period_end: pgBoolean.nullable().optional(),
    interval: z.string().nullable().optional(),
    interval_count: z.number().nullable().optional(),
    metadata: z.record(z.string(), z.unknown()).nullable().optional(),
  })
  .passthrough();

export type SubscriptionRow = z.infer<typeof SubscriptionRowSchema>;

/** Repository for billing subscription operations. */
export class BillingSubscriptionRepository {
  constructor(private query: QueryFn) {}

  /** Upsert a billing subscription record.
   *
   * Requires userId, provider, and providerSubscriptionId in the state object.
   */
  async upsert(state: Record<string, unknown>): Promise<void> {
    const required = ["userId", "provider", "providerSubscriptionId"];
    for (const key of required) {
      if (!state[key]) throw new Error(`subscription.upsert: ${key} is required`);
    }
    await this.query(
      `INSERT INTO public.billing_subscriptions (${SUBSCRIPTION_COLS})
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
       ON CONFLICT (provider, provider_subscription_id) DO UPDATE SET
         user_id = EXCLUDED.user_id,
         provider_customer_id = COALESCE(EXCLUDED.provider_customer_id, billing_subscriptions.provider_customer_id),
         offer_key = COALESCE(EXCLUDED.offer_key, billing_subscriptions.offer_key),
         plan = COALESCE(EXCLUDED.plan, billing_subscriptions.plan),
         status = EXCLUDED.status,
         current_period_start = COALESCE(EXCLUDED.current_period_start, billing_subscriptions.current_period_start),
         current_period_end = COALESCE(EXCLUDED.current_period_end, billing_subscriptions.current_period_end),
         cancel_at_period_end = EXCLUDED.cancel_at_period_end,
         interval = COALESCE(EXCLUDED.interval, billing_subscriptions.interval),
         interval_count = COALESCE(EXCLUDED.interval_count, billing_subscriptions.interval_count),
         metadata = CASE WHEN EXCLUDED.metadata IS NOT NULL THEN EXCLUDED.metadata ELSE billing_subscriptions.metadata END,
         updated_at = now()`,
      [
        state.userId,
        state.provider,
        state.providerSubscriptionId,
        state.providerCustomerId ?? null,
        state.offerKey ?? null,
        state.plan ?? null,
        state.status ?? SUBSCRIPTION_STATUS.INCOMPLETE,
        state.currentPeriodStart ?? null,
        state.currentPeriodEnd ?? null,
        state.cancelAtPeriodEnd ?? false,
        state.interval ?? null,
        state.intervalCount ?? null,
        state.metadata ? JSON.stringify(state.metadata) : null,
      ],
    );
  }

  /** Fetch a subscription by provider identifiers. */
  async get(provider: string, providerSubscriptionId: string): Promise<SubscriptionRow | null> {
    const rows = await this.query(
      `SELECT ${SUBSCRIPTION_COLS}
       FROM public.billing_subscriptions
       WHERE provider = $1 AND provider_subscription_id = $2`,
      [provider, providerSubscriptionId],
    );
    if (rows.length === 0) return null;
    return safeParse(SubscriptionRowSchema, rows[0], "BillingSubscriptionRepository.get");
  }

  /** Fetch a user's most recent subscription, filtered by status.
   *
   * @param statuses - Allowed statuses to return, defaults to active/trialing.
   */
  async getUserSubscription(userId: string, statuses?: string[]): Promise<SubscriptionRow | null> {
    const activeStatuses = statuses ?? [SUBSCRIPTION_STATUS.ACTIVE, SUBSCRIPTION_STATUS.TRIALING];
    const rows = await this.query(
      `SELECT ${SUBSCRIPTION_COLS}
       FROM public.billing_subscriptions
       WHERE user_id = $1 AND status = ANY($2::text[])
       ORDER BY current_period_start DESC NULLS LAST, created_at DESC
       LIMIT 1`,
      [userId, activeStatuses],
    );
    if (rows.length === 0) return null;
    return safeParse(
      SubscriptionRowSchema,
      rows[0],
      "BillingSubscriptionRepository.getUserSubscription",
    );
  }

  /** Fetch all subscriptions for a user. */
  async getUserSubscriptions(userId: string): Promise<SubscriptionRow[]> {
    const rows = await this.query(
      `SELECT ${SUBSCRIPTION_COLS}
       FROM public.billing_subscriptions
       WHERE user_id = $1
       ORDER BY current_period_start DESC NULLS LAST`,
      [userId],
    );
    return rows.map((r) =>
      safeParse(SubscriptionRowSchema, r, "BillingSubscriptionRepository.getUserSubscriptions"),
    );
  }

  /** Deactivate subscriptions from other providers, keeping the current one. */
  async deactivateOtherProviderSubscriptions(
    userId: string,
    keepProvider: string,
  ): Promise<number> {
    const rows = await this.query(
      `UPDATE public.billing_subscriptions
       SET status = 'canceled', cancel_at_period_end = true, updated_at = now()
       WHERE user_id = $1 AND provider != $2 AND status IN ('active', 'trialing')
       RETURNING 1`,
      [userId, keepProvider],
    );
    return rows.length;
  }
}
