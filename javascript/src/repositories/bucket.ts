import { z } from "zod";
import type { CallProc } from "./types.js";
import { pgBoolean, safeParse } from "./_shared.js";

export const BucketBalanceRowSchema = z
  .object({
    bucket_key: z.string().optional(),
    label: z.string().optional(),
    priority: z.number().optional(),
    expires: pgBoolean.nullable().optional(),
    balance: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
  })
  .passthrough();

export const BucketEnvelopeRowSchema = z
  .object({
    user_id: z.string().optional(),
    buckets: z.array(z.record(z.string(), z.unknown())).optional(),
    total_balance: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
  })
  .passthrough();

export const SweepRowSchema = z
  .object({
    expired_count: z.number().optional(),
    expired_amount: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
    expired_by_bucket: z
      .record(z.string(), z.union([z.string(), z.number()] as const))
      .nullable()
      .optional(),
  })
  .passthrough();

export type BucketBalanceRow = z.infer<typeof BucketBalanceRowSchema>;
export type BucketEnvelopeRow = z.infer<typeof BucketEnvelopeRowSchema>;
export type SweepRow = z.infer<typeof SweepRowSchema>;

/** Repository for credit bucket operations. */
export class BucketRepository {
  constructor(private callproc: CallProc) {}

  /** Fetch per-bucket credit balances for a user. */
  async getBucketBalances(userId: string): Promise<BucketEnvelopeRow> {
    const rows = await this.callproc("get_user_credit_buckets", [userId]);
    return safeParse(
      BucketEnvelopeRowSchema,
      rows?.[0] ?? {},
      "BucketRepository.getBucketBalances",
    );
  }

  /** Sweep expired credit grants. */
  async sweepExpiredCredits(dryRun: boolean, userId: string | null): Promise<SweepRow> {
    const rows = await this.callproc("expire_credits", [dryRun, userId]);
    return safeParse(SweepRowSchema, rows?.[0] ?? {}, "BucketRepository.sweepExpiredCredits");
  }
}
