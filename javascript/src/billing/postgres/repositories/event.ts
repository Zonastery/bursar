import { z } from "zod";
import type { QueryFn } from "../../../shared/postgres-types.js";
import { safeParse } from "../../../shared/postgres-validation.js";

const BillingEventRowSchema = z
  .object({
    status: z.string().optional(),
    event_id: z.string().uuid().nullable().optional(),
    claim_token: z.string().uuid().nullable().optional(),
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
    const row = rows[0] as Record<string, unknown> | undefined;
    return row
      ? safeParse(
          BillingEventRowSchema,
          { ...row, status: row.result, event_id: row.event_id },
          "BillingEventRepository.claim",
        )
      : null;
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
