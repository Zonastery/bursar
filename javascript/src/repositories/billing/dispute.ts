import type { QueryFn } from "../types.js";

export class BillingDisputeRepository {
  constructor(private query: QueryFn) {}

  async upsert(
    provider: string,
    providerDisputeId: string,
    providerPaymentId: string | null,
    userId: string | null,
    status: string,
    reason: string | null,
    metadata: string | null,
  ): Promise<void> {
    await this.query(`SELECT public.upsert_billing_dispute($1, $2, $3, $4, $5, $6, $7)`, [
      provider,
      providerDisputeId,
      providerPaymentId,
      userId,
      status,
      reason,
      metadata,
    ]);
  }
}
