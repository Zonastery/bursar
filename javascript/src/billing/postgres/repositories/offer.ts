import { z } from "zod";
import type { QueryFn } from "../../../shared/postgres-types.js";
import { pgBoolean, unwrapJsonb, safeParse } from "../../../shared/postgres-validation.js";

const BillingOfferRowSchema = z
  .object({
    id: z.string().optional(),
    plan_id: z.string().nullable().optional(),
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
    const rows = await this.query("SELECT * FROM bursar.resolve_catalog_offer($1, $2, $3)", [
      provider,
      priceId != null ? "price_id" : "product_id",
      priceId ?? productId,
    ]);
    const data = unwrapJsonb(rows);
    const planRows = (await this.query("SELECT * FROM bursar.resolve_catalog_plan($1, $2, $3)", [
      provider,
      priceId != null ? "price_id" : "product_id",
      priceId ?? productId,
    ])) as Array<Record<string, unknown>>;
    return data && data.offer_key != null
      ? safeParse(
          BillingOfferRowSchema,
          {
            ...data,
            plan_id: planRows[0]?.id,
            plan: data.plan_key,
            interval: data.billing_unit,
            interval_count: data.billing_count,
            grant_mode: data.cycle_grant_amount == null ? undefined : "cycle_grant",
            grant_credits: data.cycle_grant_amount,
            grant_bucket: data.cycle_grant_bucket_key,
            grant_replace_prior: data.cycle_grant_renewal === "replace_previous",
          },
          "BillingOfferRepository.resolveByPrice",
        )
      : null;
  }

  /** Resolve a billing offer by provider lookup key. */
  async resolveByLookup(provider: string, lookupKey: string): Promise<BillingOfferRow | null> {
    const rows = await this.query(
      "SELECT * FROM bursar.resolve_catalog_offer($1, 'external_id', $2)",
      [provider, lookupKey],
    );
    const data = unwrapJsonb(rows);
    const planRows = (await this.query(
      "SELECT * FROM bursar.resolve_catalog_plan($1, 'external_id', $2)",
      [provider, lookupKey],
    )) as Array<Record<string, unknown>>;
    return data && data.offer_key != null
      ? safeParse(
          BillingOfferRowSchema,
          {
            ...data,
            plan_id: planRows[0]?.id,
            plan: data.plan_key,
            interval: data.billing_unit,
            interval_count: data.billing_count,
            grant_mode: data.cycle_grant_amount == null ? undefined : "cycle_grant",
            grant_credits: data.cycle_grant_amount,
            grant_bucket: data.cycle_grant_bucket_key,
            grant_replace_prior: data.cycle_grant_renewal === "replace_previous",
          },
          "BillingOfferRepository.resolveByLookup",
        )
      : null;
  }
}
