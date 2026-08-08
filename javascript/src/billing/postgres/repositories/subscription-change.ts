import { z } from "zod";
import type { BillingSubscriptionChangeUpdate } from "../../contracts.js";
import type {
  BillingSubscriptionChange,
  BillingSubscriptionChangeInput,
  BillingSubscriptionOfferContext,
} from "../../types/index.js";
import { StoreError } from "../../../errors.js";
import { optionalBoundedDiagnosticMessage } from "../../../shared/diagnostics.js";
import type { QueryFn } from "../../../shared/postgres-types.js";
import {
  optionalRecordRow,
  pgBoolean,
  postgresUuid,
  requireRecordRow,
  requireResultField,
  safeParse,
} from "../../../shared/postgres-validation.js";

const timestamp = z
  .union([
    z.string().datetime({ offset: true }),
    z.date().refine((value) => !Number.isNaN(value.getTime())),
  ])
  .transform((value) => (value instanceof Date ? value.toISOString() : value));
const positiveBigint = z
  .union([z.string().regex(/^[1-9]\d*$/), z.number().int().positive().safe()])
  .transform(String);
const changeState = z.enum(["awaiting_payment", "scheduled", "applied", "failed", "canceled"]);
const effectiveBehavior = z.enum(["immediate", "renewal"]);
const prorationBehavior = z.enum(["provider_default", "invoice_immediately", "none"]);

const OpenChangeResultSchema = z
  .object({
    change_id: positiveBigint.nullable(),
    state: changeState.nullable(),
    error_code: z
      .enum([
        "invalid_request",
        "missing_subscription",
        "invalid_target_offer",
        "idempotency_conflict",
        "open_change_exists",
      ])
      .nullable(),
  })
  .strict()
  .superRefine((row, ctx) => {
    const succeeded = row.error_code === null;
    if (succeeded !== (row.change_id !== null && row.state !== null)) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: "subscription-change result has inconsistent success fields",
      });
    }
  });

const ChangeRowSchema = z
  .object({
    id: positiveBigint,
    subscription_id: postgresUuid,
    from_offer_id: postgresUuid,
    from_catalog_revision_id: postgresUuid,
    to_offer_id: postgresUuid,
    to_catalog_revision_id: postgresUuid,
    effective_at: timestamp.nullable(),
    effective_behavior: effectiveBehavior,
    state: changeState,
    proration_behavior: prorationBehavior,
    idempotency_key: z.string().min(1),
    provider_operation_id: z.string().min(1).nullable(),
    error_message: z.string().min(1).max(8192).nullable(),
  })
  .strict();

type ChangeRow = z.infer<typeof ChangeRowSchema>;

const OfferContextRowSchema = z
  .object({
    side: z.enum(["from", "to"]),
    offer_id: postgresUuid,
    offer_key: z.string().min(1),
    plan_id: postgresUuid,
    plan_key: z.string().min(1),
    billing_unit: z.enum(["day", "week", "month", "year"]),
    billing_count: z.number().int().positive(),
  })
  .strict();

type OfferContextRow = z.infer<typeof OfferContextRowSchema>;

function projectChange(row: Record<string, unknown>): Record<string, unknown> {
  return {
    id: row.id,
    subscription_id: row.subscription_id,
    from_offer_id: row.from_offer_id,
    from_catalog_revision_id: row.from_catalog_revision_id,
    to_offer_id: row.to_offer_id,
    to_catalog_revision_id: row.to_catalog_revision_id,
    effective_at: row.effective_at,
    effective_behavior: row.effective_behavior,
    state: row.state,
    proration_behavior: row.proration_behavior,
    idempotency_key: row.idempotency_key,
    provider_operation_id: row.provider_operation_id,
    error_message: row.error_message,
  };
}

function projectContext(row: Record<string, unknown>): Record<string, unknown> {
  return {
    side: row.side,
    offer_id: row.offer_id,
    offer_key: row.offer_key,
    plan_id: row.plan_id,
    plan_key: row.plan_key,
    billing_unit: row.billing_unit,
    billing_count: row.billing_count,
  };
}

function publicContext(row: OfferContextRow): BillingSubscriptionOfferContext {
  return {
    offerId: row.offer_id,
    offerKey: row.offer_key,
    planId: row.plan_id,
    plan: row.plan_key,
    interval: row.billing_unit,
    intervalCount: row.billing_count,
  };
}

/** Persistence boundary for durable subscription offer transitions. */
export class BillingSubscriptionChangeRepository {
  constructor(private readonly query: QueryFn) {}

  async create(
    subscriptionId: string,
    input: BillingSubscriptionChangeInput,
  ): Promise<BillingSubscriptionChange> {
    const rows = await this.query(
      `SELECT * FROM bursar.open_subscription_change(
         $1::uuid, $2::uuid, $3::timestamptz, $4, $5, $6
       )`,
      [
        subscriptionId,
        input.toOfferId,
        input.effectiveAt,
        input.effective,
        input.idempotencyKey,
        input.prorationBehavior ?? "provider_default",
      ],
    );
    const raw = requireRecordRow(rows, "BillingSubscriptionChangeRepository.create");
    const result = safeParse(
      OpenChangeResultSchema,
      { change_id: raw.change_id, state: raw.state, error_code: raw.error_code },
      "BillingSubscriptionChangeRepository.create",
      { indeterminate: true },
    );
    if (result.error_code !== null) {
      throw new StoreError(`subscription change rejected: ${result.error_code}`, {
        details: { errorCode: result.error_code },
      });
    }
    if (result.change_id === null) {
      throw new StoreError("subscription change returned no identifier", { indeterminate: true });
    }
    const change = await this.getById(result.change_id);
    if (change === null) {
      throw new StoreError("subscription change could not be read after creation", {
        indeterminate: true,
        details: { subscriptionChangeId: result.change_id },
      });
    }
    return change;
  }

  async getById(id: string): Promise<BillingSubscriptionChange | null> {
    const rows = await this.query(
      "SELECT * FROM bursar.get_billing_subscription_change($1::bigint)",
      [id],
    );
    return this.parseOptional(rows, "BillingSubscriptionChangeRepository.getById");
  }

  async getOpen(
    provider: string,
    providerSubscriptionId: string,
  ): Promise<BillingSubscriptionChange | null> {
    const rows = await this.query(
      "SELECT * FROM bursar.get_open_billing_subscription_change($1, $2)",
      [provider, providerSubscriptionId],
    );
    return this.parseOptional(rows, "BillingSubscriptionChangeRepository.getOpen");
  }

  async update(id: string, update: BillingSubscriptionChangeUpdate): Promise<void> {
    if (update.state === undefined) return;
    const rows = await this.query(
      "SELECT bursar.advance_subscription_change($1::bigint, $2, $3, $4) AS advanced",
      [
        id,
        update.state,
        update.providerOperationId ?? null,
        optionalBoundedDiagnosticMessage(update.errorMessage),
      ],
    );
    if (
      !requireResultField(rows, "advanced", pgBoolean, "BillingSubscriptionChangeRepository.update")
    ) {
      throw new StoreError(`subscription change transition rejected: ${id}`, {
        details: { subscriptionChangeId: id },
      });
    }
  }

  private async parseOptional(
    rows: readonly unknown[],
    context: string,
  ): Promise<BillingSubscriptionChange | null> {
    const raw = optionalRecordRow(rows, context);
    if (raw === null) return null;
    const row = safeParse(ChangeRowSchema, projectChange(raw), context);
    const contexts = await this.getOfferContexts(row, context);
    return {
      id: row.id,
      subscriptionId: row.subscription_id,
      fromOfferId: row.from_offer_id,
      toOfferId: row.to_offer_id,
      fromOffer: publicContext(contexts.from),
      toOffer: publicContext(contexts.to),
      effectiveAt: row.effective_at,
      effective: row.effective_behavior,
      state: row.state,
      prorationBehavior: row.proration_behavior,
      idempotencyKey: row.idempotency_key,
      providerOperationId: row.provider_operation_id,
      errorMessage: row.error_message,
    };
  }

  private async getOfferContexts(
    row: ChangeRow,
    context: string,
  ): Promise<{ from: OfferContextRow; to: OfferContextRow }> {
    const rows = await this.query(
      `SELECT requested.side, requested.offer_id, offer_context.*
       FROM (
         VALUES
           ('from', $1::uuid, $2::uuid),
           ('to', $3::uuid, $4::uuid)
       ) AS requested(side, offer_id, catalog_revision_id)
       CROSS JOIN LATERAL bursar.get_catalog_offer_context(
         requested.offer_id,
         requested.catalog_revision_id
       ) AS offer_context`,
      [
        row.from_offer_id,
        row.from_catalog_revision_id,
        row.to_offer_id,
        row.to_catalog_revision_id,
      ],
    );
    if (rows.length !== 2) {
      throw new StoreError(`${context}: expected both subscription-change offer contexts`, {
        details: { subscriptionChangeId: row.id, rowCount: rows.length },
      });
    }
    const parsed = rows.map((candidate, index) => {
      if (typeof candidate !== "object" || candidate === null || Array.isArray(candidate)) {
        throw new StoreError(`${context}: malformed subscription-change offer context`, {
          details: { subscriptionChangeId: row.id, index },
        });
      }
      const record = candidate as Record<string, unknown>;
      return safeParse(OfferContextRowSchema, projectContext(record), `${context}.context`);
    });
    const bySide = new Map(parsed.map((candidate) => [candidate.side, candidate]));
    const from = bySide.get("from");
    const to = bySide.get("to");
    if (bySide.size !== 2 || from === undefined || to === undefined) {
      throw new StoreError(`${context}: duplicate or missing subscription-change offer context`, {
        details: { subscriptionChangeId: row.id },
      });
    }
    if (from.offer_id !== row.from_offer_id || to.offer_id !== row.to_offer_id) {
      throw new StoreError(`${context}: subscription-change offer context does not match`, {
        details: { subscriptionChangeId: row.id },
      });
    }
    return { from, to };
  }
}
