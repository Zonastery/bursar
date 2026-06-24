import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { readdirSync, readFileSync } from "fs";
import { join, dirname } from "path";
import { fileURLToPath } from "url";
import pg from "pg";
import { PostgresStore } from "../src/stores/postgres-store.js";
import { CreditManager } from "../src/manager.js";
import type { PricingConfigData } from "../src/types.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const SQL_DIR = join(__dirname, "../../python/src/ducto/sql");
const DATABASE_URL = process.env.DATABASE_URL;

const TEST_PRICING: PricingConfigData = {
  version: 1,
  models: {
    "gpt-4": "input_tokens * 0.01 + output_tokens * 0.03",
    _default: "input_tokens * 0.001 + output_tokens * 0.003",
  },
  tools: { _default: "tool_calls * 0" },
  minBalance: 5,
};

const PG_USER = "00000000-0000-0000-0000-000000000001";
const METRICS = { model: "gpt-4", inputTokens: 100, outputTokens: 50 };
const EXPECTED_COST = 2; // 100*0.01 + 50*0.03 = 2.5 → 2

describe.runIf(DATABASE_URL)("PostgresStore integration", () => {
  let pool: pg.Pool;

  beforeAll(async () => {
    pool = new pg.Pool({ connectionString: DATABASE_URL });

    // Bootstrap auth.role() (no-op in Supabase, required in raw PG)
    await pool.query(`
      DO $$
      BEGIN
        IF NOT EXISTS (
          SELECT 1 FROM pg_proc p
          JOIN pg_namespace n ON n.oid = p.pronamespace
          WHERE n.nspname = 'auth' AND p.proname = 'role'
        ) THEN
          CREATE SCHEMA IF NOT EXISTS auth;
          CREATE FUNCTION auth.role() RETURNS text
          LANGUAGE SQL IMMUTABLE AS $func$ SELECT 'service_role'::text $func$;
          CREATE FUNCTION auth.uid() RETURNS uuid
          LANGUAGE SQL IMMUTABLE AS $func$ SELECT '00000000-0000-0000-0000-000000000000'::uuid $func$;
        END IF;
      END
      $$;
    `);

    // Run all SQL migrations
    const files = readdirSync(SQL_DIR).sort();
    for (const file of files) {
      if (!file.endsWith(".sql")) continue;
      const sql = readFileSync(join(SQL_DIR, file), "utf8");
      await pool.query(sql);
    }
  }, 30000);

  afterAll(async () => {
    if (pool) await pool.end();
  });

  it("setup is idempotent", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    const r1 = await store.setup();
    expect(r1.success).toBe(true);
    // second call should also succeed
    const store2 = new PostgresStore(DATABASE_URL!, pg.Pool);
    const r2 = await store2.setup();
    expect(r2.success).toBe(true);
  });

  it("full credit lifecycle: add → deduct → balance persists", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    const manager = new CreditManager(store);
    await manager.publishPricingFromDict(TEST_PRICING);

    await manager.addCredits(PG_USER, 100);

    const result = await manager.deduct(PG_USER, METRICS, "tx_1");
    expect(result.amount).toBe(-EXPECTED_COST);
    expect(result.balanceAfter).toBe(100 - EXPECTED_COST);

    const balance = await manager.getBalance(PG_USER);
    expect(balance.balance).toBe(100 - EXPECTED_COST);
  });

  it("balance persists across manager instances", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);

    const m1 = new CreditManager(store);
    await m1.publishPricingFromDict(TEST_PRICING);
    await m1.addCredits(PG_USER, 100);
    await m1.deduct(PG_USER, METRICS, "tx_2");

    const m2 = new CreditManager(store);
    await m2.loadPricingFromStore();
    const balance = await m2.getBalance(PG_USER);
    expect(balance.balance).toBe(100 - EXPECTED_COST);
  });

  it("insufficient credits raises error", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    const manager = new CreditManager(store);
    await manager.publishPricingFromDict(TEST_PRICING);

    await expect(() => manager.deduct("no-funds-user", METRICS)).rejects.toThrow(
      "Credit reservation failed",
    );
  });

  it("reserve and deduct flow", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    const manager = new CreditManager(store);
    await manager.publishPricingFromDict(TEST_PRICING);

    await manager.addCredits(PG_USER, 100);

    const reserve = await manager.reserveCredits(PG_USER, 30, "usage");
    expect(reserve.amount).toBe(30);

    // Over-reserve should be rejected
    await expect(() => manager.reserveCredits(PG_USER, 999, "usage")).rejects.toThrow(
      "insufficient_credits",
    );
  });
});
