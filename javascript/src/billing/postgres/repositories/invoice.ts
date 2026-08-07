import { z } from "zod";
import type { QueryFn } from "../../../shared/postgres-types.js";
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

const InvoiceRowSchema = z.object({
  provider: z.string().min(1),
  provider_invoice_id: z.string().min(1),
  status: z.enum(["draft", "open", "paid", "void", "uncollectible"]),
  amount_paid_minor: safeMinorUnits,
  amount_due_minor: safeMinorUnits,
  currency: z.string().regex(/^[A-Z]{3}$/),
  period_start: z.unknown().nullable(),
  period_end: z.unknown().nullable(),
});

export class BillingInvoiceRepository {
  constructor(private query: QueryFn) {}

  async listForUser(userId: string): Promise<BillingInvoiceRecord[]> {
    const rows = await this.query(`SELECT * FROM bursar.list_billing_invoices($1::uuid)`, [userId]);
    return rows.map((row) => {
      const parsed = safeParse(InvoiceRowSchema, row, "BillingInvoiceRepository.listForUser");
      return {
        provider: parsed.provider,
        providerInvoiceId: parsed.provider_invoice_id,
        status: parsed.status,
        amountPaidMinor: parsed.amount_paid_minor,
        amountDueMinor: parsed.amount_due_minor,
        currency: parsed.currency,
        periodStart: parsed.period_start == null ? null : String(parsed.period_start),
        periodEnd: parsed.period_end == null ? null : String(parsed.period_end),
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
    metadata: Record<string, unknown>,
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
