import { z } from "zod";
import Decimal from "decimal.js";
import type { CallProc } from "../../../shared/postgres-types.js";
import { pgBoolean, safeParse } from "../../../shared/postgres-validation.js";

const BalanceRowSchema = z
  .object({
    user_id: z.string(),
    balance: z.union([z.string(), z.number()] as const).nullable(),
    lifetime_purchased: z.union([z.string(), z.number()] as const).nullable(),
  })
  .partial()
  .passthrough();

const AddCreditsRowSchema = z
  .object({
    entry_id: z.string().nullable().optional(),
    user_id: z.string().optional(),
    amount: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
    new_balance: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
    lifetime_purchased: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
    bucket: z.string().optional(),
    idempotent: pgBoolean.nullable().optional(),
    error: z.string().nullable().optional(),
  })
  .passthrough();

const AvailableRowSchema = z
  .object({
    balance: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
    reserved: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
    available: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
  })
  .passthrough();

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
 * NOTE: getBalance returns null when no row exists (new user), while
 * addCredits and getAvailable always return a parsed object (empty object
 * fallback). This inconsistency is intentional: balance queries distinguish
 * "no data" from "zeroed data" whereas mutation reads always produce a result.
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
    const row = (rows?.[0] ?? {}) as Record<string, unknown>;
    const state =
      row.error_code == null
        ? ((await this.callproc("get_credit_state", [userId]))[0] as
            Record<string, unknown> | undefined)
        : undefined;
    const grant =
      row.error_code == null && decimalAmount.isPositive() && row.entry_id != null
        ? ((await this.callproc("get_credit_grant_details", [userId, row.entry_id]))[0] as
            Record<string, unknown> | undefined)
        : undefined;
    return safeParse(
      AddCreditsRowSchema,
      {
        ...row,
        user_id: userId,
        amount,
        new_balance: row.balance_after,
        lifetime_purchased: state?.lifetime_purchased,
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
    return safeParse(AvailableRowSchema, rows?.[0] ?? {}, "BalanceRepository.getAvailable");
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
