import { z } from "zod";
import type { CallProc } from "../../../shared/postgres-types.js";
import {
  optionalRecordRow,
  postgresUuid,
  requireRow,
  safeParse,
} from "../../../shared/postgres-validation.js";

const decimal = z.union([z.string().min(1), z.number().finite()] as const);

export const DeductionRowSchema = z
  .object({
    charge_id: postgresUuid.nullable(),
    entry_id: postgresUuid.nullable(),
    amount: decimal,
    balance_after: decimal.nullable(),
    allowance_consumed: decimal,
    idempotent: z.boolean(),
    bucket_breakdown: z.record(z.string().min(1), decimal).nullable(),
    error: z.string().min(1).nullable(),
    user_id: postgresUuid,
  })
  .strict()
  .superRefine((row, context) => {
    if (row.error === null && (row.charge_id === null || row.balance_after === null)) {
      context.addIssue({
        code: "custom",
        message: "successful usage charges require a receipt and committed balance",
      });
    }
    if (
      row.error !== null &&
      (row.charge_id !== null ||
        row.entry_id !== null ||
        row.balance_after !== null ||
        row.idempotent)
    ) {
      context.addIssue({
        code: "custom",
        message: "failed usage charges cannot expose committed fields",
      });
    }
  });

const ChargeRpcRowSchema = z
  .object({
    charge_id: postgresUuid.nullable(),
    ledger_entry_id: postgresUuid.nullable(),
    charged: decimal,
    allowance_covered: decimal,
    replayed: z.boolean(),
    error_code: z.string().min(1).nullable(),
  })
  .strict()
  .superRefine((row, context) => {
    if (row.error_code === null && row.charge_id === null) {
      context.addIssue({ code: "custom", message: "successful usage charges require a receipt" });
    }
    if (
      row.error_code !== null &&
      (row.charge_id !== null || row.ledger_entry_id !== null || row.replayed)
    ) {
      context.addIssue({
        code: "custom",
        message: "failed usage charges cannot expose committed fields",
      });
    }
  });

const RefundRowSchema = z
  .object({
    refund_entry_id: postgresUuid.nullable(),
    user_id: postgresUuid.nullable(),
    amount: decimal.nullable(),
    new_balance: decimal.nullable(),
    bucket_breakdown: z.record(z.string().min(1), decimal).nullable(),
    error: z.string().min(1).nullable(),
  })
  .strict()
  .superRefine((row, context) => {
    if (
      row.error === null &&
      (row.refund_entry_id === null ||
        row.user_id === null ||
        row.amount === null ||
        row.new_balance === null)
    ) {
      context.addIssue({
        code: "custom",
        message: "successful refunds require identity and balance fields",
      });
    }
    if (row.error !== null && (row.refund_entry_id !== null || row.bucket_breakdown !== null)) {
      context.addIssue({
        code: "custom",
        message: "failed refunds cannot expose committed fields",
      });
    }
  });

const RefundRpcRowSchema = z
  .object({
    entry_id: postgresUuid.nullable(),
    subject_id: postgresUuid.nullable(),
    amount: decimal.nullable(),
    balance_after: decimal.nullable(),
    replayed: z.boolean(),
    error_code: z.string().min(1).nullable(),
  })
  .strict();

const RevokeRowSchema = z
  .object({
    user_id: postgresUuid,
    entry_type: z.string().min(1),
    revoked: decimal,
    balance_after: decimal.nullable(),
    error_code: z.string().min(1).nullable(),
  })
  .strict()
  .superRefine((row, context) => {
    if (row.error_code === null && row.balance_after === null) {
      context.addIssue({
        code: "custom",
        message: "successful revocations require a committed balance",
      });
    }
  });

export type DeductionRow = z.infer<typeof DeductionRowSchema>;
export type RefundRow = z.infer<typeof RefundRowSchema>;
export type RevokeRow = z.infer<typeof RevokeRowSchema>;

const UsageRecordRowSchema = z
  .object({
    charge_id: postgresUuid.nullable(),
    requested: decimal,
    replayed: z.boolean(),
    error_code: z.string().min(1).nullable(),
  })
  .strict()
  .superRefine((row, context) => {
    if (row.error_code === null && row.charge_id === null) {
      context.addIssue({
        code: "custom",
        message: "successful usage records require a charge receipt",
      });
    }
    if (row.error_code !== null && (row.charge_id !== null || row.replayed)) {
      context.addIssue({
        code: "custom",
        message: "failed usage records cannot expose committed fields",
      });
    }
  });

const UsageRecordRpcRowSchema = z
  .object({
    charge_id: postgresUuid.nullable(),
    requested: decimal,
    ledger_entry_id: postgresUuid.nullable(),
    charged: decimal,
    allowance_covered: decimal,
    replayed: z.boolean(),
    error_code: z.string().min(1).nullable(),
  })
  .strict();

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
    const row = safeParse(
      ChargeRpcRowSchema,
      requireRow(rows, "DeductionRepository.deductWithAllowance"),
      "DeductionRepository.deductWithAllowance",
    );
    const details =
      row.error_code != null
        ? null
        : optionalRecordRow(
            await this.callproc("get_credit_operation_details", [
              params.userId,
              row.ledger_entry_id ?? null,
              params.idempotencyKey,
            ]),
            "DeductionRepository.deductWithAllowance.details",
          );
    return safeParse(
      DeductionRowSchema,
      {
        charge_id: row.charge_id,
        user_id: params.userId,
        entry_id: row.ledger_entry_id,
        amount: row.charged,
        allowance_consumed: row.allowance_covered,
        balance_after: row.error_code === null ? details?.balance_after : null,
        bucket_breakdown: row.error_code === null ? details?.bucket_breakdown : null,
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
    const row = safeParse(
      UsageRecordRpcRowSchema,
      requireRow(rows, "DeductionRepository.recordUsage"),
      "DeductionRepository.recordUsage",
    );
    return safeParse(
      UsageRecordRowSchema,
      {
        charge_id: row.charge_id,
        requested: row.requested,
        replayed: row.replayed,
        error_code: row.error_code,
      },
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
    const row = safeParse(
      RefundRpcRowSchema,
      requireRow(rows, "DeductionRepository.refundCredits"),
      "DeductionRepository.refundCredits",
    );
    return safeParse(
      RefundRowSchema,
      {
        refund_entry_id: row.entry_id,
        user_id: row.subject_id,
        amount: row.amount,
        new_balance: row.balance_after,
        bucket_breakdown: null,
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
        entry_type: entryType,
        revoked: row.revoked,
        balance_after: row.balance_after,
        error_code: row.error_code,
      },
      "DeductionRepository.revokeCreditsByEntryType",
    );
  }
}
