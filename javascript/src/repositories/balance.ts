import { z } from "zod";
import type { CallProc } from "./types.js";
import { pgBoolean, safeParse } from "./_shared.js";

export const BalanceRowSchema = z
  .object({
    user_id: z.string(),
    balance: z.union([z.string(), z.number()] as const).nullable(),
    lifetime_purchased: z.union([z.string(), z.number()] as const).nullable(),
  })
  .partial()
  .passthrough();

export const AddCreditsRowSchema = z
  .object({
    id: z.string().optional(),
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
    error: z.string().optional(),
  })
  .passthrough();

export const AvailableRowSchema = z
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

export type BalanceRow = z.infer<typeof BalanceRowSchema>;
export type AddCreditsRow = z.infer<typeof AddCreditsRowSchema>;
export type AvailableRow = z.infer<typeof AvailableRowSchema>;

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
    const rows = await this.callproc("get_credits_balance", [userId]);
    if (!rows || rows.length === 0) return null;
    return safeParse(BalanceRowSchema, rows[0], "BalanceRepository.getBalance");
  }

  /** Add credits to a user's account. */
  async addCredits(
    userId: string,
    amount: string,
    type: string,
    metadata: string,
    bucket: string | null,
    idempotencyKey: string | null,
  ): Promise<AddCreditsRow> {
    const rows = await this.callproc("credits_add", [
      userId,
      amount,
      type,
      metadata,
      bucket,
      idempotencyKey,
    ]);
    return safeParse(AddCreditsRowSchema, rows?.[0] ?? {}, "BalanceRepository.addCredits");
  }

  /** Fetch available balance (balance minus reserved holds). */
  async getAvailable(userId: string): Promise<AvailableRow> {
    const rows = await this.callproc("get_available_credits", [userId]);
    return safeParse(AvailableRowSchema, rows?.[0] ?? {}, "BalanceRepository.getAvailable");
  }
}
