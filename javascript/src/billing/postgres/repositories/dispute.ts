import type { QueryFn } from "../../../shared/postgres-types.js";
import type { JsonObject } from "../../../shared/json.js";
import { postgresUuid, requireResultField } from "../../../shared/postgres-validation.js";

export class BillingDisputeRepository {
  constructor(private query: QueryFn) {}

  async upsert(
    provider: string,
    providerDisputeId: string,
    paymentId: string,
    status: string,
    reason: string | null,
    metadata: JsonObject,
    providerUpdatedAt: string,
  ): Promise<void> {
    const rows = await this.query(
      `SELECT bursar.upsert_billing_dispute($1, $2, $3::uuid, $4, $5, $6::jsonb, $7) AS id`,
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
    requireResultField(rows, "id", postgresUuid, "BillingDisputeRepository.upsert");
  }
}
