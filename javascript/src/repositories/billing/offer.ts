import { z } from "zod";
import type { QueryFn } from "../types.js";
import { pgBoolean, unwrapJsonb, safeParse } from "../_shared.js";

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
    grant_replace_prior: pgBoolean.nullable().optional(),
  })
  .passthrough();

export type BillingOfferRow = z.infer<typeof BillingOfferRowSchema>;

/** Repository for billing offer resolution. */
export class BillingOfferRepository {
  constructor(private query: QueryFn) {}

  /** Resolve a billing offer by price ID and product ID. */
  async resolveByPrice(
    provider: string,
    priceId: string | null,
    productId: string | null,
  ): Promise<BillingOfferRow | null> {
    const rows = await this.query(
      "SELECT * FROM bursar.resolve_billing_offer_by_price($1, $2, $3)",
      [provider, priceId, productId],
    );
    const data = unwrapJsonb(rows);
    return data
      ? safeParse(BillingOfferRowSchema, data, "BillingOfferRepository.resolveByPrice")
      : null;
  }

  /** Resolve a billing offer by provider lookup key. */
  async resolveByLookup(provider: string, lookupKey: string): Promise<BillingOfferRow | null> {
    const rows = await this.query("SELECT * FROM bursar.resolve_billing_offer_by_lookup($1, $2)", [
      provider,
      lookupKey,
    ]);
    const data = unwrapJsonb(rows);
    return data
      ? safeParse(BillingOfferRowSchema, data, "BillingOfferRepository.resolveByLookup")
      : null;
  }
}
