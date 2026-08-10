import { z } from "zod";
import { Decimal } from "decimal.js";
import type { CallProc } from "../../../shared/postgres-types.js";
import { postgresUuid, requireRow, safeParse } from "../../../shared/postgres-validation.js";

const decimal = z.union([z.string().min(1), z.number().finite()] as const);

const BucketRowSchema = z
  .object({
    bucket_key: z.string().min(1),
    label: z.string().min(1),
    priority: z.number().int().nonnegative(),
    expires: z.boolean(),
    balance: decimal,
  })
  .strict();

const BucketEnvelopeRowSchema = z
  .object({
    user_id: postgresUuid,
    buckets: z.array(BucketRowSchema),
    total_balance: decimal,
  })
  .strict();

const SweepRowSchema = z
  .object({
    expired_count: z.number().int().nonnegative(),
    expired_amount: decimal,
    expired_by_bucket: z.record(z.string().min(1), decimal),
  })
  .strict();

export type BucketEnvelopeRow = z.infer<typeof BucketEnvelopeRowSchema>;
export type SweepRow = z.infer<typeof SweepRowSchema>;

/** Repository for credit bucket operations. */
export class BucketRepository {
  constructor(private callproc: CallProc) {}

  /** Fetch per-bucket credit balances for a user. */
  async getBucketBalances(userId: string): Promise<BucketEnvelopeRow> {
    const rows = await this.callproc("get_credit_bucket_balances", [userId]);
    const buckets = (rows ?? []).map((row) =>
      safeParse(BucketRowSchema, row, "BucketRepository.getBucketBalances"),
    );
    const totalBalance = buckets
      .reduce((total, row) => total.plus(row.balance), new Decimal(0))
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
