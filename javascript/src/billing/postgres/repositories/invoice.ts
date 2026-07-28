import type { QueryFn } from "../../../shared/postgres-types.js";
import type { BillingInvoiceInfo } from "../../types/index.js";

export class BillingInvoiceRepository {
  constructor(private query: QueryFn) {}

  async listForUser(userId: string): Promise<BillingInvoiceInfo[]> {
    const rows = await this.query(`SELECT * FROM bursar.list_billing_invoices($1::uuid)`, [userId]);
    return (rows as Array<Record<string, unknown>>).map((row) => ({
      provider: String(row.provider),
      providerInvoiceId: String(row.provider_invoice_id),
      status: row.status == null ? null : String(row.status),
      amountPaidMinor: row.amount_paid_minor == null ? null : Number(row.amount_paid_minor),
      amountDueMinor: row.amount_due_minor == null ? null : Number(row.amount_due_minor),
      currency: row.currency == null ? null : String(row.currency),
      periodStart: row.period_start == null ? null : String(row.period_start),
      periodEnd: row.period_end == null ? null : String(row.period_end),
    }));
  }

  async upsert(
    subjectId: string,
    provider: string,
    providerInvoiceId: string,
    subscriptionId: string | null,
    status: string | null,
    amountPaidMinor: number | null,
    amountDueMinor: number | null,
    currency: string,
    periodStart: string | null,
    periodEnd: string | null,
    metadata: Record<string, unknown>,
    providerUpdatedAt: string,
  ): Promise<void> {
    await this.query(
      `SELECT bursar.upsert_billing_invoice($1::uuid, $2, $3, $4::uuid, $5, $6, $7, $8, $9, $10, $11::jsonb, $12)`,
      [
        subjectId,
        provider,
        providerInvoiceId,
        subscriptionId,
        status,
        amountDueMinor,
        amountPaidMinor,
        currency,
        periodStart,
        periodEnd,
        JSON.stringify(metadata),
        providerUpdatedAt,
      ],
    );
  }
}
