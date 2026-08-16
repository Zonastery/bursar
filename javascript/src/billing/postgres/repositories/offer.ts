import { Decimal } from "decimal.js";
import { z } from "zod";
import { StoreError } from "../../../errors.js";
import type { QueryFn } from "../../../shared/postgres-types.js";
import { postgresUuid, safeParse, unwrapJsonb } from "../../../shared/postgres-validation.js";

const PositiveDecimalSchema = z
  .union([z.string(), z.number(), z.instanceof(Decimal)])
  .transform((value) => new Decimal(value))
  .refine((value) => value.isFinite() && value.gt(0), "expected a finite positive decimal");

const CatalogOfferRowSchema = z
  .object({
    id: postgresUuid,
    catalog_revision_id: postgresUuid,
    offer_key: z.string().min(1),
    plan_key: z.string().min(1),
    billing_unit: z.enum(["day", "week", "month", "year"]),
    billing_count: z.number().int().positive(),
    cycle_grant_amount: PositiveDecimalSchema.nullable(),
    cycle_grant_bucket_key: z.string().min(1).nullable(),
    cycle_grant_renewal: z.enum(["replace_previous", "accumulate"]).nullable(),
  })
  .strict()
  .superRefine((row, ctx) => {
    const grantFields = [
      row.cycle_grant_amount,
      row.cycle_grant_bucket_key,
      row.cycle_grant_renewal,
    ];
    if (
      grantFields.some((value) => value !== null) &&
      grantFields.some((value) => value === null)
    ) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: "cycle grant fields must either all be set or all be null",
      });
    }
  });

const OfferContextRowSchema = z
  .object({
    offer_key: z.string().min(1),
    plan_id: postgresUuid,
    plan_key: z.string().min(1),
    billing_unit: z.enum(["day", "week", "month", "year"]),
    billing_count: z.number().int().positive(),
  })
  .strict();

interface BillingOfferRowBase {
  id: string;
  planId: string;
  offerKey: string;
  plan: string;
  interval: "day" | "week" | "month" | "year";
  intervalCount: number;
}

export type BillingOfferRow = BillingOfferRowBase &
  (
    | {
        grantCredits: null;
        grantBucket: null;
        grantReplacePrior: false;
      }
    | {
        grantCredits: Decimal;
        grantBucket: string;
        grantReplacePrior: boolean;
      }
  );

/** Repository for billing offer resolution. */
export class BillingOfferRepository {
  constructor(private readonly query: QueryFn) {}

  async resolveByPrice(
    provider: string,
    priceId: string | null,
    productId: string | null,
  ): Promise<BillingOfferRow | null> {
    const lookupType = priceId !== null ? "price_id" : "product_id";
    return this.resolve(provider, lookupType, priceId ?? productId, "resolveByPrice");
  }

  async resolveByLookup(provider: string, lookupKey: string): Promise<BillingOfferRow | null> {
    return this.resolve(provider, "external_id", lookupKey, "resolveByLookup");
  }

  private async resolve(
    provider: string,
    lookupType: "price_id" | "product_id" | "external_id",
    lookupValue: string | null,
    operation: string,
  ): Promise<BillingOfferRow | null> {
    if (!provider.trim()) throw new TypeError("provider must not be empty");
    if (!lookupValue?.trim()) return null;

    const context = `BillingOfferRepository.${operation}`;
    const rows = await this.query("SELECT * FROM bursar.resolve_catalog_offer($1, $2, $3)", [
      provider,
      lookupType,
      lookupValue,
    ]);
    if (rows.length > 1) {
      throw new StoreError(`${context}: expected at most one offer`, {
        details: { rowCount: rows.length },
      });
    }
    const raw = unwrapJsonb(rows);
    if (raw?.id == null) return null;
    const offer = safeParse(
      CatalogOfferRowSchema,
      {
        id: raw.id,
        catalog_revision_id: raw.catalog_revision_id,
        offer_key: raw.offer_key,
        plan_key: raw.plan_key,
        billing_unit: raw.billing_unit,
        billing_count: raw.billing_count,
        cycle_grant_amount: raw.cycle_grant_amount,
        cycle_grant_bucket_key: raw.cycle_grant_bucket_key,
        cycle_grant_renewal: raw.cycle_grant_renewal,
      },
      context,
    );

    const contextRows = await this.query(
      "SELECT * FROM bursar.get_catalog_offer_context($1::uuid, $2::uuid)",
      [offer.id, offer.catalog_revision_id],
    );
    if (contextRows.length !== 1) {
      throw new StoreError(`${context}: catalog plan context is missing`, {
        details: { offerId: offer.id, catalogRevisionId: offer.catalog_revision_id },
      });
    }
    const rawContext = contextRows[0];
    if (rawContext === undefined) {
      throw new StoreError("BillingOfferRepository: catalog plan context is missing", {
        details: { offerId: offer.id, catalogRevisionId: offer.catalog_revision_id },
      });
    }
    const offerContext = safeParse(
      OfferContextRowSchema,
      {
        offer_key: rawContext.offer_key,
        plan_id: rawContext.plan_id,
        plan_key: rawContext.plan_key,
        billing_unit: rawContext.billing_unit,
        billing_count: rawContext.billing_count,
      },
      `${context}.context`,
    );
    if (
      offerContext.offer_key !== offer.offer_key ||
      offerContext.plan_key !== offer.plan_key ||
      offerContext.billing_unit !== offer.billing_unit ||
      offerContext.billing_count !== offer.billing_count
    ) {
      throw new StoreError(`${context}: catalog plan context does not match the resolved offer`, {
        details: { offerId: offer.id, catalogRevisionId: offer.catalog_revision_id },
      });
    }

    const resolvedOffer: BillingOfferRowBase = {
      id: offer.id,
      planId: offerContext.plan_id,
      offerKey: offer.offer_key,
      plan: offer.plan_key,
      interval: offer.billing_unit,
      intervalCount: offer.billing_count,
    };
    if (offer.cycle_grant_amount === null) {
      return {
        ...resolvedOffer,
        grantCredits: null,
        grantBucket: null,
        grantReplacePrior: false,
      };
    }
    if (offer.cycle_grant_bucket_key === null || offer.cycle_grant_renewal === null) {
      throw new StoreError(`${context}: resolved offer has an incomplete cycle grant`, {
        details: { offerId: offer.id, catalogRevisionId: offer.catalog_revision_id },
      });
    }
    return {
      ...resolvedOffer,
      grantCredits: offer.cycle_grant_amount,
      grantBucket: offer.cycle_grant_bucket_key,
      grantReplacePrior: offer.cycle_grant_renewal === "replace_previous",
    };
  }
}
