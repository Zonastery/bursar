import { z } from "zod";
import type { QueryFn } from "../types.js";

export const BillingEventRowSchema = z
  .object({
    status: z.string().optional(),
  })
  .passthrough();

export type BillingEventRow = z.infer<typeof BillingEventRowSchema>;

function unwrapJsonb(rows: unknown[]): Record<string, unknown> | null {
  if (rows.length !== 1) return null;
  const row = rows[0] as Record<string, unknown>;
  const keys = Object.keys(row);
  if (keys.length !== 1) return null;
  const v = row[keys[0]];
  if (v !== null && typeof v === "object" && !Array.isArray(v)) return v as Record<string, unknown>;
  return null;
}

export class BillingEventRepository {
  constructor(private query: QueryFn) {}

  async claim(
    provider: string,
    eventId: string,
    eventType: string,
    metadata: string,
  ): Promise<BillingEventRow | null> {
    const rows = await this.query("SELECT * FROM public.claim_billing_event($1, $2, $3, $4)", [
      provider,
      eventId,
      eventType,
      metadata,
    ]);
    const data = unwrapJsonb(rows);
    return data ? BillingEventRowSchema.parse(data) : null;
  }

  async complete(provider: string, eventId: string): Promise<void> {
    await this.query("SELECT * FROM public.complete_billing_event($1, $2)", [provider, eventId]);
  }

  async fail(provider: string, eventId: string): Promise<void> {
    await this.query("SELECT * FROM public.fail_billing_event($1, $2)", [provider, eventId]);
  }
}
