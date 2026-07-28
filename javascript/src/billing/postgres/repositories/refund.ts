import type { QueryFn } from "../../../shared/postgres-types.js";

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
    status: string,
    providerUpdatedAt: string,
  ): Promise<string> {
    if (!providerPaymentId) throw new Error("refund requires providerPaymentId");
    const payments = await this.query(
      "SELECT * FROM bursar.get_billing_payment_by_provider($1, $2)",
      [provider, providerPaymentId],
    );
    const paymentId = (payments[0] as Record<string, unknown> | undefined)?.id;
    if (!paymentId) throw new Error("refund payment not found");
    const rows = await this.query(
      `SELECT bursar.upsert_billing_refund(
         $1::uuid, $2, $3, $4, $5, $6, $7::uuid, $8::char(3), $9::jsonb
       )`,
      [
        paymentId,
        providerRefundId,
        amountMinor,
        status,
        reason,
        providerUpdatedAt,
        userId,
        currency,
        metadata ?? "{}",
      ],
    );
    const refundId = (rows[0] as Record<string, unknown> | undefined)?.upsert_billing_refund;
    if (!refundId) throw new Error("billing refund upsert returned no ID");
    return String(refundId);
  }
}
