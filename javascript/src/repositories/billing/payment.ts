import { z } from "zod";
import type { QueryFn } from "../types.js";

export const BillingPaymentRowSchema = z
  .object({
    provider: z.string().optional(),
    provider_payment_id: z.string().optional(),
    user_id: z.string().nullable().optional(),
    amount_minor: z.number().optional(),
    tax_minor: z.number().nullable().optional(),
    currency: z.string().optional(),
    purpose: z.string().nullable().optional(),
    metadata: z.record(z.string(), z.unknown()).nullable().optional(),
    created_at: z.unknown().optional(),
    updated_at: z.unknown().optional(),
    credits_per_unit: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
    credits_per_major_unit: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
  })
  .passthrough();

export const ForRefundRowSchema = z
  .object({
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

export type BillingPaymentRow = z.infer<typeof BillingPaymentRowSchema>;
export type ForRefundRow = z.infer<typeof ForRefundRowSchema>;

function unwrapJsonb(rows: unknown[]): Record<string, unknown> | null {
  if (rows.length !== 1) return null;
  const row = rows[0] as Record<string, unknown>;
  const keys = Object.keys(row);
  if (keys.length !== 1) return null;
  const v = row[keys[0]];
  if (v !== null && typeof v === "object" && !Array.isArray(v)) return v as Record<string, unknown>;
  return null;
}

export class BillingPaymentRepository {
  constructor(private query: QueryFn) {}

  async upsert(
    provider: string,
    providerPaymentId: string,
    providerInvoiceId: string | null,
    userId: string | null,
    amountMinor: number,
    taxMinor: number | null,
    currency: string,
    purpose: string | null,
    metadata: string | null,
  ): Promise<void> {
    await this.query(`SELECT public.upsert_billing_payment($1, $2, $3, $4, $5, $6, $7, $8, $9)`, [
      provider,
      providerPaymentId,
      providerInvoiceId,
      userId,
      amountMinor,
      taxMinor,
      currency,
      purpose,
      metadata,
    ]);
  }

  async getForRefund(provider: string, providerPaymentId: string): Promise<ForRefundRow | null> {
    const rows = await this.query("SELECT * FROM public.get_billing_payment_for_refund($1, $2)", [
      provider,
      providerPaymentId,
    ]);
    const data = unwrapJsonb(rows);
    return data ? ForRefundRowSchema.parse(data) : null;
  }

  async getDirect(provider: string, providerPaymentId: string): Promise<BillingPaymentRow | null> {
    const rows = await this.query(
      `SELECT provider, provider_payment_id, user_id, amount_minor,
              tax_minor, currency, purpose, metadata, created_at, updated_at
       FROM public.billing_payments
       WHERE provider = $1 AND provider_payment_id = $2`,
      [provider, providerPaymentId],
    );
    if (rows.length === 0) return null;
    return BillingPaymentRowSchema.parse(rows[0]);
  }
}
