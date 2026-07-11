import type { QueryFn } from "../types.js";

export class BillingCustomerRepository {
  constructor(private query: QueryFn) {}

  async upsert(
    provider: string,
    providerCustomerId: string,
    userId: string,
    email: string | null,
  ): Promise<void> {
    await this.query(
      `INSERT INTO public.billing_customers (provider, provider_customer_id, user_id, email)
       VALUES ($1, $2, $3, $4)
       ON CONFLICT (provider, provider_customer_id) DO UPDATE SET
         user_id = EXCLUDED.user_id,
         email = COALESCE(EXCLUDED.email, billing_customers.email),
         updated_at = now()`,
      [provider, providerCustomerId, userId, email],
    );
  }

  async get(provider: string, providerCustomerId: string): Promise<string | null> {
    const rows = await this.query(
      "SELECT user_id FROM public.billing_customers WHERE provider = $1 AND provider_customer_id = $2",
      [provider, providerCustomerId],
    );
    if (rows.length === 0) return null;
    return String((rows[0] as Record<string, unknown>).user_id);
  }
}
