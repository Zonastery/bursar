import { z } from "zod";
import type { QueryFn } from "../../../shared/postgres-types.js";
import type { PostgresRow } from "../../../shared/json.js";
import {
  optionalRecordRow,
  postgresUuid,
  requireResultField,
  safeParse,
} from "../../../shared/postgres-validation.js";

const PgSafeMinorUnitsSchema = z.union([
  z.string().regex(/^\d+$/),
  z.number().int().nonnegative().safe(),
]);

const BillingPaymentRowSchema = z
  .object({
    id: postgresUuid,
    provider: z.string().min(1),
    provider_payment_id: z.string().min(1),
    provider_invoice_id: z.string().nullable(),
    subject_id: postgresUuid,
    amount_minor: PgSafeMinorUnitsSchema,
    tax_minor: PgSafeMinorUnitsSchema,
    currency: z.string().regex(/^[A-Z]{3}$/),
    purpose: z.enum(["subscription", "credit_topup"]),
    status: z.enum(["pending", "succeeded", "failed", "canceled"]),
    provider_updated_at: z.union([z.string().datetime({ offset: true }), z.date()]),
    metadata: z.record(z.string(), z.json()),
  })
  .strict();
export type BillingPaymentRow = z.infer<typeof BillingPaymentRowSchema>;
export type ForRefundRow = BillingPaymentRow;

function parsePaymentRow(row: PostgresRow, context: string): BillingPaymentRow {
  return safeParse(
    BillingPaymentRowSchema,
    {
      id: row.id,
      provider: row.provider,
      provider_payment_id: row.provider_payment_id,
      provider_invoice_id: row.provider_invoice_id,
      subject_id: row.subject_id,
      amount_minor: row.amount_minor,
      tax_minor: row.tax_minor,
      currency: row.currency,
      purpose: row.purpose,
      status: row.status,
      provider_updated_at: row.provider_updated_at,
      metadata: row.metadata,
    },
    context,
  );
}

export class BillingPaymentRepository {
  constructor(private query: QueryFn) {}
  async upsert(
    provider: string,
    providerPaymentId: string,
    providerInvoiceId: string | null,
    userId: string,
    amountMinor: number,
    taxMinor: number,
    currency: string,
    purpose: "subscription" | "credit_topup",
    metadata: string | null,
    status: "pending" | "succeeded" | "failed" | "canceled",
    providerUpdatedAt: string,
  ): Promise<string> {
    if (!userId) throw new TypeError("billing payment requires a subject");
    if (purpose !== "subscription" && purpose !== "credit_topup") {
      throw new TypeError("billing payment requires a known purpose");
    }
    const rows = await this.query(
      `SELECT bursar.upsert_billing_payment(
         $1::uuid,$2,$3,$4,$5,$6,$7,$8::bursar.billing_payment_status,$9,$10,$11::jsonb
       ) AS id`,
      [
        userId,
        provider,
        providerPaymentId,
        amountMinor,
        taxMinor,
        currency,
        purpose,
        status,
        providerUpdatedAt,
        providerInvoiceId,
        metadata ?? "{}",
      ],
    );
    return requireResultField(rows, "id", postgresUuid, "BillingPaymentRepository.upsert");
  }
  async getForRefund(provider: string, providerPaymentId: string): Promise<ForRefundRow | null> {
    const rows = await this.query("SELECT * FROM bursar.get_billing_payment_by_provider($1,$2)", [
      provider,
      providerPaymentId,
    ]);
    const row = optionalRecordRow(rows, "BillingPaymentRepository.getForRefund");
    return row === null ? null : parsePaymentRow(row, "BillingPaymentRepository.getForRefund");
  }
  async getDirect(provider: string, providerPaymentId: string): Promise<BillingPaymentRow | null> {
    const rows = await this.query("SELECT * FROM bursar.get_billing_payment_by_provider($1,$2)", [
      provider,
      providerPaymentId,
    ]);
    const row = optionalRecordRow(rows, "BillingPaymentRepository.getDirect");
    return row === null ? null : parsePaymentRow(row, "BillingPaymentRepository.getDirect");
  }
}
