import { z } from "zod";
import Decimal from "decimal.js";
import type { CallProc } from "../../../shared/postgres-types.js";
import {
  pgBoolean,
  postgresUuid,
  requireRow,
  safeParse,
} from "../../../shared/postgres-validation.js";

const decimal = z.union([z.string().min(1), z.number().finite()] as const);

const BalanceRowSchema = z
  .object({
    user_id: z.string().min(1),
    balance: decimal,
    lifetime_purchased: decimal,
  })
  .strict();

const AddCreditsRowSchema = z
  .object({
    entry_id: z.string().nullable(),
    user_id: z.string().min(1),
    amount: decimal,
    new_balance: decimal.nullable(),
    lifetime_purchased: decimal.nullable(),
    bucket: z.string().min(1).nullable(),
    idempotent: pgBoolean,
    error: z.string().min(1).nullable(),
  })
  .strict()
  .superRefine((row, context) => {
    if (
      row.error === null &&
      (row.entry_id === null || row.new_balance === null || row.lifetime_purchased === null)
    ) {
      context.addIssue({
        code: "custom",
        message: "successful credit postings require entry and balance fields",
      });
    }
    const amount = new Decimal(row.amount);
    if (row.error === null && amount.isPositive() && row.bucket === null) {
      context.addIssue({
        code: "custom",
        message: "successful positive credit postings require a destination bucket",
      });
    }
    if (row.error === null && !amount.isPositive() && row.bucket !== null) {
      context.addIssue({
        code: "custom",
        message: "debit postings cannot claim a single destination bucket",
      });
    }
    if (
      row.error !== null &&
      (row.entry_id !== null ||
        row.new_balance !== null ||
        row.lifetime_purchased !== null ||
        row.bucket !== null ||
        row.idempotent)
    ) {
      context.addIssue({
        code: "custom",
        message: "failed credit postings cannot expose committed result fields",
      });
    }
  });

const AvailableRowSchema = z
  .object({
    balance: decimal,
    reserved: decimal,
    available: decimal,
  })
  .strict();

const GrantProgramAwardRowSchema = z
  .object({
    grant_event_id: postgresUuid.nullable(),
    grant_award_id: postgresUuid.nullable(),
    recipient_subject_id: postgresUuid.nullable(),
    ledger_entry_id: postgresUuid.nullable(),
    amount: decimal.nullable(),
    replayed: z.boolean(),
    error_code: z.string().min(1).nullable(),
  })
  .strict()
  .superRefine((row, context) => {
    const award = [
      row.grant_event_id,
      row.grant_award_id,
      row.recipient_subject_id,
      row.ledger_entry_id,
      row.amount,
    ];
    if (row.error_code === null && award.some((value) => value === null)) {
      context.addIssue({
        code: "custom",
        message: "successful grant awards require committed fields",
      });
    }
    if (row.error_code !== null && (award.some((value) => value !== null) || row.replayed)) {
      context.addIssue({
        code: "custom",
        message: "failed grant awards cannot expose committed fields",
      });
    }
  });

export type BalanceRow = z.infer<typeof BalanceRowSchema>;
export type AddCreditsRow = z.infer<typeof AddCreditsRowSchema>;
export type AvailableRow = z.infer<typeof AvailableRowSchema>;
export type GrantProgramAwardRow = z.infer<typeof GrantProgramAwardRowSchema>;

/** Repository for user credit balance operations.
 *
 * All methods call Postgres RPCs via the callproc function.
 *
 * Reads may return no row for a new account. Mutations require the database's
 * single-row result envelope and fail closed when that contract is violated.
 */
export class BalanceRepository {
  constructor(private callproc: CallProc) {}

  /** Fetch a user's credit balance. Returns null for new users with no balance row. */
  async getBalance(userId: string): Promise<BalanceRow | null> {
    const rows = await this.callproc("get_credit_state", [userId]);
    if (!rows || rows.length === 0) return null;
    const row = requireRow(rows, "BalanceRepository.getBalance") as Record<string, unknown>;
    return safeParse(
      BalanceRowSchema,
      {
        user_id: userId,
        balance: row.balance,
        lifetime_purchased: row.lifetime_purchased,
      },
      "BalanceRepository.getBalance",
    );
  }

  /** Add credits to a user's account. */
  async addCredits(
    userId: string,
    amount: string,
    type: string,
    metadata: string,
    expiresAt: string | null,
    bucket: string | null,
    idempotencyKey: string | null,
  ): Promise<AddCreditsRow> {
    const decimalAmount = new Decimal(amount);
    const rows = await this.callproc("post_credit", [
      userId,
      decimalAmount.isNegative() ? "adjustment" : type === "purchase" ? "purchase" : "grant",
      amount,
      type,
      idempotencyKey,
      metadata,
      bucket,
      null,
      expiresAt,
      "0",
    ]);
    const row = requireRow(rows, "BalanceRepository.addCredits") as Record<string, unknown>;
    const state =
      row.error_code == null
        ? ((await this.callproc("get_credit_state", [userId]))[0] as
            | Record<string, unknown>
            | undefined)
        : undefined;
    const grant =
      row.error_code == null && decimalAmount.isPositive() && row.entry_id != null
        ? ((await this.callproc("get_credit_grant_details", [userId, row.entry_id]))[0] as
            | Record<string, unknown>
            | undefined)
        : undefined;
    return safeParse(
      AddCreditsRowSchema,
      {
        entry_id: row.entry_id ?? null,
        user_id: userId,
        amount,
        new_balance: row.balance_after ?? null,
        lifetime_purchased: state?.lifetime_purchased ?? null,
        bucket: decimalAmount.isPositive() ? (grant?.bucket_key ?? null) : null,
        idempotent: row.replayed,
        error: row.error_code,
      },
      "BalanceRepository.addCredits",
    );
  }

  /** Fetch available balance (balance minus reserved holds). */
  async getAvailable(userId: string): Promise<AvailableRow | null> {
    const rows = await this.callproc("get_credit_state", [userId]);
    if (!rows || rows.length === 0) {
      return null;
    }
    const row = requireRow(rows, "BalanceRepository.getAvailable") as Record<string, unknown>;
    return safeParse(
      AvailableRowSchema,
      {
        balance: row.balance,
        reserved: row.reserved,
        available: row.available,
      },
      "BalanceRepository.getAvailable",
    );
  }

  /** Execute a configured grant-program event and return every award row. */
  async executeGrantProgram(params: {
    trigger: string;
    programKey: string;
    subjectId: string;
    eventKey: string;
    referrerSubjectId: string | null;
    region: string | null;
    metadata: string;
  }): Promise<GrantProgramAwardRow[]> {
    const rows = await this.callproc("execute_grant_program", [
      params.trigger,
      params.programKey,
      params.subjectId,
      params.eventKey,
      params.referrerSubjectId,
      params.region,
      params.metadata,
    ]);
    return (rows ?? []).map((row) =>
      safeParse(GrantProgramAwardRowSchema, row, "BalanceRepository.executeGrantProgram"),
    );
  }
}
