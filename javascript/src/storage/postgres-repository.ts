import { boundedDiagnosticMessage } from "../shared/diagnostics.js";
import type { QueryFn } from "../shared/postgres-types.js";
import type {
  BillingEventPayloadExport,
  OutboxEvent,
  OutboxStore,
  UsageChargeExport,
} from "./ports.js";

type Row = Record<string, unknown>;

function asRow(value: unknown, context: string): Row {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${context} returned an invalid row`);
  }
  return value as Row;
}

function requiredString(row: Row, key: string, context: string): string {
  const value = row[key];
  if (value === null || value === undefined || String(value).length === 0) {
    throw new Error(`${context} is missing ${key}`);
  }
  if (value instanceof Date) return value.toISOString();
  return String(value);
}

function optionalString(row: Row, key: string): string | null {
  const value = row[key];
  return value === null || value === undefined ? null : String(value);
}

function jsonObject(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function scalarBoolean(rows: unknown[]): boolean {
  if (rows.length !== 1) return false;
  const values = Object.values(asRow(rows[0], "PostgreSQL boolean RPC"));
  return values.length === 1 && values[0] === true;
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
        payloadVersion: Number(row.payload_version),
        payload: jsonObject(row.payload),
        claimToken: requiredString(row, "claim_token", "outbox event"),
        attemptCount: Number(row.attempt_count),
        createdAt: requiredString(row, "created_at", "outbox event"),
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
      feature: optionalString(row, "feature"),
      model: optionalString(row, "model"),
      region: optionalString(row, "region"),
      measures: jsonObject(row.measures),
      dimensions: jsonObject(row.dimensions),
      metadata: jsonObject(row.metadata),
      requested: requiredString(row, "requested", "usage charge export"),
      charged: requiredString(row, "charged", "usage charge export"),
      allowanceRequested: requiredString(row, "allowance_requested", "usage charge export"),
      allowanceCovered: requiredString(row, "allowance_covered", "usage charge export"),
      billingDisposition:
        requiredString(row, "billing_disposition", "usage charge export") === "record_only"
          ? "record_only"
          : "billable",
      catalogRevisionId: optionalString(row, "catalog_revision_id"),
      planId: optionalString(row, "plan_id"),
      rateCardKey: optionalString(row, "rate_card_key"),
      pricingSnapshot: jsonObject(row.pricing_snapshot),
      ledgerEntryId: optionalString(row, "ledger_entry_id"),
      correctionOfChargeId: optionalString(row, "correction_of_charge_id"),
      idempotencyKey: requiredString(row, "idempotency_key", "usage charge export"),
      requestDigest: requiredString(row, "request_digest", "usage charge export"),
      eventAt: requiredString(row, "event_at", "usage charge export"),
      createdAt: requiredString(row, "created_at", "usage charge export"),
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
      receivedAt: requiredString(row, "received_at", "billing payload export"),
      completedAt: optionalString(row, "completed_at"),
      envelope:
        row.envelope === null || row.envelope === undefined ? null : jsonObject(row.envelope),
      objectKey: optionalString(row, "object_key"),
      objectVersion: optionalString(row, "object_version"),
      archivedAt: optionalString(row, "archived_at"),
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
