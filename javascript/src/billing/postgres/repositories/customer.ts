import { z } from "zod";
import type { QueryFn } from "../../../shared/postgres-types.js";
import {
  optionalRecordRow,
  postgresUuid,
  requireResultField,
  safeParse,
} from "../../../shared/postgres-validation.js";

const CustomerRowSchema = z.object({
  subject_id: postgresUuid,
});

const CustomerByUserRowSchema = z.object({
  provider: z.string().min(1),
  provider_customer_id: z.string().min(1),
});

export class BillingCustomerRepository {
  constructor(private query: QueryFn) {}

  async upsert(
    provider: string,
    providerCustomerId: string,
    userId: string,
    email: string | null,
  ): Promise<void> {
    const rows = await this.query(
      "SELECT bursar.upsert_billing_customer($1::uuid, $2, $3, $4) AS id",
      [userId, provider, providerCustomerId, email],
    );
    requireResultField(rows, "id", postgresUuid, "BillingCustomerRepository.upsert");
  }

  async get(provider: string, providerCustomerId: string): Promise<string | null> {
    const rows = await this.query("SELECT * FROM bursar.get_billing_customer_by_provider($1, $2)", [
      provider,
      providerCustomerId,
    ]);
    const row = optionalRecordRow(rows, "BillingCustomerRepository.get");
    return row === null
      ? null
      : safeParse(CustomerRowSchema, row, "BillingCustomerRepository.get").subject_id;
  }

  async getByUserId(
    userId: string,
    provider?: string | null,
  ): Promise<{ provider: string; providerCustomerId: string } | null> {
    const rows = await this.query("SELECT * FROM bursar.get_billing_customer($1::uuid, $2)", [
      userId,
      provider ?? null,
    ]);
    const row = optionalRecordRow(rows, "BillingCustomerRepository.getByUserId");
    if (row === null) return null;
    const parsed = safeParse(CustomerByUserRowSchema, row, "BillingCustomerRepository.getByUserId");
    return { provider: parsed.provider, providerCustomerId: parsed.provider_customer_id };
  }
}
