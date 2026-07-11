import { z } from "zod";
import type { QueryFn } from "../types.js";

export const BillingTopupRowSchema = z
  .object({
    topup_key: z.string().optional(),
    credits_per_unit: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
    credits_per_major_unit: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
    tier: z.string().optional(),
    deposit_to: z.string().optional(),
  })
  .passthrough();

export type BillingTopupRow = z.infer<typeof BillingTopupRowSchema>;

function unwrapJsonb(rows: unknown[]): Record<string, unknown> | null {
  if (rows.length !== 1) return null;
  const row = rows[0] as Record<string, unknown>;
  const keys = Object.keys(row);
  if (keys.length !== 1) return null;
  const v = row[keys[0]];
  if (v !== null && typeof v === "object" && !Array.isArray(v)) return v as Record<string, unknown>;
  return null;
}

export class BillingTopupRepository {
  constructor(private query: QueryFn) {}

  async resolveByPrice(
    provider: string,
    priceId: string | null,
    productId: string | null,
  ): Promise<BillingTopupRow | null> {
    const rows = await this.query(
      "SELECT * FROM public.resolve_credit_topup_by_price($1, $2, $3)",
      [provider, priceId, productId],
    );
    const data = unwrapJsonb(rows);
    return data ? BillingTopupRowSchema.parse(data) : null;
  }

  async resolveByLookup(provider: string, lookupKey: string): Promise<BillingTopupRow | null> {
    const rows = await this.query("SELECT * FROM public.resolve_credit_topup_by_lookup($1, $2)", [
      provider,
      lookupKey,
    ]);
    const data = unwrapJsonb(rows);
    return data ? BillingTopupRowSchema.parse(data) : null;
  }
}
