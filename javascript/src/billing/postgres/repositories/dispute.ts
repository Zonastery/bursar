import type { QueryFn } from "../../../shared/postgres-types.js";

export class BillingDisputeRepository {
  constructor(private query: QueryFn) {}

  async upsert(
    provider: string,
    providerDisputeId: string,
    paymentId: string,
    status: string,
    reason: string | null,
    metadata: Record<string, unknown>,
    providerUpdatedAt: string,
  ): Promise<void> {
    await this.query(
      `SELECT bursar.upsert_billing_dispute($1, $2, $3::uuid, $4, $5, $6::jsonb, $7)`,
      [
        provider,
        providerDisputeId,
        paymentId,
        status,
        reason,
        JSON.stringify(metadata),
        providerUpdatedAt,
      ],
    );
  }
}
