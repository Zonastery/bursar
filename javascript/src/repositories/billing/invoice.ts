import type { QueryFn } from "../types.js";

export class BillingInvoiceRepository {
  constructor(private query: QueryFn) {}

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
      `SELECT public.upsert_billing_invoice($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)`,
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
