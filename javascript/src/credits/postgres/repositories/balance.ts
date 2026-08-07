import { z } from "zod";
import Decimal from "decimal.js";
import type { CallProc } from "../../../shared/postgres-types.js";
import { pgBoolean, requireRow, safeParse } from "../../../shared/postgres-validation.js";

const decimal = z.union([z.string().min(1), z.number().finite()] as const);

const BalanceRowSchema = z.object({
  user_id: z.string().min(1),
  balance: decimal,
  lifetime_purchased: decimal,
});

const AddCreditsRowSchema = z
  .object({
    entry_id: z.string().nullable().optional(),
    user_id: z.string().min(1),
    amount: decimal,
    new_balance: decimal.nullable(),
    lifetime_purchased: decimal.nullable(),
    bucket: z.string().min(1),
    idempotent: pgBoolean,
    error: z.string().min(1).nullable(),
  })
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
  });

const AvailableRowSchema = z.object({
  balance: decimal,
  reserved: decimal,
  available: decimal,
});

const GrantProgramAwardRowSchema = z
  .object({
    grant_event_id: z.string().nullable().optional(),
    grant_award_id: z.string().nullable().optional(),
    recipient_subject_id: z.string().nullable().optional(),
    ledger_entry_id: z.string().nullable().optional(),
    amount: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
    replayed: pgBoolean.nullable().optional(),
    error_code: z.string().nullable().optional(),
  })
  .passthrough();

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
    return safeParse(
      BalanceRowSchema,
      {
        user_id: userId,
        ...(rows[0] as Record<string, unknown>),
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
        ...row,
        user_id: userId,
        amount,
        new_balance: row.balance_after ?? null,
        lifetime_purchased: state?.lifetime_purchased ?? null,
        bucket: grant?.bucket_key ?? bucket ?? "default",
        idempotent: row.replayed,
        error: row.error_code,
      },
      "BalanceRepository.addCredits",
    );
  }

  /** Fetch available balance (balance minus reserved holds). */
  async getAvailable(userId: string): Promise<AvailableRow> {
    const rows = await this.callproc("get_credit_state", [userId]);
    if (!rows || rows.length === 0) {
      return { balance: "0", reserved: "0", available: "0" };
    }
    return safeParse(AvailableRowSchema, rows[0], "BalanceRepository.getAvailable");
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
