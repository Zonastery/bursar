import { z } from "zod";
import type { CallProc } from "./types.js";
import { pgBoolean, safeParse } from "./_shared.js";

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
    idempotent: pgBoolean.nullable().optional(),
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

/** Typed parameters for deductWithAllowance, replacing 13 positional parameters. */
export interface DeductParams {
  /** The user ID to deduct from. */
  userId: string;
  /** Amount to deduct as a decimal string. */
  amount: string;
  /** Idempotency key for replay safety. */
  idempotencyKey: string | null;
  /** Minimum balance floor after deduction. */
  minBalance: string;
  /** Model identifier for rate tracking. */
  model: string | null;
  /** JSON-encoded metadata. */
  metadata: string;
  /** Skip free-allowance consumption. */
  skipAllowance: boolean;
  /** Free-allowance window start override. */
  periodStart: string | null;
  /** Feature name for per-feature limits. */
  feature: string | null;
  /** Maximum calls for feature limit enforcement. */
  featureMaxCalls: number | null;
  /** Action when feature limit exceeded: deny, warn, notify. */
  featureOnExceed: string | null;
  /** Feature limit window start. */
  featurePeriodStart: string | null;
  /** Feature limit window end. */
  featurePeriodEnd: string | null;
}

/** Repository for credit deduction operations. */
export class DeductionRepository {
  constructor(private callproc: CallProc) {}

  /** Atomically deduct credits with allowance consumption, spend cap enforcement, and floor check. */
  async deductWithAllowance(params: DeductParams): Promise<DeductionRow> {
    const rows = await this.callproc("deduct_with_allowance", [
      params.userId,
      params.amount,
      params.idempotencyKey,
      params.minBalance,
      params.model,
      params.metadata,
      params.skipAllowance,
      params.periodStart,
      params.feature,
      params.featureMaxCalls,
      params.featureOnExceed,
      params.featurePeriodStart,
      params.featurePeriodEnd,
    ]);
    return safeParse(
      DeductionRowSchema,
      rows?.[0] ?? {},
      "DeductionRepository.deductWithAllowance",
    );
  }

  /** Refund a previous credit deduction. */
  async refundCredits(
    transactionId: string,
    amount: string | null,
    reason: string | null,
    metadata: string,
  ): Promise<RefundRow> {
    const rows = await this.callproc("refund_credits", [transactionId, amount, reason, metadata]);
    return safeParse(RefundRowSchema, rows?.[0] ?? {}, "DeductionRepository.refundCredits");
  }

  /** Revoke credits by transaction type. */
  async revokeCreditsByTxType(userId: string, txType: string): Promise<RevokeRow> {
    const rows = await this.callproc("revoke_credits_by_tx_type", [userId, txType]);
    return safeParse(RevokeRowSchema, rows?.[0] ?? {}, "DeductionRepository.revokeCreditsByTxType");
  }
}
