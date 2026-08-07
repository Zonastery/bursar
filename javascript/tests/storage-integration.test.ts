/**
 * DB-backed storage repository integration tests for the JavaScript SDK.
 */

import { afterAll, beforeAll, beforeEach, describe, expect, inject, it } from "vitest";
import Decimal from "decimal.js";
import pg from "pg";
import { PostgresClient } from "../src/shared/postgres-client.js";
import { createBursarRuntime } from "../src/storage/runtime.js";
import { PostgresStorageRepository } from "../src/storage/postgres-repository.js";
import type { BillingEventPayloadExport, UsageChargeExport } from "../src/storage/ports.js";
import { TEST_TENANT_ID, applyMigrations, truncateBursarTables } from "./helpers/bootstrap.js";

const DATABASE_URL = process.env.DATABASE_URL ?? inject("DATABASE_URL");

async function seedStorageRows(pool: pg.Pool): Promise<{
  chargeId: string;
  billingEventId: string;
}> {
  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    await client.query("SELECT set_config('bursar.tenant_id', $1, true)", [TEST_TENANT_ID]);
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

describe.runIf(DATABASE_URL)("PostgresStorageRepository integration", () => {
  let pool: pg.Pool;
  let postgres: PostgresClient;
  let repository: PostgresStorageRepository;

  beforeAll(async () => {
    pool = new pg.Pool({ connectionString: DATABASE_URL!, max: 1 });
    await applyMigrations(pool);
    await truncateBursarTables(pool);
    postgres = new PostgresClient(pool, {
      tenantId: TEST_TENANT_ID,
      accessRole: "bursar_operator",
    });
    repository = new PostgresStorageRepository(postgres.query, TEST_TENANT_ID);
  }, 60000);

  beforeEach(async () => {
    await truncateBursarTables(pool);
  });

  afterAll(async () => {
    await postgres?.close();
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
    expect(usageOutbox.rows[0]?.payload).toEqual({
      delivery_required: false,
      tenant_id: TEST_TENANT_ID,
      charge_id: chargeId,
      account_id: usage?.accountId,
      event_at: usage?.eventAt,
      created_at: usage?.createdAt,
    });

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
    await expect(repository.fail(billingEvent, "archive unavailable", 0, 3)).resolves.toBe(true);
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
      tenantId: TEST_TENANT_ID,
      clickhouse,
      s3,
      outbox: { batchSize: 10, pollIntervalMs: 60_000 },
    });
    try {
      expect(runtime.health()).toMatchObject({ started: false, closed: false });
      await runtime.start({ loadCatalog: false });
      expect(initialized).toBe(true);
      const result = await runtime.flush();
      expect(result).toEqual({ claimed: 2, delivered: 2, failed: 0 });
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
