import { z } from "zod";
import type { CallProc } from "../../../shared/postgres-types.js";
import { pgBoolean, requireRow, safeParse } from "../../../shared/postgres-validation.js";

export const DeductionRowSchema = z
  .object({
    charge_id: z.string().nullable().optional(),
    entry_id: z.string().nullable().optional(),
    amount: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
    balance_after: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
    allowance_consumed: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
    idempotent: pgBoolean.nullable().optional(),
    bucket_breakdown: z
      .record(z.string(), z.union([z.string(), z.number()] as const))
      .nullable()
      .optional(),
    error: z.string().nullable().optional(),
    user_id: z.string().optional(),
  })
  .passthrough();

const RefundRowSchema = z
  .object({
    refund_entry_id: z.string().optional(),
    user_id: z.string().optional(),
    amount: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
    new_balance: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
    bucket_breakdown: z
      .record(z.string(), z.union([z.string(), z.number()] as const))
      .nullable()
      .optional(),
    error: z.string().nullable().optional(),
  })
  .passthrough();

const RevokeRowSchema = z
  .object({
    user_id: z.string().optional(),
    amount: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
    new_balance: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
    bucket: z.string().nullable().optional(),
  })
  .passthrough();

export type DeductionRow = z.infer<typeof DeductionRowSchema>;
export type RefundRow = z.infer<typeof RefundRowSchema>;
export type RevokeRow = z.infer<typeof RevokeRowSchema>;

const UsageRecordRowSchema = z
  .object({
    charge_id: z.string().nullable().optional(),
    requested: z.union([z.string(), z.number()]).nullable().optional(),
    replayed: pgBoolean.nullable().optional(),
    error_code: z.string().nullable().optional(),
  })
  .passthrough();

export type UsageRecordRow = z.infer<typeof UsageRecordRowSchema>;

/** Typed input for the plan-aware operation charge RPC. */
export interface DeductParams {
  userId: string;
  operation: string;
  amount: string;
  idempotencyKey: string;
  feature: string | null;
  model: string | null;
  region: string | null;
  measures: string;
  dimensions: string;
  metadata: string;
}

/** Repository for credit deduction operations. */
export class DeductionRepository {
  constructor(private callproc: CallProc) {}

  /** Atomically deduct credits with allowance, entitlement, quota, and credit-policy enforcement. */
  async deductWithAllowance(params: DeductParams): Promise<DeductionRow> {
    const rows = await this.callproc("charge_usage_for_operation", [
      params.userId,
      params.operation,
      params.amount,
      params.idempotencyKey,
      params.feature,
      params.model,
      params.region,
      params.metadata,
      params.measures,
      params.dimensions,
    ]);
    const row = requireRow(rows, "DeductionRepository.deductWithAllowance") as Record<
      string,
      unknown
    >;
    const details =
      row.error_code != null
        ? undefined
        : ((
            await this.callproc("get_credit_operation_details", [
              params.userId,
              row.ledger_entry_id ?? null,
              params.idempotencyKey,
            ])
          )[0] as Record<string, unknown> | undefined);
    return safeParse(
      DeductionRowSchema,
      {
        ...row,
        user_id: params.userId,
        entry_id: row.ledger_entry_id,
        amount: row.charged,
        allowance_consumed: row.allowance_covered,
        balance_after: details?.balance_after ?? row.balance_after,
        bucket_breakdown: details?.bucket_breakdown ?? row.bucket_breakdown,
        idempotent: row.replayed,
        error: row.error_code,
      },
      "DeductionRepository.deductWithAllowance",
    );
  }

  /** Append a priced usage event without creating another balance debit. */
  async recordUsage(params: DeductParams): Promise<UsageRecordRow> {
    const rows = await this.callproc("record_usage", [
      params.userId,
      params.operation,
      params.amount,
      params.idempotencyKey,
      params.feature,
      params.model,
      params.region,
      params.metadata,
      params.measures,
      params.dimensions,
    ]);
    return safeParse(
      UsageRecordRowSchema,
      requireRow(rows, "DeductionRepository.recordUsage"),
      "DeductionRepository.recordUsage",
    );
  }

  /** Refund a previous credit deduction. */
  async refundCredits(
    entryId: string,
    amount: string | null,
    idempotencyKey: string,
    reason: string | null,
    metadata: string,
  ): Promise<RefundRow> {
    const rows = await this.callproc("refund_credit_by_entry", [
      entryId,
      amount,
      idempotencyKey,
      reason,
      metadata,
    ]);
    const row = requireRow(rows, "DeductionRepository.refundCredits") as Record<string, unknown>;
    return safeParse(
      RefundRowSchema,
      {
        ...row,
        refund_entry_id: row.entry_id,
        user_id: row.subject_id,
        new_balance: row.balance_after,
        error: row.error_code,
      },
      "DeductionRepository.refundCredits",
    );
  }

  /** Revoke credits by transaction type. */
  async revokeCreditsByEntryType(userId: string, entryType: string): Promise<RevokeRow> {
    const rows = await this.callproc("revoke_subject_credits_by_operation", [userId, entryType]);
    const row = requireRow(rows, "DeductionRepository.revokeCreditsByEntryType") as Record<
      string,
      unknown
    >;
    return safeParse(
      RevokeRowSchema,
      {
        user_id: userId,
        amount: row.revoked,
        new_balance: row.balance_after,
        bucket: null,
      },
      "DeductionRepository.revokeCreditsByEntryType",
    );
  }
}
