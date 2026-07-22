import type { QueryFn } from "../types.js";
import type { BillingInvoiceInfo } from "../../billing/billing-types.js";

export class BillingInvoiceRepository {
  constructor(private query: QueryFn) {}

  async listForUser(userId: string): Promise<BillingInvoiceInfo[]> {
    const rows = await this.query(
      `SELECT provider, provider_invoice_id, status, amount_paid_minor, amount_due_minor,
              currency, period_start, period_end
         FROM bursar.billing_invoices
        WHERE user_id = $1
        ORDER BY COALESCE(period_end, created_at) DESC`,
      [userId],
    );
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
    provider: string,
    providerInvoiceId: string,
    providerSubscriptionId: string | null,
    userId: string | null,
    status: string | null,
    amountPaidMinor: number | null,
    amountDueMinor: number | null,
    currency: string,
    periodStart: string | null,
    periodEnd: string | null,
    metadata: string | null,
  ): Promise<void> {
    await this.query(
      `SELECT bursar.upsert_billing_invoice($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)`,
      [
        provider,
        providerInvoiceId,
        providerSubscriptionId,
        userId,
        status,
        amountPaidMinor,
        amountDueMinor,
        currency,
        periodStart,
        periodEnd,
        metadata,
      ],
    );
  }
}
