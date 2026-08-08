import Decimal from "decimal.js";
import { z } from "zod";
import { StoreError } from "../../../errors.js";
import type { QueryFn } from "../../../shared/postgres-types.js";
import { postgresUuid, safeParse, unwrapJsonb } from "../../../shared/postgres-validation.js";

const PositiveDecimalSchema = z
  .union([z.string(), z.number(), z.instanceof(Decimal)])
  .transform((value) => new Decimal(value))
  .refine((value) => value.isFinite() && value.gt(0), "expected a finite positive decimal");

const SafeMinorUnitsSchema = z
  .union([z.string().regex(/^\d+$/), z.number().int().nonnegative().safe()])
  .transform(Number)
  .refine(Number.isSafeInteger, "minor-unit amount exceeds JavaScript's safe integer range");

const BillingTopupRowSchema = z
  .object({
    id: postgresUuid,
    topup_key: z.string().min(1),
    credits_per_unit: PositiveDecimalSchema,
    bucket_key: z.string().min(1),
    amount_minor: SafeMinorUnitsSchema,
    currency: z.string().regex(/^[A-Z]{3}$/),
    min_quantity: z.number().int().positive(),
    max_quantity: z.number().int().positive(),
    default_quantity: z.number().int().positive(),
  })
  .strict()
  .superRefine((row, ctx) => {
    if (row.max_quantity < row.min_quantity) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: "max_quantity is below min_quantity" });
    }
    if (row.default_quantity < row.min_quantity || row.default_quantity > row.max_quantity) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: "default_quantity is outside the configured range",
      });
    }
  });

export type BillingTopupRow = z.infer<typeof BillingTopupRowSchema>;

/** Repository for credit top-up resolution. */
export class BillingTopupRepository {
  constructor(private readonly query: QueryFn) {}

  async resolveByPrice(
    provider: string,
    priceId: string | null,
    productId: string | null,
  ): Promise<BillingTopupRow | null> {
    const lookupType = priceId !== null ? "price_id" : "product_id";
    return this.resolve(provider, lookupType, priceId ?? productId, "resolveByPrice");
  }

  async resolveByLookup(provider: string, lookupKey: string): Promise<BillingTopupRow | null> {
    return this.resolve(provider, "external_id", lookupKey, "resolveByLookup");
  }

  private async resolve(
    provider: string,
    lookupType: "price_id" | "product_id" | "external_id",
    lookupValue: string | null,
    operation: string,
  ): Promise<BillingTopupRow | null> {
    if (!provider.trim()) throw new TypeError("provider must not be empty");
    if (!lookupValue?.trim()) return null;

    const context = `BillingTopupRepository.${operation}`;
    const rows = await this.query("SELECT * FROM bursar.resolve_catalog_topup($1, $2, $3)", [
      provider,
      lookupType,
      lookupValue,
    ]);
    if (rows.length > 1) {
      throw new StoreError(`${context}: expected at most one top-up`, {
        details: { rowCount: rows.length },
      });
    }
    const raw = unwrapJsonb(rows);
    if (raw?.id == null) return null;
    return safeParse(
      BillingTopupRowSchema,
      {
        id: raw.id,
        topup_key: raw.topup_key,
        credits_per_unit: raw.credits_per_unit,
        bucket_key: raw.bucket_key,
        amount_minor: raw.amount_minor,
        currency: raw.currency,
        min_quantity: raw.min_quantity,
        max_quantity: raw.max_quantity,
        default_quantity: raw.default_quantity,
      },
      context,
    );
  }
}
