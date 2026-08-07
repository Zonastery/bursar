import { z } from "zod";
import type { CallProc } from "../../../shared/postgres-types.js";
import { DeductionRowSchema } from "./deduction.js";
import type { DeductionRow } from "./deduction.js";
import { pgBoolean, requireRow, safeParse } from "../../../shared/postgres-validation.js";

const decimal = z.union([z.string().min(1), z.number().finite()] as const);

const LeaseRowSchema = z
  .object({
    lease_id: z.string().nullable().optional(),
    user_id: z.string().min(1),
    amount: decimal.nullable(),
    available: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
    reserved: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
    billing_mode: z.string().optional(),
    minimum_balance: decimal.nullable(),
    expires_at: z
      .union([z.string(), z.date().transform((value) => value.toISOString())])
      .nullable(),
    error: z.string().min(1).nullable(),
  })
  .superRefine((row, context) => {
    if (
      row.error === null &&
      (row.lease_id === null ||
        row.amount === null ||
        row.minimum_balance === null ||
        row.expires_at === null)
    ) {
      context.addIssue({
        code: "custom",
        message: "successful lease acquisition requires lease and policy fields",
      });
    }
  });

const ReleaseRowSchema = z
  .object({
    released: pgBoolean.nullable().optional(),
    reason: z.string().nullable().optional(),
  })
  .passthrough();

const LeasePricingContextRowSchema = z
  .object({
    catalog_revision_no: z.coerce.number(),
    plan_id: z.string().nullable().optional(),
    plan_key: z.string().nullable().optional(),
    rate_card: z.string().nullable().optional(),
  })
  .passthrough();

export type LeaseRow = z.infer<typeof LeaseRowSchema>;
export type ReleaseRow = z.infer<typeof ReleaseRowSchema>;
export type LeasePricingContextRow = z.infer<typeof LeasePricingContextRowSchema>;

/** Repository for lease lifecycle operations (admission control). */
export class LeaseRepository {
  constructor(private callproc: CallProc) {}

  /** Atomically acquire a lease (hold) — admission control. */
  async createLease(params: {
    userId: string;
    amount: string;
    operationType: string;
    idempotencyKey: string;
    ttlSeconds: number;
    metadata: string;
    feature: string | null;
    measures: string;
    dimensions: string;
    minimumBalance: string | null;
    maxConcurrent: number | null;
  }): Promise<LeaseRow> {
    const rows = await this.callproc("create_lease_for_operation", [
      params.userId,
      params.operationType,
      params.amount,
      params.idempotencyKey,
      `${params.ttlSeconds} seconds`,
      params.metadata,
      params.feature,
      params.measures,
      params.dimensions,
      params.minimumBalance,
      params.maxConcurrent,
    ]);
    const row = requireRow(rows, "LeaseRepository.createLease") as Record<string, unknown>;
    const lease =
      row.lease_id == null
        ? null
        : ((await this.callproc("get_credit_lease", [params.userId, row.lease_id]))[0] as
            | Record<string, unknown>
            | undefined);
    return safeParse(
      LeaseRowSchema,
      {
        ...row,
        user_id: params.userId,
        amount: row.reserved_amount,
        expires_at: lease?.expires_at ?? null,
        minimum_balance: lease?.minimum_balance ?? null,
        error: row.error_code,
      },
      "LeaseRepository.createLease",
    );
  }

  /** Charge the actual cost against a lease and mark it settled. */
  async settleLease(params: {
    userId: string;
    leaseId: string;
    amount: string;
    idempotencyKey: string;
    feature: string | null;
    model: string | null;
    region: string | null;
    measures: string;
    dimensions: string;
    metadata: string;
  }): Promise<DeductionRow> {
    const rows = await this.callproc("settle_lease", [
      params.userId,
      params.leaseId,
      params.amount,
      params.idempotencyKey,
      params.feature,
      params.model,
      params.region,
      params.measures,
      params.dimensions,
      params.metadata,
    ]);
    const row = requireRow(rows, "LeaseRepository.settleLease") as Record<string, unknown>;
    const charge =
      row.error_code == null
        ? ((
            await this.callproc("get_credit_operation_details", [
              params.userId,
              row.ledger_entry_id ?? null,
              params.idempotencyKey,
            ])
          )[0] as Record<string, unknown> | undefined)
        : undefined;
    return safeParse(
      DeductionRowSchema,
      {
        ...row,
        user_id: params.userId,
        entry_id: row.ledger_entry_id,
        amount: row.settled_amount,
        allowance_consumed: charge?.allowance_covered ?? "0",
        balance_after: charge?.balance_after ?? null,
        idempotent: row.replayed,
        error: row.error_code,
      },
      "LeaseRepository.settleLease",
    );
  }

  /** Return the immutable pricing context captured at lease admission. */
  async getPricingContext(userId: string, leaseId: string): Promise<LeasePricingContextRow | null> {
    const rows = await this.callproc("get_credit_lease_pricing_context", [userId, leaseId]);
    if (!rows?.length) return null;
    return safeParse(LeasePricingContextRowSchema, rows[0], "LeaseRepository.getPricingContext");
  }

  /** Release a lease without charging — idempotent. */
  async releaseLease(userId: string, leaseId: string): Promise<ReleaseRow> {
    const rows = await this.callproc("release_lease", [userId, leaseId]);
    const value = requireRow(rows, "LeaseRepository.releaseLease");
    const result = value as { released?: boolean };
    const reason = typeof value === "string" ? value : result.released === true ? "released" : null;
    return safeParse(
      ReleaseRowSchema,
      {
        released: reason === "released",
        reason,
      },
      "LeaseRepository.releaseLease",
    );
  }

  /** Extend an active lease without changing its captured policy snapshot. */
  async renewLease(userId: string, leaseId: string, ttlSeconds: number): Promise<LeaseRow> {
    const rows = await this.callproc("renew_lease", [userId, leaseId, `${ttlSeconds} seconds`]);
    const row = requireRow(rows, "LeaseRepository.renewLease") as Record<string, unknown>;
    const lease =
      row.error_code == null && row.lease_id != null
        ? ((await this.callproc("get_credit_lease", [userId, row.lease_id]))[0] as
            | Record<string, unknown>
            | undefined)
        : undefined;
    return safeParse(
      LeaseRowSchema,
      {
        ...row,
        ...lease,
        user_id: userId,
        amount: row.reserved_amount,
        expires_at: lease?.expires_at ?? null,
        minimum_balance: lease?.minimum_balance ?? null,
        error: row.error_code,
      },
      "LeaseRepository.renewLease",
    );
  }

  /** Expire a bounded batch of leases and release their reservations. */
  async expireLeases(limit: number): Promise<number> {
    const rows = await this.callproc("expire_leases", [limit]);
    const row = requireRow(rows, "LeaseRepository.expireLeases") as Record<string, unknown>;
    return Number(row.expired ?? 0);
  }
}
