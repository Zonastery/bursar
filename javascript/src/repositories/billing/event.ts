import { z } from "zod";
import type { QueryFn } from "../types.js";
import { unwrapJsonb, safeParse } from "../_shared.js";

export const BillingEventRowSchema = z
  .object({
    status: z.string().optional(),
    claim_token: z.string().uuid().optional(),
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
    const rows = await this.query("SELECT * FROM bursar.claim_billing_event($1, $2, $3, $4)", [
      provider,
      eventId,
      eventType,
      metadata,
    ]);
    const data = unwrapJsonb(rows);
    return data ? safeParse(BillingEventRowSchema, data, "BillingEventRepository.claim") : null;
  }

  /** Mark a billing event as completed. */
  async complete(provider: string, eventId: string, claimToken: string): Promise<void> {
    await this.query("SELECT * FROM bursar.complete_billing_event($1, $2, $3::uuid)", [
      provider,
      eventId,
      claimToken,
    ]);
  }

  /** Mark a billing event as failed. */
  async fail(provider: string, eventId: string, claimToken: string, error?: string): Promise<void> {
    await this.query("SELECT * FROM bursar.fail_billing_event($1, $2, $3::uuid, $4)", [
      provider,
      eventId,
      claimToken,
      error ?? null,
    ]);
  }
}
