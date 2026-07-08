import type {
  BillingConfig,
  BillingEventClaim,
  BillingSubscriptionState,
} from "./billing-types.js";
import { BillingStore } from "./billing-store.js";

/**
 * Postgres-backed billing store — calls SQL RPCs via a pg pool.
 * Mirrors Python bursar/billing/postgres.py.
 */
export class PostgresBillingStore extends BillingStore {
  private pool: import("pg").Pool;

  constructor(pool: import("pg").Pool) {
    super();
    this.pool = pool;
  }

  private snakeToCamel(str: string): string {
    return str.replace(/_([a-z])/g, (_, letter) => letter.toUpperCase());
  }

  private snakeToCamelKeys(obj: unknown): unknown {
    if (Array.isArray(obj)) return obj.map((item) => this.snakeToCamelKeys(item));
    if (obj && typeof obj === "object" && obj !== null) {
      const converted: Record<string, unknown> = {};
      for (const [key, value] of Object.entries(obj as Record<string, unknown>)) {
        converted[this.snakeToCamel(key)] = this.snakeToCamelKeys(value);
      }
      return converted;
    }
    return obj;
  }

  private async callRpcJson(
    rpcName: string,
    params: unknown[],
  ): Promise<Record<string, unknown> | null> {
    const placeholders = params.map((_, i) => `$${i + 1}`).join(", ");
    const rows = await this.pool.query(`SELECT * FROM public.${rpcName}(${placeholders})`, params);
    if (rows.rows.length === 1) {
      const row = rows.rows[0] as Record<string, unknown>;
      const keys = Object.keys(row);
      if (keys.length === 1) {
        const v = row[keys[0]];
        if (v !== null && typeof v === "object" && !Array.isArray(v)) {
          return this.snakeToCamelKeys(v) as Record<string, unknown>;
        }
      }
    }
    return null;
  }

  private async callRpcVoid(rpcName: string, params: unknown[]): Promise<void> {
    const placeholders = params.map((_, i) => `$${i + 1}`).join(", ");
    await this.pool.query(`SELECT * FROM public.${rpcName}(${placeholders})`, params);
  }

  private camelToSnake(str: string): string {
    return str
      .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
      .replace(/([A-Z]+)([A-Z][a-z])/g, "$1_$2")
      .toLowerCase();
  }

  private camelToSnakeKeys(obj: unknown): unknown {
    if (Array.isArray(obj)) return obj.map((item) => this.camelToSnakeKeys(item));
    if (obj && typeof obj === "object" && obj !== null) {
      const converted: Record<string, unknown> = {};
      for (const [key, value] of Object.entries(obj as Record<string, unknown>)) {
        converted[this.camelToSnake(key)] = this.camelToSnakeKeys(value);
      }
      return converted;
    }
    return obj;
  }

  async syncBillingFromConfig(config: BillingConfig): Promise<void> {
    await this.pool.query("SELECT public.sync_billing_from_config($1::jsonb)", [
      JSON.stringify(this.camelToSnakeKeys(config)),
    ]);
  }

  async resolveBillingOffer(
    provider: string,
    productId?: string | null,
    priceId?: string | null,
  ): Promise<Record<string, unknown> | null> {
    const result = await this.callRpcJson("resolve_billing_offer_by_price", [
      provider,
      priceId ?? null,
      productId ?? null,
    ]);
    if (result && result.offerKey) return result;
    return null;
  }

  async claimBillingEvent(
    provider: string,
    eventId: string,
    eventType: string,
  ): Promise<BillingEventClaim> {
    const result = await this.callRpcJson("claim_billing_event", [
      provider,
      eventId,
      eventType,
      JSON.stringify({ eventType }),
    ]);
    if (!result) return { status: "retry" as const };
    const s = result.status as string;
    if (s === "claimed") return { status: "claimed" as const };
    if (s === "duplicate") return { status: "duplicate" as const };
    return { status: "retry" as const };
  }

  async completeBillingEvent(provider: string, eventId: string): Promise<void> {
    await this.callRpcVoid("complete_billing_event", [provider, eventId]);
  }

  async failBillingEvent(provider: string, eventId: string): Promise<void> {
    await this.callRpcVoid("fail_billing_event", [provider, eventId]);
  }

  async upsertBillingCustomer(
    provider: string,
    providerCustomerId: string,
    userId: string,
    email?: string | null,
  ): Promise<void> {
    await this.pool.query(
      `INSERT INTO public.billing_customers (provider, provider_customer_id, user_id, email)
       VALUES ($1, $2, $3, $4)
       ON CONFLICT (provider, provider_customer_id) DO UPDATE SET
         user_id = EXCLUDED.user_id,
         email = COALESCE(EXCLUDED.email, billing_customers.email),
         updated_at = now()`,
      [provider, providerCustomerId, userId, email ?? null],
    );
  }

  async upsertBillingSubscription(state: BillingSubscriptionState): Promise<void> {
    await this.pool.query(
      `INSERT INTO public.billing_subscriptions (
         user_id, provider, provider_subscription_id, provider_customer_id,
         offer_key, plan_key, status, current_period_start,
         current_period_end, cancel_at_period_end, interval, interval_count, metadata
       )
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
       ON CONFLICT (provider, provider_subscription_id) DO UPDATE SET
         user_id = EXCLUDED.user_id,
         provider_customer_id = COALESCE(EXCLUDED.provider_customer_id, billing_subscriptions.provider_customer_id),
         offer_key = COALESCE(EXCLUDED.offer_key, billing_subscriptions.offer_key),
         plan_key = COALESCE(EXCLUDED.plan_key, billing_subscriptions.plan_key),
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
        state.planKey ?? null,
        state.status ?? "incomplete",
        state.currentPeriodStart ?? null,
        state.currentPeriodEnd ?? null,
        state.cancelAtPeriodEnd ?? false,
        state.interval ?? null,
        state.intervalCount ?? null,
        state.metadata ? JSON.stringify(state.metadata) : null,
      ],
    );
  }

  async getBillingCustomer(provider: string, providerCustomerId: string): Promise<string | null> {
    const result = await this.pool.query(
      "SELECT user_id FROM public.billing_customers WHERE provider = $1 AND provider_customer_id = $2",
      [provider, providerCustomerId],
    );
    if (result.rows.length === 0) return null;
    return String(result.rows[0].user_id);
  }

  async getBillingSubscription(
    provider: string,
    providerSubscriptionId: string,
  ): Promise<BillingSubscriptionState | null> {
    const result = await this.pool.query(
      `SELECT user_id, provider, provider_subscription_id, provider_customer_id,
              offer_key, plan_key, status, current_period_start,
              current_period_end, cancel_at_period_end, interval, interval_count, metadata
       FROM public.billing_subscriptions
       WHERE provider = $1 AND provider_subscription_id = $2`,
      [provider, providerSubscriptionId],
    );
    if (result.rows.length === 0) return null;

    const r = result.rows[0];
    return this.rowToSubscriptionState(r);
  }

  async getUserSubscription(userId: string): Promise<BillingSubscriptionState | null> {
    const result = await this.pool.query(
      `SELECT user_id, provider, provider_subscription_id, provider_customer_id,
              offer_key, plan_key, status, current_period_start,
              current_period_end, cancel_at_period_end, interval, interval_count, metadata
       FROM public.billing_subscriptions
       WHERE user_id = $1
       ORDER BY current_period_start DESC NULLS LAST, created_at DESC
       LIMIT 1`,
      [userId],
    );
    if (result.rows.length === 0) return null;
    return this.rowToSubscriptionState(result.rows[0]);
  }

  private rowToSubscriptionState(r: Record<string, unknown>): BillingSubscriptionState {
    return {
      userId: String(r.user_id),
      provider: String(r.provider),
      providerSubscriptionId: String(r.provider_subscription_id),
      providerCustomerId: r.provider_customer_id ? String(r.provider_customer_id) : null,
      offerKey: r.offer_key ? String(r.offer_key) : null,
      planKey: r.plan_key ? String(r.plan_key) : null,
      status: r.status ? String(r.status) : "incomplete",
      currentPeriodStart: r.current_period_start
        ? (r.current_period_start as Date).toISOString()
        : null,
      currentPeriodEnd: r.current_period_end ? (r.current_period_end as Date).toISOString() : null,
      cancelAtPeriodEnd: Boolean(r.cancel_at_period_end),
      interval: r.interval ? String(r.interval) : null,
      intervalCount: r.interval_count ? Number(r.interval_count) : null,
      metadata:
        r.metadata && typeof r.metadata === "object"
          ? (r.metadata as Record<string, unknown>)
          : null,
    };
  }

  async resolveCreditTopup(
    provider: string,
    productId?: string | null,
    priceId?: string | null,
  ): Promise<Record<string, unknown> | null> {
    const result = await this.callRpcJson("resolve_credit_topup_by_price", [
      provider,
      priceId ?? null,
      productId ?? null,
    ]);
    if (result && result.topupKey) return result;
    return null;
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
    await this.pool.query(
      `SELECT public.upsert_billing_payment($1, $2, $3, $4, $5, $6, $7, $8, $9)`,
      [
        provider,
        providerPaymentId,
        providerInvoiceId ?? null,
        userId ?? null,
        amountMinor ?? 0,
        taxMinor ?? null,
        currency ?? "USD",
        purpose ?? null,
        metadata ? JSON.stringify(metadata) : null,
      ],
    );
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
    await this.pool.query(`SELECT public.upsert_billing_refund($1, $2, $3, $4, $5, $6, $7, $8)`, [
      provider,
      providerRefundId,
      providerPaymentId ?? null,
      userId ?? null,
      amountMinor ?? 0,
      currency ?? "USD",
      reason ?? null,
      metadata ? JSON.stringify(metadata) : null,
    ]);
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
    await this.pool.query(
      `SELECT public.upsert_billing_invoice($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)`,
      [
        provider,
        providerInvoiceId,
        providerSubscriptionId ?? null,
        userId ?? null,
        status ?? null,
        amountPaidMinor ?? null,
        amountDueMinor ?? null,
        currency ?? "USD",
        periodStart ?? null,
        periodEnd ?? null,
        metadata ? JSON.stringify(metadata) : null,
      ],
    );
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
    await this.pool.query(`SELECT public.upsert_billing_dispute($1, $2, $3, $4, $5, $6, $7)`, [
      provider,
      providerDisputeId,
      providerPaymentId ?? null,
      userId ?? null,
      status ?? "open",
      reason ?? null,
      metadata ? JSON.stringify(metadata) : null,
    ]);
  }

  async getBillingPayment(
    provider: string,
    providerPaymentId: string,
  ): Promise<Record<string, unknown> | null> {
    const result = await this.pool.query(
      `SELECT * FROM public.get_billing_payment_for_refund($1, $2)`,
      [provider, providerPaymentId],
    );
    if (result.rows.length === 0) return null;
    const row = result.rows[0] as Record<string, unknown>;
    const keys = Object.keys(row);
    if (keys.length === 1) {
      const v = row[keys[0]];
      if (v !== null && typeof v === "object" && !Array.isArray(v)) {
        return this.snakeToCamelKeys(v) as Record<string, unknown>;
      }
    }
    return null;
  }
}
