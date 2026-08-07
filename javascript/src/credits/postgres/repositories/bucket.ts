import { z } from "zod";
import Decimal from "decimal.js";
import type { CallProc } from "../../../shared/postgres-types.js";
import { requireRow, safeParse } from "../../../shared/postgres-validation.js";

const BucketEnvelopeRowSchema = z
  .object({
    user_id: z.string().optional(),
    buckets: z.array(z.record(z.string(), z.unknown())).optional(),
    total_balance: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
  })
  .passthrough();

const SweepRowSchema = z
  .object({
    expired_count: z.coerce.number().optional(),
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

export type BucketEnvelopeRow = z.infer<typeof BucketEnvelopeRowSchema>;
export type SweepRow = z.infer<typeof SweepRowSchema>;

/** Repository for credit bucket operations. */
export class BucketRepository {
  constructor(private callproc: CallProc) {}

  /** Fetch per-bucket credit balances for a user. */
  async getBucketBalances(userId: string): Promise<BucketEnvelopeRow> {
    const rows = await this.callproc("get_credit_bucket_balances", [userId]);
    const buckets = (rows ?? []).filter(
      (row): row is Record<string, unknown> =>
        row != null && typeof row === "object" && !Array.isArray(row),
    );
    const totalBalance = buckets
      .reduce((total, row) => total.plus(String(row.balance ?? 0)), new Decimal(0))
      .toString();
    return safeParse(
      BucketEnvelopeRowSchema,
      {
        user_id: userId,
        buckets,
        total_balance: totalBalance,
      },
      "BucketRepository.getBucketBalances",
    );
  }

  /** Sweep expired credit grants. */
  async sweepExpiredCredits(dryRun = false, userId?: string, limit = 100): Promise<SweepRow> {
    const rows = await this.callproc("sweep_expired_lots", [limit, userId ?? null, dryRun]);
    return safeParse(
      SweepRowSchema,
      requireRow(rows, "BucketRepository.sweepExpiredCredits"),
      "BucketRepository.sweepExpiredCredits",
    );
  }
}
