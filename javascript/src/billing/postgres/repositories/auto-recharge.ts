import type { QueryFn } from "../../../shared/postgres-types.js";
import type { BillingAutoRechargeAttempt, BillingAutoRechargeProfile } from "../../types/index.js";

function iso(value: unknown): string {
  return new Date(String(value)).toISOString();
}

function profileFromRow(row: Record<string, unknown>): BillingAutoRechargeProfile {
  return {
    userId: String(row.subject_id),
    enabled: Boolean(row.enabled),
    armed: Boolean(row.armed),
    state: String(row.state) as BillingAutoRechargeProfile["state"],
    provider: row.provider == null ? null : String(row.provider),
    topupId: row.topup_id == null ? null : String(row.topup_id),
    quantity: Number(row.quantity),
    threshold: Number(row.threshold),
    maxChargesPerWindow:
      row.max_charges_per_window == null ? null : Number(row.max_charges_per_window),
    windowUnit: String(row.window_unit) as BillingAutoRechargeProfile["windowUnit"],
    windowCount: Number(row.window_count),
    windowAnchor: String(row.window_anchor) as BillingAutoRechargeProfile["windowAnchor"],
    windowTimezone: String(row.window_timezone),
    updatedAt: row.updated_at == null ? null : iso(row.updated_at),
  };
}

function attemptFromRow(row: Record<string, unknown>): BillingAutoRechargeAttempt {
  return {
    id: String(row.id),
    userId: String(row.subject_id),
    provider: String(row.provider),
    idempotencyKey: String(row.idempotency_key),
    providerAttemptId: row.provider_attempt_id == null ? null : String(row.provider_attempt_id),
    topupId: String(row.topup_id),
    quantity: Number(row.quantity),
    state: String(row.state) as BillingAutoRechargeAttempt["state"],
    windowStart: iso(row.window_start),
    windowEnd: iso(row.window_end),
    quotedAmountMinor: row.quoted_amount_minor == null ? null : Number(row.quoted_amount_minor),
    currency: row.currency == null ? null : String(row.currency),
    failureCode: row.failure_code == null ? null : String(row.failure_code),
    failureMessage: row.failure_message == null ? null : String(row.failure_message),
    metadata:
      row.metadata != null && typeof row.metadata === "object" && !Array.isArray(row.metadata)
        ? (row.metadata as Record<string, unknown>)
        : {},
    createdAt: iso(row.created_at),
    updatedAt: iso(row.updated_at),
  };
}

export class BillingAutoRechargeRepository {
  constructor(private readonly query: QueryFn) {}

  async getProfile(userId: string): Promise<BillingAutoRechargeProfile | null> {
    const rows = await this.query("SELECT * FROM bursar.get_auto_recharge_profile($1::uuid)", [
      userId,
    ]);
    const row = rows[0] as Record<string, unknown> | undefined;
    return row?.subject_id == null ? null : profileFromRow(row);
  }

  async upsertProfile(profile: BillingAutoRechargeProfile): Promise<void> {
    const rows = await this.query(
      `SELECT bursar.upsert_auto_recharge_profile(
         $1::uuid, $2, $3, $4::uuid, $5, $6, $7, $8, $9, $10, $11
       ) AS updated`,
      [
        profile.userId,
        profile.enabled,
        profile.provider,
        profile.topupId,
        profile.quantity,
        profile.threshold,
        profile.maxChargesPerWindow,
        profile.windowUnit,
        profile.windowCount,
        profile.windowAnchor,
        profile.windowTimezone,
      ],
    );
    if (!(rows[0] as Record<string, unknown> | undefined)?.updated) {
      throw new Error(`auto-recharge profile update rejected: ${profile.userId}`);
    }
  }

  async claimAttempt(input: {
    userId: string;
    idempotencyKey: string;
  }): Promise<BillingAutoRechargeAttempt | null> {
    const rows = await this.query(
      "SELECT * FROM bursar.claim_auto_recharge_attempt($1::uuid, $2)",
      [input.userId, input.idempotencyKey],
    );
    const row = rows[0] as Record<string, unknown> | undefined;
    return row?.id == null ? null : attemptFromRow(row);
  }

  private async advanceAttempt(input: {
    id: string;
    state: string;
    providerAttemptId?: string | null;
    failureCode?: string | null;
    failureMessage?: string | null;
    metadata?: Record<string, unknown>;
  }): Promise<void> {
    const currentRows = await this.query(
      "SELECT * FROM bursar.get_auto_recharge_attempt($1::uuid)",
      [input.id],
    );
    const current = String((currentRows[0] as Record<string, unknown> | undefined)?.state ?? "");
    const paths: Record<string, Record<string, string[]>> = {
      claimed: {
        submitted: ["submitted"],
        processing: ["submitted", "processing"],
        succeeded: ["submitted", "processing", "succeeded"],
        failed: ["submitted", "processing", "failed"],
        unknown: ["submitted", "processing", "unknown"],
      },
      submitted: {
        submitted: [],
        processing: ["processing"],
        succeeded: ["processing", "succeeded"],
        failed: ["processing", "failed"],
        unknown: ["processing", "unknown"],
      },
      processing: {
        processing: [],
        succeeded: ["succeeded"],
        failed: ["failed"],
        unknown: ["unknown"],
      },
      unknown: { unknown: [], processing: ["processing"], action_required: ["action_required"] },
      action_required: { action_required: [], processing: ["processing"] },
    };
    const path = paths[current]?.[input.state];
    if (!path) {
      throw new Error(`auto-recharge attempt transition rejected: ${input.id}`);
    }
    for (const state of path) {
      const rows = await this.query(
        `SELECT bursar.advance_auto_recharge_attempt(
         $1::uuid,
         $2::bursar.recharge_attempt_status,
         $3,
         $4,
         $5,
         $6::jsonb
       ) AS advanced`,
        [
          input.id,
          state,
          input.providerAttemptId ?? null,
          input.failureCode ?? null,
          input.failureMessage ?? null,
          JSON.stringify(input.metadata ?? {}),
        ],
      );
      if (!(rows[0] as Record<string, unknown> | undefined)?.advanced) {
        throw new Error(`auto-recharge attempt transition rejected: ${input.id}`);
      }
    }
  }

  async updateAttempt(input: {
    id: string;
    state: string;
    providerAttemptId?: string | null;
    failureCode?: string | null;
    failureMessage?: string | null;
    metadata?: Record<string, unknown>;
  }): Promise<void> {
    return this.advanceAttempt(input);
  }

  async updateAttemptByProviderPayment(input: {
    provider: string;
    providerPaymentId: string;
    state: string;
    failureCode?: string | null;
    failureMessage?: string | null;
  }): Promise<void> {
    const rows = await this.query(
      `SELECT * FROM bursar.get_auto_recharge_attempt_by_provider($1, $2)`,
      [input.provider, input.providerPaymentId],
    );
    for (const row of rows as Array<Record<string, unknown>>) {
      if (row.id == null) {
        continue;
      }
      await this.advanceAttempt({
        id: String(row.id),
        state: input.state,
        providerAttemptId: input.providerPaymentId,
        failureCode: input.failureCode,
        failureMessage: input.failureMessage,
      });
    }
  }

  async countAttempts(userId: string, since: string | Date): Promise<number> {
    const sinceDate = since instanceof Date ? since : new Date(since);
    if (Number.isNaN(sinceDate.getTime())) {
      throw new TypeError("auto-recharge attempt boundary must be a valid instant");
    }
    const rows = await this.query(
      `SELECT bursar.count_auto_recharge_attempts($1::uuid, $2::timestamptz) AS count`,
      [userId, sinceDate.toISOString()],
    );
    return Number((rows[0] as Record<string, unknown> | undefined)?.count ?? 0);
  }
}
