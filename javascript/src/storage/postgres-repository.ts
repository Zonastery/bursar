import { z } from "zod";

import { boundedDiagnosticMessage } from "../shared/diagnostics.js";
import { safeParse } from "../shared/postgres-validation.js";
import type { QueryFn } from "../shared/postgres-types.js";
import type {
  BillingEventPayloadExport,
  OutboxEvent,
  OutboxStore,
  UsageChargeExport,
} from "./ports.js";

type Row = Record<string, unknown>;

const rowSchema = z.record(z.string(), z.unknown());
const textSchema = z.string().min(1);
const timestampSchema = z
  .union([
    z.date().refine((value) => !Number.isNaN(value.getTime())),
    z.string().datetime({ offset: true }),
  ])
  .transform((value) => (value instanceof Date ? value : new Date(value)).toISOString());
const nonnegativeIntegerSchema = z
  .union([z.number(), z.string().regex(/^\d+$/u)])
  .transform(Number)
  .pipe(z.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER));

function asRow(value: unknown, context: string): Row {
  return safeParse(rowSchema, value, context);
}

function requiredString(row: Row, key: string, context: string): string {
  return safeParse(textSchema, row[key], `${context}.${key}`);
}

function optionalString(row: Row, key: string, context: string): string | null {
  const value = row[key];
  return value === null || value === undefined
    ? null
    : safeParse(textSchema, value, `${context}.${key}`);
}

function requiredTimestamp(row: Row, key: string, context: string): string {
  return safeParse(timestampSchema, row[key], `${context}.${key}`);
}

function optionalTimestamp(row: Row, key: string, context: string): string | null {
  const value = row[key];
  return value === null || value === undefined
    ? null
    : safeParse(timestampSchema, value, `${context}.${key}`);
}

function jsonObject(value: unknown, context: string): Record<string, unknown> {
  return safeParse(rowSchema, value, context);
}

function nonnegativeInteger(row: Row, key: string, context: string): number {
  return safeParse(nonnegativeIntegerSchema, row[key], `${context}.${key}`);
}

function scalarBoolean(rows: unknown[]): boolean {
  if (rows.length !== 1) {
    throw new Error(`PostgreSQL boolean RPC returned ${rows.length} rows; expected one`);
  }
  const values = Object.values(asRow(rows[0], "PostgreSQL boolean RPC"));
  if (values.length !== 1) {
    throw new Error("PostgreSQL boolean RPC returned an invalid result envelope");
  }
  return safeParse(z.boolean(), values[0], "PostgreSQL boolean RPC result");
}

export class PostgresStorageRepository implements OutboxStore {
  constructor(
    private readonly query: QueryFn,
    private readonly tenantId: string,
  ) {}

  async claim(
    topics: readonly string[],
    limit: number,
    leaseSeconds: number,
  ): Promise<OutboxEvent[]> {
    const rows = await this.query(
      "SELECT * FROM bursar.claim_outbox_events($1::uuid, $2::integer, $3::integer, $4::text[])",
      [this.tenantId, limit, leaseSeconds, [...topics]],
    );
    return rows.map((raw) => {
      const row = asRow(raw, "claim_outbox_events");
      return {
        eventId: requiredString(row, "event_id", "outbox event"),
        tenantId: requiredString(row, "tenant_id", "outbox event"),
        topic: requiredString(row, "topic", "outbox event"),
        aggregateType: requiredString(row, "aggregate_type", "outbox event"),
        aggregateId: requiredString(row, "aggregate_id", "outbox event"),
        payloadVersion: nonnegativeInteger(row, "payload_version", "outbox event"),
        payload: jsonObject(row.payload, "outbox event.payload"),
        claimToken: requiredString(row, "claim_token", "outbox event"),
        attemptCount: nonnegativeInteger(row, "attempt_count", "outbox event"),
        createdAt: requiredTimestamp(row, "created_at", "outbox event"),
      };
    });
  }

  async complete(event: OutboxEvent): Promise<boolean> {
    return scalarBoolean(
      await this.query("SELECT bursar.complete_outbox_event($1::bigint, $2::uuid)", [
        event.eventId,
        event.claimToken,
      ]),
    );
  }

  async fail(
    event: OutboxEvent,
    error: string,
    retryDelaySeconds: number,
    attemptLimit: number,
  ): Promise<boolean> {
    return scalarBoolean(
      await this.query(
        "SELECT bursar.fail_outbox_event($1::bigint, $2::uuid, $3::text, $4::integer, $5::integer)",
        [
          event.eventId,
          event.claimToken,
          boundedDiagnosticMessage(error, "outbox_delivery_failed"),
          retryDelaySeconds,
          attemptLimit,
        ],
      ),
    );
  }

  async getUsageCharge(chargeId: string): Promise<UsageChargeExport | null> {
    const rows = await this.query("SELECT bursar.export_usage_charge($1::uuid) AS payload", [
      chargeId,
    ]);
    const value = rows.length > 0 ? asRow(rows[0], "export_usage_charge").payload : null;
    if (value === null || value === undefined) return null;
    const row = asRow(value, "export_usage_charge payload");
    if (row.payload_available !== true) {
      throw new Error(`Usage charge ${chargeId} payload expired before export`);
    }
    return {
      tenantId: requiredString(row, "tenant_id", "usage charge export"),
      chargeId: requiredString(row, "charge_id", "usage charge export"),
      accountId: requiredString(row, "account_id", "usage charge export"),
      subjectId: requiredString(row, "subject_id", "usage charge export"),
      operation: requiredString(row, "operation", "usage charge export"),
      feature: optionalString(row, "feature", "usage charge export"),
      model: optionalString(row, "model", "usage charge export"),
      region: optionalString(row, "region", "usage charge export"),
      measures: jsonObject(row.measures, "usage charge export.measures"),
      dimensions: jsonObject(row.dimensions, "usage charge export.dimensions"),
      metadata: jsonObject(row.metadata, "usage charge export.metadata"),
      requested: requiredString(row, "requested", "usage charge export"),
      charged: requiredString(row, "charged", "usage charge export"),
      allowanceRequested: requiredString(row, "allowance_requested", "usage charge export"),
      allowanceCovered: requiredString(row, "allowance_covered", "usage charge export"),
      billingDisposition: safeParse(
        z.enum(["billable", "record_only"]),
        row.billing_disposition,
        "usage charge export.billing_disposition",
      ),
      catalogRevisionId: optionalString(row, "catalog_revision_id", "usage charge export"),
      planId: optionalString(row, "plan_id", "usage charge export"),
      rateCardKey: optionalString(row, "rate_card_key", "usage charge export"),
      pricingSnapshot: jsonObject(row.pricing_snapshot, "usage charge export.pricing_snapshot"),
      ledgerEntryId: optionalString(row, "ledger_entry_id", "usage charge export"),
      correctionOfChargeId: optionalString(row, "correction_of_charge_id", "usage charge export"),
      idempotencyKey: requiredString(row, "idempotency_key", "usage charge export"),
      requestDigest: requiredString(row, "request_digest", "usage charge export"),
      eventAt: requiredTimestamp(row, "event_at", "usage charge export"),
      createdAt: requiredTimestamp(row, "created_at", "usage charge export"),
    };
  }

  async getBillingEventPayload(eventId: string): Promise<BillingEventPayloadExport | null> {
    const rows = await this.query(
      "SELECT bursar.export_billing_event_payload($1::uuid) AS payload",
      [eventId],
    );
    const value = rows.length > 0 ? asRow(rows[0], "export_billing_event_payload").payload : null;
    if (value === null || value === undefined) return null;
    const row = asRow(value, "export_billing_event_payload payload");
    return {
      tenantId: requiredString(row, "tenant_id", "billing payload export"),
      eventId: requiredString(row, "event_id", "billing payload export"),
      provider: requiredString(row, "provider", "billing payload export"),
      providerEnvironment: requiredString(row, "provider_environment", "billing payload export"),
      providerEventId: requiredString(row, "provider_event_id", "billing payload export"),
      eventType: requiredString(row, "event_type", "billing payload export"),
      status: requiredString(row, "status", "billing payload export"),
      receivedAt: requiredTimestamp(row, "received_at", "billing payload export"),
      completedAt: optionalTimestamp(row, "completed_at", "billing payload export"),
      envelope:
        row.envelope === null || row.envelope === undefined
          ? null
          : jsonObject(row.envelope, "billing payload export.envelope"),
      objectKey: optionalString(row, "object_key", "billing payload export"),
      objectVersion: optionalString(row, "object_version", "billing payload export"),
      archivedAt: optionalTimestamp(row, "archived_at", "billing payload export"),
    };
  }

  async archiveBillingEventPayload(
    eventId: string,
    objectKey: string,
    objectVersion: string | null,
  ): Promise<boolean> {
    return scalarBoolean(
      await this.query(
        "SELECT bursar.archive_billing_event_payload($1::uuid, $2::text, $3::text)",
        [eventId, objectKey, objectVersion],
      ),
    );
  }
}
