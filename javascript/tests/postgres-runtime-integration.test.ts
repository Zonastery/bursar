/**
 * PostgreSQL-backed lifecycle tests for SDK-owned connections.
 *
 * Existing storage tests primarily pass borrowed pools. These scenarios cover
 * the production path where the SDK creates, configures, and closes its own
 * node-postgres pools.
 */

import { afterAll, beforeAll, describe, expect, inject, it } from "vitest";
import { Decimal } from "decimal.js";
import pg from "pg";
import { PostgresClient } from "../src/shared/postgres-client.js";
import { StoreClosedError } from "../src/errors.js";
import { createBursarRuntime } from "../src/storage/runtime.js";
import { TEST_TENANT_ID, applyMigrations, truncateBursarTables } from "./helpers/bootstrap.js";

const DATABASE_URL = inject("DATABASE_URL");
const UNKNOWN_SUBJECT_ID = "00000000-0000-0000-0000-0000000000f1";

describe.runIf(DATABASE_URL)("PostgreSQL-owned runtime integration", () => {
  let adminPool: pg.Pool;

  beforeAll(async () => {
    adminPool = new pg.Pool({ connectionString: DATABASE_URL!, max: 2 });
    await applyMigrations(adminPool);
    await truncateBursarTables(adminPool);
  }, 60_000);

  afterAll(async () => {
    await adminPool?.end();
  });

  it("executes an unscoped query through an SDK-owned pool and closes it idempotently", async () => {
    const client = new PostgresClient(DATABASE_URL!, {
      providerEnvironment: "test",
      applicationName: "bursar-owned-client-test",
      maxConnections: 1,
    });

    try {
      await expect(
        client.query("SELECT current_setting('application_name') AS application_name"),
      ).resolves.toEqual([
        {
          application_name: "bursar-owned-client-test",
        },
      ]);
    } finally {
      const firstClose = client.close();
      expect(client.close()).toBe(firstClose);
      await firstClose;
    }

    await expect(client.query("SELECT 1")).rejects.toBeInstanceOf(StoreClosedError);
  });

  it("starts and closes a runtime with two SDK-owned tenant-scoped pools", async () => {
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
      postgres: `${DATABASE_URL!}?application_name=bursar-owned-runtime`,
      operatorPostgres: `${DATABASE_URL!}?application_name=bursar-owned-operator`,
      tenantId: TEST_TENANT_ID,
      tenantSlug: " BURSAR-TESTS ",
      providerEnvironment: "test",
      clickhouse,
      outbox: { batchSize: 1, pollIntervalMs: 60_000 },
    });

    await runtime.start({ loadCatalog: false });
    try {
      expect(runtime.health()).toMatchObject({
        started: true,
        closed: false,
        catalogLoaded: false,
      });
      await expect(runtime.checkDependencies({ outboxLimit: 1 })).resolves.toMatchObject({
        postgres: { status: "ok" },
        catalog: { status: "ok", loaded: false },
        outbox: { status: "ok", limit: 1 },
      });
      await expect(
        runtime.bursar.credits.getLedgerEntry(UNKNOWN_SUBJECT_ID, UNKNOWN_SUBJECT_ID),
      ).resolves.toBeNull();
      await expect(runtime.bursar.credits.getAvailable(UNKNOWN_SUBJECT_ID)).resolves.toMatchObject({
        userId: UNKNOWN_SUBJECT_ID,
        balance: new Decimal(0),
        reserved: new Decimal(0),
        available: new Decimal(0),
      });
    } finally {
      const firstClose = runtime.close();
      expect(runtime.close()).toBe(firstClose);
      await firstClose;
    }

    expect(runtime.health()).toMatchObject({ started: true, closed: true, ready: false });
    await expect(runtime.flush()).rejects.toThrow("BursarRuntime has been closed");
  });
});
