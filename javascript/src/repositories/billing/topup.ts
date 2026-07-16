import { z } from "zod";
import type { QueryFn } from "../types.js";
import { unwrapJsonb, safeParse } from "../_shared.js";

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

/** Repository for credit top-up resolution. */
export class BillingTopupRepository {
  constructor(private query: QueryFn) {}

  /** Resolve a credit top-up by price ID and product ID. */
  async resolveByPrice(
    provider: string,
    priceId: string | null,
    productId: string | null,
  ): Promise<BillingTopupRow | null> {
    const rows = await this.query(
      "SELECT * FROM bursar.resolve_credit_topup_by_price($1, $2, $3)",
      [provider, priceId, productId],
    );
    const data = unwrapJsonb(rows);
    return data
      ? safeParse(BillingTopupRowSchema, data, "BillingTopupRepository.resolveByPrice")
      : null;
  }

  /** Resolve a credit top-up by provider lookup key. */
  async resolveByLookup(provider: string, lookupKey: string): Promise<BillingTopupRow | null> {
    const rows = await this.query("SELECT * FROM bursar.resolve_credit_topup_by_lookup($1, $2)", [
      provider,
      lookupKey,
    ]);
    const data = unwrapJsonb(rows);
    return data
      ? safeParse(BillingTopupRowSchema, data, "BillingTopupRepository.resolveByLookup")
      : null;
  }
}
