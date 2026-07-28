import { z } from "zod";
import type { QueryFn } from "../../../shared/postgres-types.js";
import { safeParse } from "../../../shared/postgres-validation.js";

const BillingPaymentRowSchema = z
  .object({
    provider: z.string().optional(),
    provider_payment_id: z.string().optional(),
    subject_id: z.string().nullable().optional(),
    user_id: z.string().nullable().optional(),
    amount_minor: z.union([z.string(), z.number()]).optional(),
    tax_minor: z.union([z.string(), z.number()]).nullable().optional(),
    currency: z.string().optional(),
    purpose: z.string().nullable().optional(),
    status: z.string().optional(),
    provider_updated_at: z.unknown().optional(),
    metadata: z.record(z.string(), z.unknown()).nullable().optional(),
  })
  .passthrough();
const ForRefundRowSchema = z
  .object({
    credits_per_unit: z.union([z.string(), z.number()]).nullable().optional(),
    credits_per_major_unit: z.union([z.string(), z.number()]).nullable().optional(),
    tier: z.string().optional(),
    deposit_to: z.string().optional(),
  })
  .passthrough();
export type BillingPaymentRow = z.infer<typeof BillingPaymentRowSchema>;
export type ForRefundRow = z.infer<typeof ForRefundRowSchema>;

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
    status: string,
    providerUpdatedAt: string,
  ): Promise<string> {
    if (!userId) throw new Error("billing payment requires a subject");
    if (purpose !== "subscription" && purpose !== "credit_topup") {
      throw new Error("billing payment requires a known purpose");
    }
    const rows = await this.query(
      `SELECT bursar.upsert_billing_payment(
         $1::uuid,$2,$3,$4,$5,$6,$7,$8::bursar.billing_payment_status,$9,$10,$11::jsonb
       )`,
      [
        userId,
        provider,
        providerPaymentId,
        amountMinor,
        taxMinor ?? 0,
        currency,
        purpose,
        status,
        providerUpdatedAt,
        providerInvoiceId,
        metadata ?? "{}",
      ],
    );
    const id = (rows[0] as Record<string, unknown> | undefined)?.upsert_billing_payment;
    if (!id) throw new Error("billing payment upsert returned no ID");
    return String(id);
  }
  async getForRefund(provider: string, providerPaymentId: string): Promise<ForRefundRow | null> {
    const rows = await this.query("SELECT * FROM bursar.get_billing_payment_by_provider($1,$2)", [
      provider,
      providerPaymentId,
    ]);
    const row = rows[0] as Record<string, unknown> | undefined;
    return row?.id == null
      ? null
      : safeParse(ForRefundRowSchema, row, "BillingPaymentRepository.getForRefund");
  }
  async getDirect(provider: string, providerPaymentId: string): Promise<BillingPaymentRow | null> {
    const rows = await this.query("SELECT * FROM bursar.get_billing_payment_by_provider($1,$2)", [
      provider,
      providerPaymentId,
    ]);
    const row = rows[0] as Record<string, unknown> | undefined;
    return row?.id == null
      ? null
      : safeParse(BillingPaymentRowSchema, row, "BillingPaymentRepository.getDirect");
  }
}
