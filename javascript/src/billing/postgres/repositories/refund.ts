import { StoreError } from "../../../errors.js";
import type { QueryFn } from "../../../shared/postgres-types.js";
import {
  optionalRecordRow,
  postgresUuid,
  requireResultField,
  safeParse,
} from "../../../shared/postgres-validation.js";

export class BillingRefundRepository {
  constructor(private query: QueryFn) {}

  async upsert(
    provider: string,
    providerRefundId: string,
    providerPaymentId: string,
    userId: string,
    amountMinor: number,
    currency: string,
    reason: string | null,
    metadata: string | null,
    status: string,
    providerUpdatedAt: string,
  ): Promise<string> {
    if (!providerPaymentId) throw new TypeError("refund requires providerPaymentId");
    const payments = await this.query(
      "SELECT * FROM bursar.get_billing_payment_by_provider($1, $2)",
      [provider, providerPaymentId],
    );
    const payment = optionalRecordRow(payments, "BillingRefundRepository.upsert.payment");
    if (payment === null) {
      throw new StoreError("refund payment not found", {
        retryable: true,
        details: { provider, providerPaymentId },
      });
    }
    const paymentId = safeParse(
      postgresUuid,
      payment.id,
      "BillingRefundRepository.upsert.payment.id",
    );
    const rows = await this.query(
      `SELECT bursar.upsert_billing_refund(
         $1::uuid, $2, $3, $4, $5, $6, $7::uuid, $8::char(3), $9::jsonb
       ) AS id`,
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
    return requireResultField(rows, "id", postgresUuid, "BillingRefundRepository.upsert");
  }
}
