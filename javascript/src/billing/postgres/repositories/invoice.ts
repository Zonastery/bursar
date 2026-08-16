import { z } from "zod";
import type { QueryFn } from "../../../shared/postgres-types.js";
import type { JsonObject } from "../../../shared/json.js";
import type { BillingInvoiceRecord } from "../../types/index.js";
import {
  postgresUuid,
  requireResultField,
  safeParse,
} from "../../../shared/postgres-validation.js";

const safeMinorUnits = z
  .union([z.number().int().nonnegative().safe(), z.string().regex(/^\d+$/)])
  .transform(Number)
  .refine(Number.isSafeInteger, "minor-unit amount exceeds JavaScript's safe integer range");
const optionalTimestamp = z
  .union([
    z.date().refine((value) => !Number.isNaN(value.getTime())),
    z.string().datetime({ offset: true }),
  ])
  .transform((value) => (value instanceof Date ? value.toISOString() : value))
  .nullable();

const InvoiceRowSchema = z
  .object({
    provider: z.string().min(1),
    provider_invoice_id: z.string().min(1),
    status: z.enum(["draft", "open", "paid", "void", "uncollectible"]),
    amount_paid_minor: safeMinorUnits,
    amount_due_minor: safeMinorUnits,
    currency: z.string().regex(/^[A-Z]{3}$/),
    period_start: optionalTimestamp,
    period_end: optionalTimestamp,
  })
  .strict();

export class BillingInvoiceRepository {
  constructor(private query: QueryFn) {}

  async listForUser(userId: string): Promise<BillingInvoiceRecord[]> {
    const rows = await this.query(`SELECT * FROM bursar.list_billing_invoices($1::uuid)`, [userId]);
    return rows.map((row) => {
      const parsed = safeParse(
        InvoiceRowSchema,
        {
          provider: row.provider,
          provider_invoice_id: row.provider_invoice_id,
          status: row.status,
          amount_paid_minor: row.amount_paid_minor,
          amount_due_minor: row.amount_due_minor,
          currency: row.currency,
          period_start: row.period_start,
          period_end: row.period_end,
        },
        "BillingInvoiceRepository.listForUser",
      );
      return {
        provider: parsed.provider,
        providerInvoiceId: parsed.provider_invoice_id,
        status: parsed.status,
        amountPaidMinor: parsed.amount_paid_minor,
        amountDueMinor: parsed.amount_due_minor,
        currency: parsed.currency,
        periodStart: parsed.period_start,
        periodEnd: parsed.period_end,
      } satisfies BillingInvoiceRecord;
    });
  }

  async upsert(
    subjectId: string,
    provider: string,
    providerInvoiceId: string,
    subscriptionId: string | null,
    status: "draft" | "open" | "paid" | "void" | "uncollectible",
    amountPaidMinor: number,
    amountDueMinor: number,
    currency: string,
    periodStart: string | null,
    periodEnd: string | null,
    metadata: JsonObject,
    providerUpdatedAt: string,
  ): Promise<void> {
    const rows = await this.query(
      `SELECT bursar.upsert_billing_invoice($1::uuid, $2, $3, $4::uuid, $5, $6, $7, $8, $9, $10, $11::jsonb, $12) AS id`,
      [
        subjectId,
        provider,
        providerInvoiceId,
        subscriptionId,
        status,
        amountDueMinor,
        amountPaidMinor,
        currency,
        periodStart,
        periodEnd,
        JSON.stringify(metadata),
        providerUpdatedAt,
      ],
    );
    requireResultField(rows, "id", postgresUuid, "BillingInvoiceRepository.upsert");
  }
}
