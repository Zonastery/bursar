/**
 * DB-backed storage repository integration tests for the JavaScript SDK.
 */

import { afterAll, beforeAll, beforeEach, describe, expect, inject, it } from "vitest";
import { Decimal } from "decimal.js";
import pg from "pg";
import { z } from "zod";
import { PostgresClient } from "../src/shared/postgres-client.js";
import type { JsonObject } from "../src/shared/json.js";
import { createBursarRuntime } from "../src/storage/runtime.js";
import { PostgresStorageRepository } from "../src/storage/postgres-repository.js";
import type { BillingEventPayloadExport, UsageChargeExport } from "../src/storage/ports.js";
import { TEST_TENANT_ID, applyMigrations, truncateBursarTables } from "./helpers/bootstrap.js";

const DATABASE_URL = inject("DATABASE_URL");
const OTHER_TENANT_ID = "00000000-0000-0000-0000-000000000002";

async function seedUsageOnly(
  pool: pg.Pool,
  idempotencyKey = "storage-repo-usage-1",
  traceId = "trace-1",
): Promise<{ chargeId: string; subjectId: string }> {
  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    await client.query("SELECT set_config('bursar.tenant_id', $1, true)", [TEST_TENANT_ID]);
    await client.query("SELECT set_config('bursar.provider_environment', 'test', true)");
    const account = await client.query(`
      WITH subject AS (
        INSERT INTO bursar.subjects DEFAULT VALUES
        RETURNING id
      ), account AS (
        INSERT INTO bursar.credit_accounts(subject_id, account_kind)
        SELECT id, 'personal' FROM subject
        RETURNING id, subject_id
      )
      SELECT id, subject_id FROM account
    `);
    const subjectId = String(account.rows[0]?.subject_id);
    const usage = await client.query(
      `
      SELECT charge_id, error_code
      FROM bursar.charge_usage(
        $1::uuid,
        'completion',
        0,
        $2,
        p_model => 'small-model',
        p_region => 'in',
        p_measures => '{"input_tokens":12}'::jsonb,
        p_dimensions => '{"tenant_tier":"starter"}'::jsonb,
        p_metadata => jsonb_build_object('trace_id', $3::text)
      )
      `,
      [subjectId, idempotencyKey, traceId],
    );
    expect(usage.rows[0]?.error_code).toBeNull();

    await client.query("COMMIT");
    return { chargeId: String(usage.rows[0]?.charge_id), subjectId };
  } catch (error) {
    await client.query("ROLLBACK");
    throw error;
  } finally {
    client.release();
  }
}

async function seedStorageRows(pool: pg.Pool): Promise<{
  chargeId: string;
  billingEventId: string;
}> {
  const { chargeId, subjectId } = await seedUsageOnly(pool);
  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    await client.query("SELECT set_config('bursar.tenant_id', $1, true)", [TEST_TENANT_ID]);
    await client.query("SELECT set_config('bursar.provider_environment', 'test', true)");
    const billingClaim = await client.query(
      `
      SELECT *
      FROM bursar.claim_billing_event(
        'stripe',
        'evt-storage-repo-1',
        'invoice.paid',
        $1::jsonb
      )
      `,
      [JSON.stringify({ userId: subjectId, kind: "invoice.paid" })],
    );
    const billingEventId = String(billingClaim.rows[0]?.event_id);
    const billingClaimToken = String(billingClaim.rows[0]?.claim_token);
    await client.query(
      "SELECT bursar.complete_billing_event('stripe', 'evt-storage-repo-1', $1::uuid)",
      [billingClaimToken],
    );
    await client.query("COMMIT");
    return { chargeId, billingEventId };
  } catch (error) {
    await client.query("ROLLBACK");
    throw error;
  } finally {
    client.release();
  }
}

function usageOutboxPayload(event: UsageChargeExport): JsonObject {
  return {
    tenant_id: event.tenantId,
    charge_id: event.chargeId,
    account_id: event.accountId,
    subject_id: event.subjectId,
    operation: event.operation,
    feature: event.feature,
    model: event.model,
    region: event.region,
    measures: event.measures,
    dimensions: event.dimensions,
    metadata: event.metadata,
    requested: event.requested,
    charged: event.charged,
    allowance_requested: event.allowanceRequested,
    allowance_covered: event.allowanceCovered,
    billing_disposition: event.billingDisposition,
    catalog_revision_id: event.catalogRevisionId,
    plan_id: event.planId,
    rate_card_key: event.rateCardKey,
    pricing_snapshot: event.pricingSnapshot,
    ledger_entry_id: event.ledgerEntryId,
    correction_of_charge_id: event.correctionOfChargeId,
    idempotency_key: event.idempotencyKey,
    request_digest: event.requestDigest,
    event_at: event.eventAt,
    created_at: event.createdAt,
  };
}

function billingOutboxPayload(event: BillingEventPayloadExport): JsonObject {
  return {
    tenant_id: event.tenantId,
    event_id: event.eventId,
    provider: event.provider,
    provider_environment: event.providerEnvironment,
    provider_event_id: event.providerEventId,
    event_type: event.eventType,
    status: event.status,
    received_at: event.receivedAt,
    completed_at: event.completedAt,
    envelope: event.envelope,
    object_key: event.objectKey,
    object_version: event.objectVersion,
    archived_at: event.archivedAt,
  };
}

interface OutboxReplay {
  sourceAggregateId: string;
  topic: string;
  aggregateId: string;
  idempotencyKey: string;
  payload?: JsonObject;
  payloadVersion?: number;
  patch?: { key: string; value: string };
}

async function enqueueOutboxReplay(pool: pg.Pool, replay: OutboxReplay): Promise<void> {
  await pool.query(
    `INSERT INTO bursar.event_outbox(
       tenant_id, topic, aggregate_type, aggregate_id, idempotency_key,
       status, payload_version, payload
     )
     SELECT tenant_id, topic, aggregate_type, $1::uuid, $2::text,
            'pending', COALESCE($3::integer, payload_version),
            CASE
              WHEN $4::jsonb IS NOT NULL THEN $4::jsonb
              WHEN $5::text IS NOT NULL
                THEN jsonb_set(payload, ARRAY[$5::text], to_jsonb($6::text))
              ELSE payload
            END
     FROM bursar.event_outbox
     WHERE aggregate_id = $7::uuid AND topic = $8::text
     ORDER BY id
     LIMIT 1`,
    [
      replay.aggregateId,
      replay.idempotencyKey,
      replay.payloadVersion ?? null,
      replay.payload === undefined ? null : JSON.stringify(replay.payload),
      replay.patch?.key ?? null,
      replay.patch?.value ?? null,
      replay.sourceAggregateId,
      replay.topic,
    ],
  );
}

async function seedDeadLetters(pool: pg.Pool): Promise<string[]> {
  await pool.query("SELECT bursar.create_tenant($1::uuid, $2::text, $3::text)", [
    OTHER_TENANT_ID,
    "outbox-other",
    "Outbox other tenant",
  ]);
  const eventIds: string[] = [];
  for (let index = 0; index < 2; index += 1) {
    const inserted = await pool.query<{ id: string }>(
      `INSERT INTO bursar.event_outbox(
         tenant_id, topic, aggregate_type, aggregate_id, idempotency_key,
         status, attempt_count, last_error, created_at
       )
       VALUES (
         $1::uuid, 'usage.charge_recorded', 'credit_usage_charge',
         gen_random_uuid(), $2::text, 'dead_letter', 10,
         'outbox_delivery_failed:Error', $3::timestamptz
       )
       RETURNING id::text`,
      [
        TEST_TENANT_ID,
        `outbox-recovery-${index}`,
        new Date(Date.UTC(2026, 7, 10, 0, 0, index)).toISOString(),
      ],
    );
    eventIds.push(inserted.rows[0]!.id);
  }
  await pool.query(
    `INSERT INTO bursar.event_outbox(
       tenant_id, topic, aggregate_type, aggregate_id, idempotency_key,
       status, attempt_count, last_error
     )
     VALUES (
       $1::uuid, 'usage.charge_recorded', 'credit_usage_charge',
       gen_random_uuid(), 'outbox-other-tenant', 'dead_letter', 10,
       'outbox_delivery_failed:Error'
     )`,
    [OTHER_TENANT_ID],
  );
  return eventIds;
}

describe.runIf(DATABASE_URL)("PostgresStorageRepository integration", () => {
  let pool: pg.Pool;
  let operatorPool: pg.Pool;
  let postgres: PostgresClient;
  let repository: PostgresStorageRepository;

  beforeAll(async () => {
    pool = new pg.Pool({ connectionString: DATABASE_URL!, max: 1 });
    operatorPool = new pg.Pool({ connectionString: DATABASE_URL!, max: 1 });
    await applyMigrations(pool);
    await truncateBursarTables(pool);
    postgres = new PostgresClient(pool, {
      tenantId: TEST_TENANT_ID,
      accessRole: "bursar_operator",
      providerEnvironment: "test",
    });
    repository = new PostgresStorageRepository(postgres.query, TEST_TENANT_ID);
  }, 60000);

  beforeEach(async () => {
    await truncateBursarTables(pool);
  });

  afterAll(async () => {
    await postgres?.close();
    await operatorPool?.end();
    await pool?.end();
  });

  it("exports payloads, archives billing envelopes, and acknowledges outbox events", async () => {
    const { chargeId, billingEventId } = await seedStorageRows(pool);

    const usage = await repository.getUsageCharge(chargeId);
    expect(usage).toMatchObject({
      chargeId,
      operation: "completion",
      model: "small-model",
      region: "in",
      dimensions: { tenant_tier: "starter" },
      metadata: { trace_id: "trace-1" },
    });
    const usageOutbox = await pool.query(
      "SELECT payload FROM bursar.event_outbox WHERE aggregate_id = $1::uuid",
      [chargeId],
    );
    const usageOutboxPayload = z.record(z.string(), z.json()).parse(usageOutbox.rows[0]?.payload);
    const eventAt = z.string().parse(usageOutboxPayload.event_at);
    const createdAt = z.string().parse(usageOutboxPayload.created_at);
    expect(usageOutboxPayload).toEqual({
      delivery_required: false,
      tenant_id: TEST_TENANT_ID,
      charge_id: chargeId,
      account_id: usage?.accountId,
      event_at: expect.any(String),
      created_at: expect.any(String),
    });
    expect(new Date(eventAt).getTime()).toBe(new Date(usage!.eventAt).getTime());
    expect(new Date(createdAt).getTime()).toBe(new Date(usage!.createdAt).getTime());

    const billingPayload = await repository.getBillingEventPayload(billingEventId);
    expect(billingPayload).toMatchObject({
      eventId: billingEventId,
      provider: "stripe",
      providerEventId: "evt-storage-repo-1",
      eventType: "invoice.paid",
      envelope: expect.objectContaining({ kind: "invoice.paid" }),
    });

    await expect(
      repository.archiveBillingEventPayload(
        billingEventId,
        "billing/stripe/evt-storage-repo-1.json",
        "version-1",
      ),
    ).resolves.toBe(true);
    await expect(repository.getBillingEventPayload(billingEventId)).resolves.toMatchObject({
      envelope: null,
      objectKey: "billing/stripe/evt-storage-repo-1.json",
      objectVersion: "version-1",
    });

    const claimed = await repository.claim(
      ["usage.charge_recorded", "billing.webhook_completed"],
      10,
      60,
    );
    expect(claimed.map((event) => event.topic).sort()).toEqual([
      "billing.webhook_completed",
      "usage.charge_recorded",
    ]);
    const usageEvent = claimed.find((event) => event.topic === "usage.charge_recorded")!;
    const billingEvent = claimed.find((event) => event.topic === "billing.webhook_completed")!;
    await expect(repository.complete(usageEvent)).resolves.toBe(true);
    await expect(repository.fail(billingEvent, "outbox_delivery_failed:Error", 0, 3)).resolves.toBe(
      true,
    );
  });

  it("recovers dead letters with bounded cursors, claim renewal, and tenant isolation", async () => {
    const eventIds = await seedDeadLetters(pool);

    await expect(repository.stats()).resolves.toMatchObject({
      pendingCount: 0,
      deadLetterCount: 2,
    });
    const firstPage = await repository.listDeadLetters({ limit: 1 });
    expect(firstPage.items.map(({ eventId }) => eventId)).toEqual([eventIds[0]]);
    expect(firstPage.nextCursor).not.toBeNull();
    const secondPage = await repository.listDeadLetters({
      limit: 1,
      cursor: firstPage.nextCursor,
    });
    expect(secondPage.items.map(({ eventId }) => eventId)).toEqual([eventIds[1]]);
    expect(secondPage.nextCursor).toBeNull();

    await expect(repository.requeue(eventIds[0]!)).resolves.toBe(true);
    await expect(repository.requeue(eventIds[0]!)).resolves.toBe(false);
    await expect(repository.stats()).resolves.toMatchObject({
      pendingCount: 1,
      deadLetterCount: 1,
    });

    const [claimed] = await repository.claim(["usage.charge_recorded"], 1, 1);
    expect(claimed?.eventId).toBe(eventIds[0]);
    await expect(repository.renew(claimed!, 60)).resolves.toBe(true);
    await expect(repository.complete(claimed!)).resolves.toBe(true);
    await expect(repository.complete(claimed!)).resolves.toBe(false);

    await expect(repository.requeue(eventIds[1]!)).resolves.toBe(true);
    const [retry] = await repository.claim(["usage.charge_recorded"], 1, 60);
    expect(retry?.eventId).toBe(eventIds[1]);
    await expect(repository.fail(retry!, "outbox_delivery_failed:Error", 0, 1)).resolves.toBe(true);
    await expect(repository.stats()).resolves.toMatchObject({ deadLetterCount: 1 });

    const crossTenant = new PostgresStorageRepository(postgres.query, OTHER_TENANT_ID);
    await expect(crossTenant.stats()).rejects.toMatchObject({
      name: "StoreError",
      code: "STORE_ERROR",
      message: "PostgreSQL query failed",
      retryable: false,
      indeterminate: false,
      details: expect.objectContaining({ sqlState: "42501" }),
    });
  });

  it("flushes usage and billing outbox events through runtime handlers", async () => {
    const { billingEventId } = await seedStorageRows(pool);
    const usageWrites: Array<{ event: UsageChargeExport; outboxEventId: string }> = [];
    const archivedBillingEvents: BillingEventPayloadExport[] = [];
    let initialized = false;
    let archiveClosed = false;
    const clickhouse = {
      initialize: async () => {
        initialized = true;
      },
      writeUsage: async (event: UsageChargeExport, outboxEventId: string) => {
        usageWrites.push({ event, outboxEventId });
      },
      spendByUser: async () => [],
      spendByModel: async () => [],
      topUsers: async () => [],
      dailySpend: async () => [],
      aggregateStats: async () => ({
        totalCreditsConsumed: new Decimal(0),
        activeUsers: 0,
        avgDailySpend: new Decimal(0),
        topModel: "",
        topUser: "",
      }),
    };
    const s3 = {
      archive: async (event: BillingEventPayloadExport) => {
        archivedBillingEvents.push(event);
        return {
          key: `archive/${event.providerEventId}.json`,
          versionId: "version-runtime",
        };
      },
      close: async () => {
        archiveClosed = true;
      },
    };
    const runtime = await createBursarRuntime({
      postgres: pool,
      operatorPostgres: operatorPool,
      tenantId: TEST_TENANT_ID,
      providerEnvironment: "test",
      clickhouse,
      s3,
      outbox: { batchSize: 10, pollIntervalMs: 60_000 },
    });
    try {
      expect(runtime.health()).toMatchObject({ started: false, closed: false });
      await runtime.start({ loadCatalog: false });
      await runtime.start({ loadCatalog: false });
      expect(initialized).toBe(true);
      const result = await runtime.flush();
      expect(result).toEqual({ claimed: 2, delivered: 2, failed: 0, claimLost: 0 });
      expect(usageWrites[0]?.event.operation).toBe("completion");
      expect(archivedBillingEvents[0]?.eventId).toBe(billingEventId);
      await expect(repository.getBillingEventPayload(billingEventId)).resolves.toMatchObject({
        envelope: null,
        objectKey: "archive/evt-storage-repo-1.json",
        objectVersion: "version-runtime",
      });
    } finally {
      await runtime.close();
    }
    expect(archiveClosed).toBe(true);
    await expect(runtime.flush()).rejects.toThrow("closed");
    await expect(runtime.worker?.stop()).resolves.toBeUndefined();
    await expect(Promise.resolve().then(() => runtime.worker!.runOnce())).rejects.toThrow(
      "stopped",
    );
  });

  it("coalesces usage exports through the optional batch sink", async () => {
    await seedUsageOnly(pool);
    await seedUsageOnly(pool, "storage-repo-usage-2", "trace-2");
    const usageRows = await pool.query<{ aggregate_id: string }>(
      `SELECT aggregate_id::text
       FROM bursar.event_outbox
       WHERE topic = 'usage.charge_recorded'
       ORDER BY id`,
    );
    for (const row of usageRows.rows) {
      const usage = await repository.getUsageCharge(row.aggregate_id);
      await pool.query(
        `UPDATE bursar.event_outbox SET payload = $1::jsonb
         WHERE aggregate_id = $2::uuid AND topic = 'usage.charge_recorded'`,
        [JSON.stringify(usageOutboxPayload(usage!)), row.aggregate_id],
      );
    }
    const batches: (readonly [UsageChargeExport, string])[][] = [];
    const clickhouse = {
      initialize: async () => {},
      writeUsage: async () => {},
      writeUsageBatch: async (
        entries: readonly (readonly [UsageChargeExport, string])[],
      ): Promise<void> => {
        batches.push(Array.from(entries));
      },
      spendByUser: async () => [],
      spendByModel: async () => [],
      topUsers: async () => [],
      dailySpend: async () => [],
      aggregateStats: async () => ({
        totalCreditsConsumed: new Decimal(0),
        activeUsers: 0,
        avgDailySpend: new Decimal(0),
        topModel: "",
        topUser: "",
      }),
    };
    const runtime = await createBursarRuntime({
      postgres: pool,
      operatorPostgres: operatorPool,
      tenantId: TEST_TENANT_ID,
      providerEnvironment: "test",
      clickhouse,
      outbox: { batchSize: 10, pollIntervalMs: 60_000 },
    });
    try {
      await runtime.start({ loadCatalog: false });
      await expect(runtime.flush()).resolves.toEqual({
        claimed: 2,
        delivered: 2,
        failed: 0,
        claimLost: 0,
      });
      expect(batches).toHaveLength(1);
      expect(batches[0]).toHaveLength(2);
      expect(batches[0]?.[0]?.[0].operation).toBe("completion");
    } finally {
      await runtime.close();
    }
  });

  it("records a failed batch export while keeping the runtime closable", async () => {
    await seedUsageOnly(pool);
    const clickhouse = {
      initialize: async () => {},
      writeUsage: async () => {},
      writeUsageBatch: async (): Promise<void> => {
        throw new Error("analytics sink unavailable");
      },
      spendByUser: async () => [],
      spendByModel: async () => [],
      topUsers: async () => [],
      dailySpend: async () => [],
      aggregateStats: async () => ({
        totalCreditsConsumed: new Decimal(0),
        activeUsers: 0,
        avgDailySpend: new Decimal(0),
        topModel: "",
        topUser: "",
      }),
    };
    const runtime = await createBursarRuntime({
      postgres: pool,
      operatorPostgres: operatorPool,
      tenantId: TEST_TENANT_ID,
      providerEnvironment: "test",
      clickhouse,
      outbox: { batchSize: 10, pollIntervalMs: 60_000, attemptLimit: 1 },
    });
    try {
      await runtime.start({ loadCatalog: false });
      await expect(runtime.flush()).resolves.toMatchObject({
        claimed: 1,
        delivered: 0,
        failed: 1,
      });
    } finally {
      await runtime.close();
    }
  });

  it("composes tenant identity, diagnostics, and operator storage maintenance", async () => {
    const runtime = await createBursarRuntime({
      postgres: `${DATABASE_URL!}?application_name=bursar-runtime-tenant`,
      operatorPostgres: `${DATABASE_URL!}?application_name=bursar-runtime-operator`,
      tenantId: TEST_TENANT_ID,
      tenantSlug: " BURSAR-TESTS ",
      providerEnvironment: "test",
      outbox: false,
    });
    try {
      await runtime.start({ loadCatalog: false });
      expect(runtime.state()).toMatchObject({
        started: true,
        closed: false,
        catalogLoaded: false,
        worker: { configured: false, lifecycle: "not_configured" },
      });
      await expect(runtime.checkDependencies({ outboxLimit: 3 })).resolves.toMatchObject({
        postgres: { status: "ok" },
        catalog: { status: "ok", loaded: false },
        outbox: { status: "ok", limit: 3 },
      });

      await expect(
        runtime.maintenance.runOnce({
          limit: 1,
          now: new Date("2026-08-19T00:00:00.000Z"),
        }),
      ).resolves.toMatchObject({ status: "completed", count: expect.any(Number) });

      const forced = await runtime.operatorMaintenance.runOnce({
        mode: "force",
        now: new Date("2026-08-19T00:00:00.000Z"),
      });
      expect(forced).toMatchObject({ status: "completed", hasMore: expect.any(Boolean) });
      expect(forced.count).toBeGreaterThanOrEqual(0);

      const maintenanceLock = await pool.connect();
      try {
        await maintenanceLock.query("BEGIN");
        await maintenanceLock.query(
          "SELECT pg_advisory_xact_lock(hashtextextended('bursar.storage.maintenance', 0))",
        );
        await expect(runtime.operatorMaintenance.runOnce({ mode: "force" })).resolves.toMatchObject(
          {
            status: "busy",
            count: 0,
            hasMore: true,
          },
        );
      } finally {
        await maintenanceLock.query("ROLLBACK");
        maintenanceLock.release();
      }

      const partitionLock = await pool.connect();
      try {
        await partitionLock.query("BEGIN");
        await partitionLock.query(
          "SELECT pg_advisory_xact_lock(hashtextextended('bursar.storage.partition.usage_charge_payloads', 0))",
        );
        await expect(
          runtime.operatorMaintenance.runPartitionOnce("usage_charge_payloads"),
        ).resolves.toMatchObject({
          status: "busy",
          parentTable: "usage_charge_payloads",
          count: 0,
          hasMore: true,
        });
      } finally {
        await partitionLock.query("ROLLBACK");
        partitionLock.release();
      }

      const notDue = await runtime.operatorMaintenance.runOnce({
        mode: "ifDue",
        now: new Date("2026-08-19T00:00:01.000Z"),
      });
      expect(notDue).toMatchObject({ status: "not_due", count: 0, hasMore: false });

      for (const parentTable of ["usage_charge_payloads", "billing_event_payloads"] as const) {
        await expect(
          runtime.operatorMaintenance.runPartitionOnce(parentTable),
        ).resolves.toMatchObject({
          status: "completed",
          parentTable,
          count: expect.any(Number),
          defaultPartitionHasRows: expect.any(Boolean),
        });
      }
    } finally {
      await runtime.close();
    }

    expect(runtime.health()).toMatchObject({ started: true, closed: true });
    await expect(runtime.checkDependencies()).resolves.toMatchObject({
      ready: false,
      postgres: { status: "skipped", reason: "runtime is closed" },
      catalog: { status: "skipped" },
      outbox: { status: "skipped" },
    });
  });

  it("recovers a usage projection after losing its PostgreSQL lease", async () => {
    await seedUsageOnly(pool);
    let writes = 0;
    const outcomes: Array<{
      status: string;
      claimLossPhase: string | null;
      summary: string | null;
    }> = [];
    const clickhouse = {
      initialize: async () => {},
      writeUsage: async (_event: UsageChargeExport, outboxEventId: string) => {
        writes += 1;
        if (writes !== 1) return;
        const client = await pool.connect();
        try {
          await client.query("SELECT set_config('bursar.tenant_id', $1, true)", [TEST_TENANT_ID]);
          await client.query(
            `UPDATE bursar.event_outbox
             SET claim_expires_at = now() - interval '1 second'
             WHERE id = $1::bigint AND status = 'processing'`,
            [outboxEventId],
          );
          await new Promise((resolve) => setTimeout(resolve, 500));
        } finally {
          client.release();
        }
      },
      spendByUser: async () => [],
      spendByModel: async () => [],
      topUsers: async () => [],
      dailySpend: async () => [],
      aggregateStats: async () => ({
        totalCreditsConsumed: new Decimal(0),
        activeUsers: 0,
        avgDailySpend: new Decimal(0),
        topModel: "",
        topUser: "",
      }),
    };
    const runtime = await createBursarRuntime({
      postgres: pool,
      operatorPostgres: operatorPool,
      tenantId: TEST_TENANT_ID,
      providerEnvironment: "test",
      clickhouse,
      outbox: {
        batchSize: 1,
        leaseSeconds: 1,
        attemptLimit: 2,
        pollIntervalMs: 60_000,
        onEventOutcome: (outcome) => {
          outcomes.push({
            status: outcome.status,
            claimLossPhase: outcome.claimLossPhase,
            summary: outcome.summary,
          });
        },
      },
    });
    try {
      await expect(runtime.flush()).resolves.toMatchObject({
        claimed: 1,
        delivered: 0,
        failed: 1,
      });
      expect(writes).toBe(1);
      expect(outcomes).toHaveLength(1);
      await expect(runtime.flush()).resolves.toEqual({
        claimed: 1,
        delivered: 1,
        failed: 0,
        claimLost: 0,
      });
    } finally {
      await runtime.close();
    }
    expect(outcomes.map((outcome) => outcome.status)).toEqual(["claim_lost", "delivered"]);
    expect(outcomes[0]?.claimLossPhase).toBe("heartbeat");
  });

  it("projects and replays valid envelopes while rejecting unsafe persisted events", async () => {
    const { chargeId, billingEventId } = await seedStorageRows(pool);
    const usage = await repository.getUsageCharge(chargeId);
    const billing = await repository.getBillingEventPayload(billingEventId);
    expect(usage).not.toBeNull();
    expect(billing).not.toBeNull();
    await pool.query(
      `UPDATE bursar.event_outbox
       SET payload = $1::jsonb
      WHERE aggregate_id = $2::uuid AND topic = 'usage.charge_recorded'`,
      [JSON.stringify(usageOutboxPayload(usage!)), chargeId],
    );
    await pool.query(
      `UPDATE bursar.event_outbox
       SET topic = 'billing.webhook_received', payload = $1::jsonb
       WHERE aggregate_id = $2::uuid AND topic = 'billing.webhook_completed'`,
      [JSON.stringify(billingOutboxPayload(billing!)), billingEventId],
    );

    const usageWrites: string[] = [];
    const archives: string[] = [];
    const archivePointerMissingId = "00000000-0000-0000-0000-0000000000b6";
    const clickhouse = {
      initialize: async () => {},
      writeUsage: async (_event: UsageChargeExport, outboxEventId: string) => {
        usageWrites.push(outboxEventId);
      },
      spendByUser: async () => [],
      spendByModel: async () => [],
      topUsers: async () => [],
      dailySpend: async () => [],
      aggregateStats: async () => ({
        totalCreditsConsumed: new Decimal(0),
        activeUsers: 0,
        avgDailySpend: new Decimal(0),
        topModel: "",
        topUser: "",
      }),
    };
    const runtime = await createBursarRuntime({
      postgres: pool,
      operatorPostgres: operatorPool,
      tenantId: TEST_TENANT_ID,
      providerEnvironment: "test",
      clickhouse,
      s3: {
        archive: async (event: BillingEventPayloadExport) => {
          archives.push(event.eventId);
          return { key: `archive/${event.providerEventId}.json`, versionId: "version-complete" };
        },
      },
      outbox: { batchSize: 10, pollIntervalMs: 60_000 },
    });
    try {
      await expect(runtime.flush()).resolves.toEqual({
        claimed: 2,
        delivered: 2,
        failed: 0,
        claimLost: 0,
      });

      await enqueueOutboxReplay(pool, {
        sourceAggregateId: billingEventId,
        topic: "billing.webhook_received",
        aggregateId: billingEventId,
        idempotencyKey: "billing-replay-after-archive",
      });
      await expect(runtime.flush()).resolves.toEqual({
        claimed: 1,
        delivered: 1,
        failed: 0,
        claimLost: 0,
      });

      await enqueueOutboxReplay(pool, {
        sourceAggregateId: chargeId,
        topic: "usage.charge_recorded",
        aggregateId: "00000000-0000-0000-0000-0000000000b2",
        idempotencyKey: "usage-version-2",
        payloadVersion: 2,
      });
      await enqueueOutboxReplay(pool, {
        sourceAggregateId: billingEventId,
        topic: "billing.webhook_received",
        aggregateId: "00000000-0000-0000-0000-0000000000b4",
        idempotencyKey: "billing-incomplete",
        payload: {},
      });
      await enqueueOutboxReplay(pool, {
        sourceAggregateId: billingEventId,
        topic: "billing.webhook_received",
        aggregateId: archivePointerMissingId,
        idempotencyKey: "billing-archive-pointer-missing",
        patch: { key: "event_id", value: archivePointerMissingId },
      });
      await expect(runtime.flush()).resolves.toMatchObject({
        claimed: 3,
        delivered: 0,
        failed: 3,
      });

      await enqueueOutboxReplay(pool, {
        sourceAggregateId: chargeId,
        topic: "usage.charge_recorded",
        aggregateId: "00000000-0000-0000-0000-0000000000a1",
        idempotencyKey: "usage-tenant-integrity",
        patch: { key: "tenant_id", value: OTHER_TENANT_ID },
      });
      await enqueueOutboxReplay(pool, {
        sourceAggregateId: billingEventId,
        topic: "billing.webhook_received",
        aggregateId: "00000000-0000-0000-0000-0000000000a4",
        idempotencyKey: "billing-event-integrity",
        patch: {
          key: "event_id",
          value: "00000000-0000-0000-0000-0000000000a5",
        },
      });
      await expect(runtime.flush()).resolves.toMatchObject({
        claimed: 2,
        delivered: 0,
        failed: 2,
        claimLost: 0,
      });
    } finally {
      await runtime.close();
    }
    expect(usageWrites).toHaveLength(1);
    expect(archives).toEqual([billingEventId, archivePointerMissingId]);
  });

  it("records manual and background diagnostics when the operator database is unavailable", async () => {
    const failedOperatorPool = new pg.Pool({ connectionString: DATABASE_URL!, max: 1 });
    const clickhouse = {
      initialize: async () => {},
      writeUsage: async () => {},
      spendByUser: async () => [],
      spendByModel: async () => [],
      topUsers: async () => [],
      dailySpend: async () => [],
      aggregateStats: async () => ({
        totalCreditsConsumed: new Decimal(0),
        activeUsers: 0,
        avgDailySpend: new Decimal(0),
        topModel: "",
        topUser: "",
      }),
    };
    const runtime = await createBursarRuntime({
      postgres: pool,
      operatorPostgres: failedOperatorPool,
      tenantId: TEST_TENANT_ID,
      providerEnvironment: "test",
      clickhouse,
      outbox: { batchSize: 1, pollIntervalMs: 60_000 },
    });
    await failedOperatorPool.end();
    try {
      await expect(runtime.operatorMaintenance.runOnce({ mode: "force" })).resolves.toMatchObject({
        status: "failed",
        hasMore: true,
      });
      await expect(
        runtime.operatorMaintenance.runPartitionOnce("usage_charge_payloads"),
      ).resolves.toMatchObject({
        status: "failed",
        parentTable: "usage_charge_payloads",
        hasMore: true,
      });
      await expect(runtime.flush()).rejects.toThrow();
      expect(runtime.state().worker.lastRun).toMatchObject({ status: "failed", result: null });

      await runtime.start({ loadCatalog: false });
      await new Promise((resolve) => setTimeout(resolve, 100));
      expect(runtime.state().worker.lastError).toMatchObject({
        error: "outbox_worker_failed:STORE_ERROR",
      });
    } finally {
      await runtime.close();
    }
  });

  it("keeps a dangling usage outbox event retryable when its charge is gone", async () => {
    const aggregateId = "00000000-0000-0000-0000-0000000000d1";
    const client = await pool.connect();
    try {
      await client.query("BEGIN");
      await client.query("SELECT set_config('bursar.tenant_id', $1, true)", [TEST_TENANT_ID]);
      await client.query(
        `INSERT INTO bursar.event_outbox(
           tenant_id, topic, aggregate_type, aggregate_id, idempotency_key,
           status, payload_version, payload
         ) VALUES ($1::uuid, 'usage.charge_recorded', 'credit_usage_charge',
           $2::uuid, 'dangling-usage-retry', 'pending', 1,
           '{"delivery_required": false}'::jsonb)`,
        [TEST_TENANT_ID, aggregateId],
      );
      await client.query("COMMIT");
    } catch (error) {
      await client.query("ROLLBACK");
      throw error;
    } finally {
      client.release();
    }

    const writes: string[] = [];
    const runtime = await createBursarRuntime({
      postgres: pool,
      operatorPostgres: operatorPool,
      tenantId: TEST_TENANT_ID,
      providerEnvironment: "test",
      clickhouse: {
        initialize: async () => {},
        writeUsage: async (_event: UsageChargeExport, outboxEventId: string) => {
          writes.push(outboxEventId);
        },
        spendByUser: async () => [],
        spendByModel: async () => [],
        topUsers: async () => [],
        dailySpend: async () => [],
        aggregateStats: async () => ({
          totalCreditsConsumed: new Decimal(0),
          activeUsers: 0,
          avgDailySpend: new Decimal(0),
          topModel: "",
          topUser: "",
        }),
      },
      outbox: {
        batchSize: 1,
        attemptLimit: 1,
        pollIntervalMs: 60_000,
        onEventOutcome: () => Promise.reject(new Error("metrics sink unavailable")),
      },
    });
    try {
      await expect(runtime.flush()).resolves.toMatchObject({
        claimed: 1,
        delivered: 0,
        failed: 1,
      });
    } finally {
      await runtime.close();
    }
    expect(writes).toEqual([]);
  });

  it("leaves a billing event pending when archive-pointer recording loses its race", async () => {
    const { billingEventId } = await seedStorageRows(pool);
    let archiveCalls = 0;
    const runtime = await createBursarRuntime({
      postgres: pool,
      operatorPostgres: operatorPool,
      tenantId: TEST_TENANT_ID,
      providerEnvironment: "test",
      s3: {
        archive: async (event: BillingEventPayloadExport) => {
          archiveCalls += 1;
          const client = await pool.connect();
          try {
            await client.query("BEGIN");
            await client.query("SELECT set_config('bursar.tenant_id', $1, true)", [TEST_TENANT_ID]);
            await client.query("SELECT set_config('bursar.provider_environment', 'test', true)");
            await client.query(
              `UPDATE bursar.billing_events
               SET payload_object_key = 'archive/race-winner.json',
                   payload_object_version = 'race-version', payload_archived_at = now()
               WHERE id = $1::uuid AND payload_object_key IS NULL`,
              [event.eventId],
            );
            await client.query("COMMIT");
          } catch (error) {
            await client.query("ROLLBACK");
            throw error;
          } finally {
            client.release();
          }
          return { key: "archive/loser.json", versionId: "loser-version" };
        },
      },
      outbox: { batchSize: 1, attemptLimit: 1, pollIntervalMs: 60_000 },
    });
    try {
      await expect(runtime.flush()).resolves.toMatchObject({
        claimed: 1,
        delivered: 0,
        failed: 1,
      });
    } finally {
      await runtime.close();
    }
    expect(archiveCalls).toBe(1);
    await expect(repository.getBillingEventPayload(billingEventId)).resolves.toMatchObject({
      objectKey: "archive/race-winner.json",
      objectVersion: "race-version",
    });
  });

  it("closes cleanly when tenant verification fails during startup", async () => {
    await pool.query("SELECT bursar.create_tenant($1::uuid, $2::text, $3::text)", [
      OTHER_TENANT_ID,
      "missing-tenant",
      "Missing tenant",
    ]);
    const runtime = await createBursarRuntime({
      postgres: pool,
      operatorPostgres: operatorPool,
      tenantId: TEST_TENANT_ID,
      tenantSlug: "missing-tenant",
      providerEnvironment: "test",
      outbox: false,
    });
    const start = runtime.start({ loadCatalog: false });
    await expect(runtime.close()).resolves.toBeUndefined();
    await expect(start).rejects.toThrow(/resolves to a different tenant/);
    expect(runtime.health()).toMatchObject({ started: false, closed: true });
  });
});
