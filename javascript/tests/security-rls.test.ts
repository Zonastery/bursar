import { afterAll, beforeAll, describe, expect, inject, it } from "vitest";
import pg from "pg";
import type { BursarConfigData } from "../src/config.js";
import { PostgresStore } from "../src/credits/postgres/store.js";
import { TEST_TENANT_ID, applyMigrations, truncateBursarTables } from "./helpers/bootstrap.js";

const DATABASE_URL = inject("DATABASE_URL");
const SECOND_TENANT_ID = "00000000-0000-0000-0000-000000000002";

function catalogConfig(displayName: string): BursarConfigData {
  return {
    version: 1,
    catalog: { default_plan: "pro" },
    pricing: {
      operations: {
        completion: {
          measures: { tokens: { unit: "token" } },
          dimensions: {},
        },
      },
      rate_cards: {
        standard: {
          operations: {
            completion: {
              rules: [],
              unmatched: {
                action: "charge",
                charge: { type: "expression", formula: "tokens" },
              },
            },
          },
        },
      },
    },
    credits: {
      buckets: { purchased: { priority: 1, expiry: { type: "never" } } },
      default_bucket: "purchased",
    },
    plans: {
      pro: {
        display_name: displayName,
        rank: 0,
        rate_card: "standard",
        allowed_operations: ["completion"],
      },
    },
  };
}

describe.runIf(DATABASE_URL)("configuration catalog RLS", () => {
  const pool = new pg.Pool({ connectionString: DATABASE_URL!, max: 1 });
  beforeAll(async () => {
    await applyMigrations(pool);
    await truncateBursarTables(pool);
    await pool.query("SELECT bursar.create_tenant($1::uuid, $2::text, $3::text)", [
      SECOND_TENANT_ID,
      "security-second",
      "Security second tenant",
    ]);
  }, 60_000);
  afterAll(async () => {
    await pool.end();
  });

  it("scopes catalog RPC state by tenant and denies private table access", async () => {
    const firstStore = new PostgresStore({
      postgres: pool,
      tenantId: TEST_TENANT_ID,
      providerEnvironment: "test",
    });
    const secondStore = new PostgresStore({
      postgres: pool,
      tenantId: SECOND_TENANT_ID,
      providerEnvironment: "test",
    });

    let firstRevisionId: string;
    let secondRevisionId: string;
    try {
      firstRevisionId = await firstStore.publishAndActivateCatalog(
        catalogConfig("First tenant Pro"),
        "first-tenant",
      );
      secondRevisionId = await secondStore.publishAndActivateCatalog(
        catalogConfig("Second tenant Pro"),
        "second-tenant",
      );

      expect(firstRevisionId).not.toBe(secondRevisionId);
      await expect(firstStore.getActiveCatalog()).resolves.toMatchObject({
        id: firstRevisionId,
        version: 1,
        config: { plans: { pro: { display_name: "First tenant Pro" } } },
      });
      await expect(secondStore.getActiveCatalog()).resolves.toMatchObject({
        id: secondRevisionId,
        version: 1,
        config: { plans: { pro: { display_name: "Second tenant Pro" } } },
      });
    } finally {
      await Promise.all([firstStore.close(), secondStore.close()]);
    }

    const client = await pool.connect();
    try {
      await client.query("BEGIN");
      await client.query("SET LOCAL ROLE bursar_client");
      await client.query("SELECT set_config('bursar.provider_environment', 'test', true)");

      const activeCatalog = async (tenantId: string) => {
        await client.query("SELECT set_config('bursar.tenant_id', $1, true)", [tenantId]);
        return client.query<{
          tenant_id: string;
          id: string;
          revision_no: string;
          status: string;
          label: string;
          display_name: string;
        }>(`SELECT
              (revision).tenant_id::text AS tenant_id,
              (revision).id::text AS id,
              (revision).revision_no::text AS revision_no,
              (revision).status::text AS status,
              (revision).label AS label,
              (revision).source_document #>> '{plans,pro,display_name}' AS display_name
            FROM (
              SELECT bursar.active_catalog_revision() AS revision
            ) AS active`);
      };

      const firstActive = await activeCatalog(TEST_TENANT_ID);
      expect(firstActive.rows).toEqual([
        {
          tenant_id: TEST_TENANT_ID,
          id: firstRevisionId,
          revision_no: "1",
          status: "active",
          label: "first-tenant",
          display_name: "First tenant Pro",
        },
      ]);

      const secondActive = await activeCatalog(SECOND_TENANT_ID);
      expect(secondActive.rows).toEqual([
        {
          tenant_id: SECOND_TENANT_ID,
          id: secondRevisionId,
          revision_no: "1",
          status: "active",
          label: "second-tenant",
          display_name: "Second tenant Pro",
        },
      ]);

      await expect(
        client.query("SELECT count(*) FROM bursar.catalog_revisions"),
      ).rejects.toMatchObject({
        code: "42501",
      });
    } finally {
      await client.query("ROLLBACK").catch(() => undefined);
      client.release();
    }
  });
});
