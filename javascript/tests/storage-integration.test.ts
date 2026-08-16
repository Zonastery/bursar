/**
 * DB-backed storage repository integration tests for the JavaScript SDK.
 */

import { afterAll, beforeAll, beforeEach, describe, expect, inject, it } from "vitest";
import { Decimal } from "decimal.js";
import pg from "pg";
import { z } from "zod";
import { PostgresClient } from "../src/shared/postgres-client.js";
import { createBursarRuntime } from "../src/storage/runtime.js";
import { PostgresStorageRepository } from "../src/storage/postgres-repository.js";
import type { BillingEventPayloadExport, UsageChargeExport } from "../src/storage/ports.js";
import { TEST_TENANT_ID, applyMigrations, truncateBursarTables } from "./helpers/bootstrap.js";

const DATABASE_URL = inject("DATABASE_URL");
const OTHER_TENANT_ID = "00000000-0000-0000-0000-000000000002";

async function seedStorageRows(pool: pg.Pool): Promise<{
  chargeId: string;
  billingEventId: string;
}> {
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
        'storage-repo-usage-1',
        p_model => 'small-model',
        p_region => 'in',
        p_measures => '{"input_tokens":12}'::jsonb,
        p_dimensions => '{"tenant_tier":"starter"}'::jsonb,
        p_metadata => '{"trace_id":"trace-1"}'::jsonb
      )
      `,
      [subjectId],
    );
    expect(usage.rows[0]?.error_code).toBeNull();

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
    return {
      chargeId: String(usage.rows[0]?.charge_id),
      billingEventId,
    };
  } catch (error) {
    await client.query("ROLLBACK");
    throw error;
  } finally {
    client.release();
  }
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
  });
});
