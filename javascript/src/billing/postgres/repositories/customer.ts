import type { QueryFn } from "../../../shared/postgres-types.js";

export class BillingCustomerRepository {
  constructor(private query: QueryFn) {}

  async upsert(
    provider: string,
    providerCustomerId: string,
    userId: string,
    email: string | null,
  ): Promise<void> {
    await this.query("SELECT bursar.upsert_billing_customer($1::uuid, $2, $3, $4)", [
      userId,
      provider,
      providerCustomerId,
      email,
    ]);
  }

  async get(provider: string, providerCustomerId: string): Promise<string | null> {
    const rows = await this.query("SELECT * FROM bursar.get_billing_customer_by_provider($1, $2)", [
      provider,
      providerCustomerId,
    ]);
    const row = rows[0] as Record<string, unknown> | undefined;
    return row?.subject_id == null ? null : String(row.subject_id);
  }

  async getByUserId(
    userId: string,
    provider?: string | null,
  ): Promise<{ provider: string; providerCustomerId: string } | null> {
    const rows = await this.query("SELECT * FROM bursar.get_billing_customer($1::uuid, $2)", [
      userId,
      provider ?? null,
    ]);
    const row = rows[0] as Record<string, unknown> | undefined;
    if (!row) return null;
    return { provider: String(row.provider), providerCustomerId: String(row.provider_customer_id) };
  }
}
