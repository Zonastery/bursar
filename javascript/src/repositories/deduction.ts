import { z } from "zod";
import type { CallProc } from "./types.js";

export const DeductionRowSchema = z
  .object({
    transaction_id: z.string().optional(),
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
    idempotent: z
      .union([z.boolean(), z.string()] as const)
      .nullable()
      .optional(),
    cap_warning: z.string().nullable().optional(),
    feature_limit_warning: z.string().nullable().optional(),
    bucket_breakdown: z
      .record(z.string(), z.union([z.string(), z.number()] as const))
      .nullable()
      .optional(),
    error: z.string().optional(),
    user_id: z.string().optional(),
  })
  .passthrough();

export const RefundRowSchema = z
  .object({
    refund_transaction_id: z.string().optional(),
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
    error: z.string().optional(),
  })
  .passthrough();

export const RevokeRowSchema = z
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

export class DeductionRepository {
  constructor(private callproc: CallProc) {}

  async deductWithAllowance(
    userId: string,
    amount: string,
    idempotencyKey: string | null,
    minBalance: string,
    model: string | null,
    metadata: string,
    skipAllowance: boolean,
    periodStart: string | null,
    feature: string | null,
    featureMaxCalls: number | null,
    featureOnExceed: string | null,
    featurePeriodStart: string | null,
    featurePeriodEnd: string | null,
  ): Promise<DeductionRow> {
    const rows = await this.callproc("deduct_with_allowance", [
      userId,
      amount,
      idempotencyKey,
      minBalance,
      model,
      metadata,
      skipAllowance,
      periodStart,
      feature,
      featureMaxCalls,
      featureOnExceed,
      featurePeriodStart,
      featurePeriodEnd,
    ]);
    return DeductionRowSchema.parse(rows?.[0] ?? {});
  }

  async refundCredits(
    transactionId: string,
    amount: string | null,
    reason: string | null,
    metadata: string,
  ): Promise<RefundRow> {
    const rows = await this.callproc("refund_credits", [transactionId, amount, reason, metadata]);
    return RefundRowSchema.parse(rows?.[0] ?? {});
  }

  async revokeCreditsByTxType(userId: string, txType: string): Promise<RevokeRow> {
    const rows = await this.callproc("revoke_credits_by_tx_type", [userId, txType]);
    return RevokeRowSchema.parse(rows?.[0] ?? {});
  }
}
