import type { SupabaseClient } from "@supabase/supabase-js";
import { BillingStore } from "./billing-store.js";
import type {
  BillingConfig,
  BillingEventClaim,
  BillingSubscriptionState,
} from "./billing-types.js";

function toSnakeCase(obj: Record<string, unknown>): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(obj)) {
    const snakeKey = key.replace(/[A-Z]/g, (m) => `_${m.toLowerCase()}`);
    result[snakeKey] = value;
  }
  return result;
}

export class SupabaseBillingStore extends BillingStore {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- SupabaseClient generic varies per project
  private supabase: SupabaseClient<any>;

  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- SupabaseClient generic varies per project
  constructor(supabase: SupabaseClient<any>) {
    super();
    this.supabase = supabase;
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

  private stripUndefined<T>(obj: T): T {
    const result = { ...obj } as Record<string, unknown>;
    for (const key of Object.keys(result)) {
      if (result[key] === undefined) {
        delete result[key];
      } else if (typeof result[key] === "object" && result[key] !== null) {
        result[key] = this.stripUndefined(result[key] as Record<string, unknown>);
      }
    }
    return result as unknown as T;
  }

  async syncBillingFromConfig(config: BillingConfig): Promise<void> {
    const { error } = await this.supabase.rpc("sync_billing_from_config", {
      p_config: this.stripUndefined(config),
    });
    if (error) throw error;
  }

  async resolveBillingOffer(
    provider: string,
    productId?: string | null,
    priceId?: string | null,
  ): Promise<Record<string, unknown> | null> {
    const { data, error } = await this.supabase.rpc("resolve_billing_offer_by_price", {
      p_provider: provider,
      p_price_id: priceId ?? undefined,
      p_product_id: productId ?? undefined,
    });
    if (error) throw error;
    if (!data) return null;
    return this.snakeToCamelKeys(data) as Record<string, unknown> | null;
  }

  async claimBillingEvent(
    provider: string,
    eventId: string,
    eventType: string,
  ): Promise<BillingEventClaim> {
    const { data, error } = await this.supabase.rpc("claim_billing_event", {
      p_provider: provider,
      p_event_id: eventId,
      p_event_type: eventType,
      p_payload: { eventType },
    });
    if (error || !data) return { status: "retry" };
    const result = data as Record<string, string>;
    const s = result.status;
    if (s === "claimed") return { status: "claimed" as const };
    if (s === "duplicate") return { status: "duplicate" as const };
    return { status: "retry" as const };
  }

  async completeBillingEvent(provider: string, eventId: string): Promise<void> {
    const { error } = await this.supabase.rpc("complete_billing_event", {
      p_provider: provider,
      p_event_id: eventId,
    });
    if (error) throw error;
  }

  async failBillingEvent(provider: string, eventId: string): Promise<void> {
    const { error } = await this.supabase.rpc("fail_billing_event", {
      p_provider: provider,
      p_event_id: eventId,
    });
    if (error) throw error;
  }

  async upsertBillingCustomer(
    provider: string,
    providerCustomerId: string,
    userId: string,
    email?: string | null,
  ): Promise<void> {
    const { error } = await this.supabase.rpc("upsert_billing_customer", {
      p_provider: provider,
      p_provider_customer_id: providerCustomerId,
      p_user_id: userId,
      p_email: email ?? undefined,
    });
    if (error) throw error;
  }

  async getBillingCustomer(provider: string, providerCustomerId: string): Promise<string | null> {
    const { data, error } = await this.supabase.rpc("get_billing_customer", {
      p_provider: provider,
      p_provider_customer_id: providerCustomerId,
    });
    if (error) throw error;
    if (!data) return null;
    return String((data as Record<string, string>).user_id);
  }

  async upsertBillingSubscription(state: BillingSubscriptionState): Promise<void> {
    const { error } = await this.supabase.rpc("upsert_billing_subscription", {
      p_state: toSnakeCase(state as unknown as Record<string, unknown>),
    });
    if (error) throw error;
  }

  async getBillingSubscription(
    provider: string,
    providerSubscriptionId: string,
  ): Promise<BillingSubscriptionState | null> {
    const { data, error } = await this.supabase.rpc("get_billing_subscription", {
      p_provider: provider,
      p_provider_subscription_id: providerSubscriptionId,
    });
    if (error) throw error;
    if (!data) return null;
    return this.rowToSubscriptionState(data as Record<string, unknown>);
  }

  async getUserSubscription(userId: string): Promise<BillingSubscriptionState | null> {
    const { data, error } = await this.supabase.rpc("get_user_billing_subscription", {
      p_user_id: userId,
    });
    if (error) throw error;
    if (!data) return null;
    return this.rowToSubscriptionState(data as Record<string, unknown>);
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
      currentPeriodStart: r.current_period_start ? String(r.current_period_start) : null,
      currentPeriodEnd: r.current_period_end ? String(r.current_period_end) : null,
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
    const { data, error } = await this.supabase.rpc("resolve_credit_topup_by_price", {
      p_provider: provider,
      p_price_id: priceId ?? undefined,
      p_product_id: productId ?? undefined,
    });
    if (error) throw error;
    if (!data) return null;
    return this.snakeToCamelKeys(data) as Record<string, unknown> | null;
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
    const { error } = await this.supabase.rpc("upsert_billing_payment", {
      p_provider: provider,
      p_provider_payment_id: providerPaymentId,
      p_provider_invoice_id: providerInvoiceId ?? null,
      p_user_id: userId ?? null,
      p_amount_minor: amountMinor ?? 0,
      p_tax_minor: taxMinor ?? null,
      p_currency: currency ?? "USD",
      p_purpose: purpose ?? null,
      p_metadata: metadata ?? null,
    });
    if (error) throw error;
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
    const { error } = await this.supabase.rpc("upsert_billing_refund", {
      p_provider: provider,
      p_provider_refund_id: providerRefundId,
      p_provider_payment_id: providerPaymentId ?? null,
      p_user_id: userId ?? null,
      p_amount_minor: amountMinor ?? 0,
      p_currency: currency ?? "USD",
      p_reason: reason ?? null,
      p_metadata: metadata ?? null,
    });
    if (error) throw error;
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
    const { error } = await this.supabase.rpc("upsert_billing_invoice", {
      p_provider: provider,
      p_provider_invoice_id: providerInvoiceId,
      p_provider_subscription_id: providerSubscriptionId ?? null,
      p_user_id: userId ?? null,
      p_status: status ?? null,
      p_amount_paid_minor: amountPaidMinor ?? null,
      p_amount_due_minor: amountDueMinor ?? null,
      p_currency: currency ?? "USD",
      p_period_start: periodStart ?? null,
      p_period_end: periodEnd ?? null,
      p_metadata: metadata ?? null,
    });
    if (error) throw error;
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
    const { error } = await this.supabase.rpc("upsert_billing_dispute", {
      p_provider: provider,
      p_provider_dispute_id: providerDisputeId,
      p_provider_payment_id: providerPaymentId ?? null,
      p_user_id: userId ?? null,
      p_status: status ?? "open",
      p_reason: reason ?? null,
      p_metadata: metadata ?? null,
    });
    if (error) throw error;
  }

  async getBillingPayment(
    provider: string,
    providerPaymentId: string,
  ): Promise<Record<string, unknown> | null> {
    const { data, error } = await this.supabase.rpc("get_billing_payment_for_refund", {
      p_provider: provider,
      p_provider_payment_id: providerPaymentId,
    });
    if (error) throw error;
    return data as Record<string, unknown> | null;
  }

  async getBillingPaymentDirect(
    provider: string,
    providerPaymentId: string,
  ): Promise<Record<string, unknown> | null> {
    const { data, error } = await this.supabase
      .from("billing_payments")
      .select("*")
      .eq("provider", provider)
      .eq("provider_payment_id", providerPaymentId)
      .maybeSingle();
    if (error) throw error;
    return data ? (this.snakeToCamelKeys(data) as Record<string, unknown>) : null;
  }
}
