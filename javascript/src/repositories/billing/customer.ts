import type { QueryFn } from "../types.js";

const BILLING_CUSTOMER_GET_SQL =
  "SELECT user_id FROM bursar.billing_customers WHERE provider = $1 AND provider_customer_id = $2";

/** Repository for billing customer operations. */
export class BillingCustomerRepository {
  constructor(private query: QueryFn) {}

  /** Upsert a billing customer record. */
  async upsert(
    provider: string,
    providerCustomerId: string,
    userId: string,
    email: string | null,
  ): Promise<void> {
    await this.query(
      `INSERT INTO bursar.billing_customers (provider, provider_customer_id, user_id, email)
       VALUES ($1, $2, $3, $4)
       ON CONFLICT (provider, provider_customer_id) DO UPDATE SET
         user_id = EXCLUDED.user_id,
         email = COALESCE(EXCLUDED.email, billing_customers.email),
         updated_at = now()`,
      [provider, providerCustomerId, userId, email],
    );
  }

  /** Fetch a user_id by provider identifiers. Returns null when no customer found.
   *
   * Guards against undefined/null user_id values that would produce the string
   * "undefined" or "null" instead of null.
   */
  async get(provider: string, providerCustomerId: string): Promise<string | null> {
    const rows = await this.query(BILLING_CUSTOMER_GET_SQL, [provider, providerCustomerId]);
    if (rows.length === 0) return null;
    const row = rows[0] as Record<string, unknown>;
    const userId = row.user_id;
    if (userId == null) return null;
    return String(userId);
  }

  /** Reverse lookup: find a customer record by user_id. Returns null if not found. */
  async getByUserId(
    userId: string,
    provider?: string | null,
  ): Promise<{ provider: string; providerCustomerId: string } | null> {
    if (provider) {
      const rows = await this.query(
        `SELECT provider, provider_customer_id
         FROM bursar.billing_customers
         WHERE user_id = $1 AND provider = $2
         ORDER BY updated_at DESC LIMIT 1`,
        [userId, provider],
      );
      if (rows.length === 0) return null;
      const row = rows[0] as Record<string, unknown>;
      return {
        provider: String(row.provider),
        providerCustomerId: String(row.provider_customer_id),
      };
    }
    const rows = await this.query(
      `SELECT provider, provider_customer_id
       FROM bursar.billing_customers
       WHERE user_id = $1
       ORDER BY updated_at DESC LIMIT 1`,
      [userId],
    );
    if (rows.length === 0) return null;
    const row = rows[0] as Record<string, unknown>;
    return {
      provider: String(row.provider),
      providerCustomerId: String(row.provider_customer_id),
    };
  }
}
