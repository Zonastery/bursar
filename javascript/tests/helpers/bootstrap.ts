/** Shared migration utilities for integration tests against PostgreSQL. */
import { readdirSync, readFileSync } from "fs";
import { join, dirname } from "path";
import { fileURLToPath } from "url";
import { createHash } from "crypto";
import pg from "pg";

const __dirname = dirname(fileURLToPath(import.meta.url));
const SQL_DIR = join(__dirname, "../../../python/src/bursar/sql");
export const TEST_TENANT_ID = "00000000-0000-0000-0000-000000000001";

function migrationFiles(): string[] {
  return readdirSync(SQL_DIR)
    .filter((f) => f.endsWith(".sql"))
    .sort();
}

export async function applyMigrations(pool: pg.Pool): Promise<void> {
  // Production setup owns the checksum ledger. The bundled SQL is a baseline
  // dump, so a missing ledger on an existing schema must be repaired by
  // stamping it rather than replaying CREATE TABLE/constraint statements.
  const ledger = await pool.query("SELECT to_regclass('bursar.schema_migrations') AS relation");
  if (!ledger.rows[0]?.relation) {
    await pool.query(`CREATE SCHEMA IF NOT EXISTS bursar`);
    await pool.query(`CREATE TABLE bursar.schema_migrations (
      version text PRIMARY KEY, checksum text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now()
    )`);
  }

  const state = await pool.query(
    "SELECT to_regclass('bursar.credit_transactions') AS relation, " +
      "count(*)::int AS ledger_count FROM bursar.schema_migrations",
  );
  if (state.rows[0]?.relation && state.rows[0].ledger_count === 0) {
    for (const file of migrationFiles()) {
      const sql = readFileSync(join(SQL_DIR, file), "utf8");
      const checksum = createHash("sha256").update(sql).digest("hex");
      await pool.query("INSERT INTO bursar.schema_migrations(version, checksum) VALUES ($1, $2)", [
        file,
        checksum,
      ]);
    }
    await pool.query("SELECT bursar.create_tenant($1::uuid, $2::text, $3::text)", [
      TEST_TENANT_ID,
      "bursar-tests",
      "Bursar tests",
    ]);
    return;
  }

  for (const file of migrationFiles()) {
    const sql = readFileSync(join(SQL_DIR, file), "utf8");
    const checksum = createHash("sha256").update(sql).digest("hex");
    const applied = await pool.query(
      "SELECT checksum FROM bursar.schema_migrations WHERE version = $1",
      [file],
    );
    if (applied.rows[0]) {
      if (applied.rows[0].checksum !== checksum) {
        throw new Error(`migration checksum mismatch for ${file}`);
      }
      continue;
    }
    await pool.query(sql);
    await pool.query("INSERT INTO bursar.schema_migrations(version, checksum) VALUES ($1, $2)", [
      file,
      checksum,
    ]);
  }
  await pool.query("SELECT bursar.create_tenant($1::uuid, $2::text, $3::text)", [
    TEST_TENANT_ID,
    "bursar-tests",
    "Bursar tests",
  ]);
}

/** Truncate all tenant-owned Bursar tables in one dependency-safe statement. */
export async function truncateBursarTables(pool: pg.Pool): Promise<void> {
  await pool.query(`
    DO $$
    DECLARE
      tenant_tables text;
    BEGIN
      SELECT string_agg(
        format('bursar.%I', table_info.relname),
        ', ' ORDER BY table_info.relname
      )
      INTO tenant_tables
      FROM pg_class AS table_info
      JOIN pg_namespace AS namespace_info
        ON namespace_info.oid = table_info.relnamespace
      WHERE namespace_info.nspname = 'bursar'
        AND table_info.relkind IN ('r', 'p')
        AND NOT table_info.relispartition
        AND EXISTS (
          SELECT 1
          FROM pg_attribute AS attribute_info
          WHERE attribute_info.attrelid = table_info.oid
            AND attribute_info.attname = 'tenant_id'
            AND NOT attribute_info.attisdropped
        );

      IF tenant_tables IS NOT NULL THEN
        EXECUTE 'TRUNCATE TABLE ' || tenant_tables || ' CASCADE';
      END IF;
    EXCEPTION WHEN undefined_table THEN NULL;
    END $$;
  `);
}
