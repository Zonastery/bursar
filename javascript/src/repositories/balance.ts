import { z } from "zod";
import type { CallProc } from "./types.js";

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
    idempotent: z
      .union([z.boolean(), z.string()] as const)
      .nullable()
      .optional(),
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

export class BalanceRepository {
  constructor(private callproc: CallProc) {}

  async getBalance(userId: string): Promise<BalanceRow | null> {
    const rows = await this.callproc("get_credits_balance", [userId]);
    if (!rows || rows.length === 0) return null;
    return BalanceRowSchema.parse(rows[0]);
  }

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
    return AddCreditsRowSchema.parse(rows?.[0] ?? {});
  }

  async getAvailable(userId: string): Promise<AvailableRow> {
    const rows = await this.callproc("get_available_credits", [userId]);
    return AvailableRowSchema.parse(rows?.[0] ?? {});
  }
}
