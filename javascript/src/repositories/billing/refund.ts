import type { QueryFn } from "../types.js";

export class BillingRefundRepository {
  constructor(private query: QueryFn) {}

  async upsert(
    provider: string,
    providerRefundId: string,
    providerPaymentId: string | null,
    userId: string | null,
    amountMinor: number,
    currency: string,
    reason: string | null,
    metadata: string | null,
  ): Promise<void> {
    await this.query(`SELECT bursar.upsert_billing_refund($1, $2, $3, $4, $5, $6, $7, $8)`, [
      provider,
      providerRefundId,
      providerPaymentId,
      userId,
      amountMinor,
      currency,
      reason,
      metadata,
    ]);
  }
}
