import { z } from "zod";
import type { QueryFn } from "../../../shared/postgres-types.js";
import type { PostgresValue } from "../../../shared/json.js";
import {
  pgBoolean,
  postgresUuid,
  requireRecordRow,
  requireResultField,
  safeParse,
} from "../../../shared/postgres-validation.js";

const BillingEventRowSchema = z
  .object({
    status: z.enum([
      "claimed",
      "duplicate",
      "busy",
      "invalid_request",
      "idempotency_conflict",
      "max_retries_exceeded",
    ]),
    event_id: postgresUuid.nullable(),
    claim_token: postgresUuid.nullable(),
  })
  .strict()
  .superRefine((row, ctx) => {
    if (row.status === "claimed") {
      if (row.event_id === null || row.claim_token === null) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          message: "claimed billing event requires event_id and claim_token",
        });
      }
      return;
    }
    if (row.claim_token !== null) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: "unclaimed billing event cannot expose a claim_token",
      });
    }
    if (row.status === "invalid_request") {
      if (row.event_id !== null) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          message: "invalid billing event request cannot identify a stored event",
        });
      }
      return;
    }
    if (row.event_id === null) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: "stored billing event outcome requires event_id",
      });
    }
  });

export type BillingEventRow = z.infer<typeof BillingEventRowSchema>;

function booleanResult(rows: PostgresValue[], key: string, context: string): boolean {
  return requireResultField(rows, key, pgBoolean, context);
}

/** Repository for billing event lifecycle operations. */
export class BillingEventRepository {
  constructor(private query: QueryFn) {}

  /** Claim a billing event for processing (idempotent claim). */
  async claim(
    provider: string,
    eventId: string,
    eventType: string,
    metadata: string,
  ): Promise<BillingEventRow> {
    const rows = await this.query("SELECT * FROM bursar.claim_billing_event($1, $2, $3, $4)", [
      provider,
      eventId,
      eventType,
      metadata,
    ]);
    const row = requireRecordRow(rows, "BillingEventRepository.claim");
    return safeParse(
      BillingEventRowSchema,
      { event_id: row.event_id, status: row.result, claim_token: row.claim_token },
      "BillingEventRepository.claim",
      { indeterminate: true },
    );
  }

  /** Mark a billing event as completed. */
  async complete(provider: string, eventId: string, claimToken: string): Promise<boolean> {
    const rows = await this.query(
      "SELECT bursar.complete_billing_event($1, $2, $3::uuid) AS completed",
      [provider, eventId, claimToken],
    );
    return booleanResult(rows, "completed", "BillingEventRepository.complete");
  }

  /** Mark a billing event as failed. */
  async fail(
    provider: string,
    eventId: string,
    claimToken: string,
    error?: string,
  ): Promise<boolean> {
    const rows = await this.query(
      "SELECT bursar.fail_billing_event($1, $2, $3::uuid, $4) AS failed",
      [provider, eventId, claimToken, error ?? null],
    );
    return booleanResult(rows, "failed", "BillingEventRepository.fail");
  }
}
