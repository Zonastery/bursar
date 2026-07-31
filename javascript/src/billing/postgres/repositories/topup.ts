import { z } from "zod";
import type { QueryFn } from "../../../shared/postgres-types.js";
import { unwrapJsonb, safeParse } from "../../../shared/postgres-validation.js";

const BillingTopupRowSchema = z
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
    bucket_key: z.string().optional(),
    amount_minor: z.union([z.string(), z.number()]).optional(),
    currency: z.string().optional(),
    min_quantity: z.number().optional(),
    max_quantity: z.number().optional(),
    default_quantity: z.number().optional(),
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
    const rows = await this.query("SELECT * FROM bursar.resolve_catalog_topup($1, $2, $3)", [
      provider,
      priceId != null ? "price_id" : "product_id",
      priceId ?? productId,
    ]);
    const data = unwrapJsonb(rows);
    return data && data.topup_key != null
      ? safeParse(BillingTopupRowSchema, data, "BillingTopupRepository.resolveByPrice")
      : null;
  }

  /** Resolve a credit top-up by provider lookup key. */
  async resolveByLookup(provider: string, lookupKey: string): Promise<BillingTopupRow | null> {
    const rows = await this.query(
      "SELECT * FROM bursar.resolve_catalog_topup($1, 'external_id', $2)",
      [provider, lookupKey],
    );
    const data = unwrapJsonb(rows);
    return data && data.topup_key != null
      ? safeParse(BillingTopupRowSchema, data, "BillingTopupRepository.resolveByLookup")
      : null;
  }
}
