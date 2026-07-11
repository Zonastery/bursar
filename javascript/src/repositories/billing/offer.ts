import { z } from "zod";
import type { QueryFn } from "../types.js";

export const BillingOfferRowSchema = z
  .object({
    offer_key: z.string().optional(),
    plan: z.string().nullable().optional(),
    interval: z.string().optional(),
    interval_count: z.number().optional(),
    grant_mode: z.string().optional(),
    grant_credits: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
    grant_bucket: z.string().nullable().optional(),
    grant_replace_prior: z
      .union([z.boolean(), z.string()] as const)
      .nullable()
      .optional(),
  })
  .passthrough();

export type BillingOfferRow = z.infer<typeof BillingOfferRowSchema>;

function unwrapJsonb(rows: unknown[]): Record<string, unknown> | null {
  if (rows.length !== 1) return null;
  const row = rows[0] as Record<string, unknown>;
  const keys = Object.keys(row);
  if (keys.length !== 1) return null;
  const v = row[keys[0]];
  if (v !== null && typeof v === "object" && !Array.isArray(v)) return v as Record<string, unknown>;
  return null;
}

export class BillingOfferRepository {
  constructor(private query: QueryFn) {}

  async resolveByPrice(
    provider: string,
    priceId: string | null,
    productId: string | null,
  ): Promise<BillingOfferRow | null> {
    const rows = await this.query(
      "SELECT * FROM public.resolve_billing_offer_by_price($1, $2, $3)",
      [provider, priceId, productId],
    );
    const data = unwrapJsonb(rows);
    return data ? BillingOfferRowSchema.parse(data) : null;
  }

  async resolveByLookup(provider: string, lookupKey: string): Promise<BillingOfferRow | null> {
    const rows = await this.query("SELECT * FROM public.resolve_billing_offer_by_lookup($1, $2)", [
      provider,
      lookupKey,
    ]);
    const data = unwrapJsonb(rows);
    return data ? BillingOfferRowSchema.parse(data) : null;
  }
}
