import { z } from "zod";
import type { CallProc } from "../../../shared/postgres-types.js";
import { DeductionRowSchema } from "./deduction.js";
import type { DeductionRow } from "./deduction.js";
import {
  optionalRecordRow,
  postgresUuid,
  requireRow,
  safeParse,
} from "../../../shared/postgres-validation.js";
import { StoreError } from "../../../errors.js";
import Decimal from "decimal.js";

const decimal = z.union([z.string().min(1), z.number().finite()] as const);
const timestamp = z.union([z.string().min(1), z.date()] as const).transform((value, context) => {
  const parsed = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    context.addIssue({ code: "custom", message: "expected a valid timestamp" });
    return z.NEVER;
  }
  return parsed.toISOString();
});
const safeInteger = z
  .union([z.number().int(), z.string().regex(/^\d+$/)] as const)
  .transform(Number)
  .refine(Number.isSafeInteger, "expected a safe integer");
const leaseStatus = z.enum(["active", "settling", "settled", "released", "expired"]);

const LeaseMutationRpcRowSchema = z
  .object({
    lease_id: postgresUuid.nullable(),
    status: leaseStatus,
    reserved_amount: decimal,
    error_code: z.string().min(1).nullable(),
  })
  .strict()
  .superRefine((row, context) => {
    if (row.error_code === null && (row.lease_id === null || row.status !== "active")) {
      context.addIssue({
        code: "custom",
        message: "successful lease mutations require an active lease",
      });
    }
  });

const SettleLeaseRpcRowSchema = z
  .object({
    ledger_entry_id: postgresUuid.nullable(),
    charge_id: postgresUuid.nullable(),
    settled_amount: decimal,
    replayed: z.boolean(),
    error_code: z.string().min(1).nullable(),
  })
  .strict()
  .superRefine((row, context) => {
    if (row.error_code === null && row.charge_id === null) {
      context.addIssue({
        code: "custom",
        message: "successful lease settlement requires a charge receipt",
      });
    }
    if (
      row.error_code !== null &&
      (row.ledger_entry_id !== null || row.charge_id !== null || row.replayed)
    ) {
      context.addIssue({
        code: "custom",
        message: "failed lease settlement cannot expose committed fields",
      });
    }
  });

const LeaseRowSchema = z
  .object({
    lease_id: postgresUuid.nullable(),
    user_id: postgresUuid,
    amount: decimal.nullable(),
    minimum_balance: decimal.nullable(),
    expires_at: timestamp.nullable(),
    error: z.string().min(1).nullable(),
  })
  .strict()
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
    if (
      row.error !== null &&
      (row.lease_id !== null ||
        row.amount !== null ||
        row.minimum_balance !== null ||
        row.expires_at !== null)
    ) {
      context.addIssue({
        code: "custom",
        message: "failed lease mutations cannot expose committed fields",
      });
    }
  });

const ReleaseRowSchema = z
  .object({
    released: z.boolean(),
    reason: leaseStatus.or(z.literal("missing_lease")).nullable(),
  })
  .strict();

const LeasePricingContextRowSchema = z
  .object({
    catalog_revision_no: safeInteger,
    plan_id: postgresUuid.nullable(),
    plan_key: z.string().min(1).nullable(),
    rate_card: z.string().min(1).nullable(),
  })
  .strict()
  .superRefine((row, context) => {
    if ((row.plan_id === null) !== (row.plan_key === null)) {
      context.addIssue({ code: "custom", message: "lease plan identity fields are inconsistent" });
    }
  });

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
    const row = safeParse(
      LeaseMutationRpcRowSchema,
      requireRow(rows, "LeaseRepository.createLease"),
      "LeaseRepository.createLease",
    );
    if (row.error_code !== null) {
      return safeParse(
        LeaseRowSchema,
        {
          lease_id: null,
          user_id: params.userId,
          amount: null,
          expires_at: null,
          minimum_balance: null,
          error: row.error_code,
        },
        "LeaseRepository.createLease",
      );
    }
    const lease = optionalRecordRow(
      await this.callproc("get_credit_lease", [params.userId, row.lease_id]),
      "LeaseRepository.createLease.details",
    );
    return safeParse(
      LeaseRowSchema,
      {
        lease_id: row.lease_id,
        user_id: params.userId,
        amount: row.reserved_amount,
        expires_at: lease?.expires_at,
        minimum_balance: lease?.minimum_balance,
        error: null,
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
    const row = safeParse(
      SettleLeaseRpcRowSchema,
      requireRow(rows, "LeaseRepository.settleLease"),
      "LeaseRepository.settleLease",
    );
    if (row.error_code === null && !new Decimal(row.settled_amount).equals(params.amount)) {
      throw new StoreError(
        "LeaseRepository.settleLease: committed amount differs from the request",
        {
          indeterminate: true,
        },
      );
    }
    const charge =
      row.error_code == null
        ? optionalRecordRow(
            await this.callproc("get_credit_operation_details", [
              params.userId,
              row.ledger_entry_id ?? null,
              params.idempotencyKey,
            ]),
            "LeaseRepository.settleLease.details",
          )
        : null;
    return safeParse(
      DeductionRowSchema,
      {
        charge_id: row.charge_id,
        user_id: params.userId,
        entry_id: row.ledger_entry_id,
        amount: row.error_code === null ? row.settled_amount : params.amount,
        allowance_consumed: row.error_code === null ? charge?.allowance_covered : "0",
        balance_after: row.error_code === null ? charge?.balance_after : null,
        bucket_breakdown: row.error_code === null ? charge?.bucket_breakdown : null,
        idempotent: row.replayed,
        error: row.error_code,
      },
      "LeaseRepository.settleLease",
    );
  }

  /** Return the immutable pricing context captured at lease admission. */
  async getPricingContext(userId: string, leaseId: string): Promise<LeasePricingContextRow | null> {
    const rows = await this.callproc("get_credit_lease_pricing_context", [userId, leaseId]);
    const row = optionalRecordRow(rows, "LeaseRepository.getPricingContext");
    return row === null
      ? null
      : safeParse(LeasePricingContextRowSchema, row, "LeaseRepository.getPricingContext");
  }

  /** Release a lease without charging — idempotent. */
  async releaseLease(userId: string, leaseId: string): Promise<ReleaseRow> {
    const rows = await this.callproc("release_lease", [userId, leaseId]);
    const status = safeParse(
      leaseStatus.or(z.literal("missing_lease")),
      requireRow(rows, "LeaseRepository.releaseLease"),
      "LeaseRepository.releaseLease",
    );
    return safeParse(
      ReleaseRowSchema,
      {
        released: status === "released",
        reason: status === "released" ? null : status,
      },
      "LeaseRepository.releaseLease",
    );
  }

  /** Extend an active lease without changing its captured policy snapshot. */
  async renewLease(userId: string, leaseId: string, ttlSeconds: number): Promise<LeaseRow> {
    const rows = await this.callproc("renew_lease", [userId, leaseId, `${ttlSeconds} seconds`]);
    const row = safeParse(
      LeaseMutationRpcRowSchema,
      requireRow(rows, "LeaseRepository.renewLease"),
      "LeaseRepository.renewLease",
    );
    if (row.error_code !== null) {
      return safeParse(
        LeaseRowSchema,
        {
          lease_id: null,
          user_id: userId,
          amount: null,
          expires_at: null,
          minimum_balance: null,
          error: row.error_code,
        },
        "LeaseRepository.renewLease",
      );
    }
    const lease = optionalRecordRow(
      await this.callproc("get_credit_lease", [userId, row.lease_id]),
      "LeaseRepository.renewLease.details",
    );
    return safeParse(
      LeaseRowSchema,
      {
        lease_id: row.lease_id,
        user_id: userId,
        amount: row.reserved_amount,
        expires_at: lease?.expires_at,
        minimum_balance: lease?.minimum_balance,
        error: null,
      },
      "LeaseRepository.renewLease",
    );
  }

  /** Expire a bounded batch of leases and release their reservations. */
  async expireLeases(limit: number): Promise<number> {
    const rows = await this.callproc("expire_leases", [limit]);
    const row = safeParse(
      z.object({ expired: z.number().int().nonnegative() }).strict(),
      requireRow(rows, "LeaseRepository.expireLeases"),
      "LeaseRepository.expireLeases",
    );
    return row.expired;
  }
}
