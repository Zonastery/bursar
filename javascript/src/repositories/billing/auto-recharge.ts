import type { QueryFn } from "../types.js";
import type {
  BillingAutoRechargeAttempt,
  BillingAutoRechargeProfile,
} from "../../billing/billing-types.js";

function profileFromRow(row: Record<string, unknown>): BillingAutoRechargeProfile {
  return {
    userId: String(row.user_id),
    enabled: Boolean(row.enabled),
    state: String(row.state) as BillingAutoRechargeProfile["state"],
    provider: row.provider == null ? null : String(row.provider),
    providerCustomerId: row.provider_customer_id == null ? null : String(row.provider_customer_id),
    paymentMethodId: row.payment_method_id == null ? null : String(row.payment_method_id),
    suspendedReason: row.suspended_reason == null ? null : String(row.suspended_reason),
    consentedAt: row.consented_at == null ? null : new Date(String(row.consented_at)).toISOString(),
    policySnapshot: (row.policy_snapshot as Record<string, unknown> | null) ?? null,
    policyHash: row.policy_hash == null ? null : String(row.policy_hash),
    quoteSnapshot: (row.quote_snapshot as Record<string, unknown> | null) ?? null,
    consentReference: row.consent_reference == null ? null : String(row.consent_reference),
    armed: row.armed == null ? true : Boolean(row.armed),
  };
}

function attemptFromRow(row: Record<string, unknown>): BillingAutoRechargeAttempt {
  return {
    id: String(row.id),
    userId: String(row.user_id),
    provider: String(row.provider),
    idempotencyKey: String(row.idempotency_key),
    providerPaymentId: row.provider_payment_id == null ? null : String(row.provider_payment_id),
    topupKey: String(row.topup_key),
    quantity: Number(row.quantity),
    state: String(row.state) as BillingAutoRechargeAttempt["state"],
    credits: row.credits == null ? null : Number(row.credits),
    failureCode: row.failure_code == null ? null : String(row.failure_code),
    actionUrl: row.action_url == null ? null : String(row.action_url),
    createdAt: new Date(String(row.created_at)).toISOString(),
    updatedAt: new Date(String(row.updated_at)).toISOString(),
  };
}

export class BillingAutoRechargeRepository {
  constructor(private readonly query: QueryFn) {}

  async getProfile(userId: string): Promise<BillingAutoRechargeProfile | null> {
    const rows = await this.query(
      "SELECT * FROM bursar.billing_auto_recharge_profiles WHERE user_id = $1",
      [userId],
    );
    return rows[0] ? profileFromRow(rows[0]) : null;
  }

  async upsertProfile(profile: BillingAutoRechargeProfile): Promise<void> {
    await this.query(
      `INSERT INTO bursar.billing_auto_recharge_profiles
        (user_id, enabled, state, armed, provider, provider_customer_id, payment_method_id,
         policy_snapshot, policy_hash, quote_snapshot, consent_reference, suspended_reason, consented_at)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10::jsonb,$11,$12,$13)
       ON CONFLICT (user_id) DO UPDATE SET
         enabled = EXCLUDED.enabled, state = EXCLUDED.state, armed = EXCLUDED.armed, provider = EXCLUDED.provider,
         provider_customer_id = EXCLUDED.provider_customer_id, payment_method_id = EXCLUDED.payment_method_id,
         policy_snapshot = EXCLUDED.policy_snapshot, policy_hash = EXCLUDED.policy_hash,
         quote_snapshot = EXCLUDED.quote_snapshot, consent_reference = EXCLUDED.consent_reference,
         suspended_reason = EXCLUDED.suspended_reason, consented_at = EXCLUDED.consented_at, updated_at = now()`,
      [
        profile.userId,
        profile.enabled,
        profile.state,
        profile.armed ?? true,
        profile.provider ?? null,
        profile.providerCustomerId ?? null,
        profile.paymentMethodId ?? null,
        JSON.stringify(profile.policySnapshot ?? null),
        profile.policyHash ?? null,
        JSON.stringify(profile.quoteSnapshot ?? null),
        profile.consentReference ?? null,
        profile.suspendedReason ?? null,
        profile.consentedAt ?? null,
      ],
    );
  }

  async claimAttempt(input: {
    userId: string;
    provider: string;
    topupKey: string;
    quantity: number;
    maxRecharges: number;
    windowDays: number;
  }): Promise<BillingAutoRechargeAttempt | null> {
    const rows = await this.query(
      "SELECT * FROM bursar.claim_auto_recharge_attempt($1, $2, $3, $4, $5, $6)",
      [
        input.userId,
        input.provider,
        input.topupKey,
        input.quantity,
        input.maxRecharges,
        input.windowDays,
      ],
    );
    return rows[0] ? attemptFromRow(rows[0]) : null;
  }

  async updateAttempt(input: {
    id: string;
    state: string;
    providerPaymentId?: string | null;
    failureCode?: string | null;
    actionUrl?: string | null;
  }): Promise<void> {
    await this.query(
      `UPDATE bursar.billing_auto_recharge_attempts
       SET state = $2, provider_payment_id = COALESCE($3, provider_payment_id),
           failure_code = $4, action_url = $5, updated_at = now()
       WHERE id = $1`,
      [
        input.id,
        input.state,
        input.providerPaymentId ?? null,
        input.failureCode ?? null,
        input.actionUrl ?? null,
      ],
    );
  }

  async updateAttemptByProviderPayment(input: {
    provider: string;
    providerPaymentId: string;
    state: string;
    failureCode?: string | null;
  }): Promise<void> {
    await this.query(
      `UPDATE bursar.billing_auto_recharge_attempts
       SET state = $3, failure_code = $4, completed_at = CASE WHEN $3 IN ('succeeded','failed') THEN now() ELSE completed_at END, updated_at = now()
       WHERE provider = $1 AND provider_payment_id = $2`,
      [input.provider, input.providerPaymentId, input.state, input.failureCode ?? null],
    );
  }

  async countAttempts(userId: string, windowDays: number): Promise<number> {
    const rows = await this.query(
      `SELECT count(*)::int AS count FROM bursar.billing_auto_recharge_attempts
       WHERE user_id = $1 AND created_at >= CASE WHEN $2 = 0 THEN date_trunc('month', now()) ELSE now() - make_interval(days => greatest($2, 1)) END
         AND state IN ('submitted', 'processing', 'succeeded', 'action_required')`,
      [userId, windowDays],
    );
    return Number((rows[0] as Record<string, unknown> | undefined)?.count ?? 0);
  }
}
