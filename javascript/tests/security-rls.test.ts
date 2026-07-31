import { afterAll, beforeAll, describe, expect, inject, it } from "vitest";
import pg from "pg";
import { BOOTSTRAP_SQL, applyMigrations } from "./helpers/bootstrap.js";

const DATABASE_URL = process.env.DATABASE_URL ?? inject("DATABASE_URL");

describe.runIf(DATABASE_URL)("configuration catalog migration smoke test", () => {
  const pool = new pg.Pool({ connectionString: DATABASE_URL!, max: 1 });
  beforeAll(async () => {
    await pool.query(BOOTSTRAP_SQL);
    await applyMigrations(pool);
  }, 60_000);
  afterAll(async () => pool.end());
  it("creates the private normalized Bursar catalog", async () => {
    const result = await pool.query("SELECT to_regclass('bursar.catalog_revisions') AS catalog");
    expect(result.rows[0].catalog).toBe("bursar.catalog_revisions");
  });
});
