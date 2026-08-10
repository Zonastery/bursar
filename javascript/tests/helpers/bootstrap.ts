/** Shared migration utilities for integration tests against PostgreSQL. */
import { readdirSync, readFileSync } from "fs";
import { join, dirname } from "path";
import { fileURLToPath } from "url";
import { createHash } from "crypto";
import pg from "pg";

const __dirname = dirname(fileURLToPath(import.meta.url));
const SQL_DIR = join(__dirname, "../../../python/src/bursar/sql");
const RESET_SQL = readFileSync(join(__dirname, "../../../tests/postgres/reset_bursar.sql"), "utf8");
export const TEST_TENANT_ID = "00000000-0000-0000-0000-000000000001";

export function validateMigrationFiles(files: string[]): string[] {
  const sorted = [...files].sort();
  if (sorted.length === 0) throw new Error("Bursar package contains no SQL migrations");
  for (const [index, file] of sorted.entries()) {
    const expectedPrefix = String(index + 1).padStart(3, "0");
    const match = /^(\d{3})_[a-z0-9]+(?:_[a-z0-9]+)*\.sql$/.exec(file);
    if (match?.[1] !== expectedPrefix) {
      throw new Error(
        `migration files must be contiguous NNN_name.sql entries; expected ${expectedPrefix}, found ${file}`,
      );
    }
  }
  return sorted;
}

function migrationFiles(): string[] {
  return validateMigrationFiles(readdirSync(SQL_DIR).filter((file) => file.endsWith(".sql")));
}

export async function applyMigrations(pool: pg.Pool): Promise<void> {
  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    await client.query("SELECT set_config('lock_timeout', '30000ms', true)");
    await client.query("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", [
      "bursar:migrations",
    ]);
    await client.query("CREATE SCHEMA IF NOT EXISTS bursar");
    await client.query(`CREATE TABLE IF NOT EXISTS bursar.schema_migrations (
      version text PRIMARY KEY, checksum text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now()
    )`);

    const files = migrationFiles();
    const knownVersions = new Set(files);
    const appliedVersions = await client.query<{ version: string }>(
      "SELECT version FROM bursar.schema_migrations ORDER BY version",
    );
    const unknownVersions = appliedVersions.rows
      .map(({ version }) => version)
      .filter((version) => !knownVersions.has(version));
    if (unknownVersions.length > 0) {
      throw new Error(`migration ledger contains unknown versions: ${unknownVersions.join(", ")}`);
    }

    for (const file of files) {
      const sql = readFileSync(join(SQL_DIR, file), "utf8");
      const checksum = createHash("sha256").update(sql).digest("hex");
      const applied = await client.query(
        "SELECT checksum FROM bursar.schema_migrations WHERE version = $1",
        [file],
      );
      if (applied.rows[0]) {
        if (applied.rows[0].checksum !== checksum) {
          throw new Error(`migration checksum mismatch for ${file}`);
        }
        continue;
      }
      await client.query(sql);
      await client.query(
        "INSERT INTO bursar.schema_migrations(version, checksum) VALUES ($1, $2)",
        [file, checksum],
      );
    }
    await client.query("SELECT bursar.create_tenant($1::uuid, $2::text, $3::text)", [
      TEST_TENANT_ID,
      "bursar-tests",
      "Bursar tests",
    ]);
    await client.query("COMMIT");
  } catch (error) {
    await client.query("ROLLBACK").catch(() => {});
    throw error;
  } finally {
    client.release();
  }
}

/** Reset all Bursar data and provision the canonical test tenant. */
export async function truncateBursarTables(pool: pg.Pool): Promise<void> {
  await pool.query(RESET_SQL);
  await pool.query("SELECT bursar.create_tenant($1::uuid, $2::text, $3::text)", [
    TEST_TENANT_ID,
    "bursar-tests",
    "Bursar tests",
  ]);
}
