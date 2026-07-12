import { z } from "zod";
import type { QueryFn } from "../types.js";
import { unwrapJsonb, safeParse } from "../_shared.js";

export const BillingEventRowSchema = z
  .object({
    status: z.string().optional(),
  })
  .passthrough();

export type BillingEventRow = z.infer<typeof BillingEventRowSchema>;

/** Repository for billing event lifecycle operations. */
export class BillingEventRepository {
  constructor(private query: QueryFn) {}

  /** Claim a billing event for processing (idempotent claim). */
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
    return data ? safeParse(BillingEventRowSchema, data, "BillingEventRepository.claim") : null;
  }

  /** Mark a billing event as completed. */
  async complete(provider: string, eventId: string): Promise<void> {
    await this.query("SELECT * FROM public.complete_billing_event($1, $2)", [provider, eventId]);
  }

  /** Mark a billing event as failed. */
  async fail(provider: string, eventId: string): Promise<void> {
    await this.query("SELECT * FROM public.fail_billing_event($1, $2)", [provider, eventId]);
  }
}
