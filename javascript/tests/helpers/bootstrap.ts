/**
 * Shared bootstrap utilities for JS integration tests against a real Postgres.
 *
 * Extracted from store-integration.test.ts, billing-integration.test.ts, and
 * security-rls.test.ts to eliminate duplication and ensure consistent
 * auth.uid()/auth.role() stubs across all three files.
 */
import { readdirSync, readFileSync } from "fs";
import { join, dirname } from "path";
import { fileURLToPath } from "url";
import { createHash } from "crypto";
import pg from "pg";

const __dirname = dirname(fileURLToPath(import.meta.url));
const SQL_DIR = join(__dirname, "../../../python/src/bursar/sql");

const BOOTSTRAP_SQL = `
DO $$ BEGIN CREATE ROLE anon NOLOGIN; EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE ROLE authenticated NOLOGIN; EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE ROLE service_role NOLOGIN; EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE SCHEMA IF NOT EXISTS auth;
CREATE TABLE IF NOT EXISTS auth.users (id uuid PRIMARY KEY);

-- Mirror of conftest.py::_preseed_supabase_objects: migration 018 moved the
-- signup-bonus trigger from auth.users to better-auth's public."user" table.
-- The migration's "IF to_regclass('public.user') IS NOT NULL" branch only
-- installs the on_signup_credit_bonus constraint trigger WHEN this table
-- exists at bootstrap time. Without it the trigger creation path silently
-- no-ops in JS, so the two SDKs would test different DB topologies.
CREATE TABLE IF NOT EXISTS public."user" (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Bursar's billing tables share the host application's standard timestamp
-- trigger. The production Supabase baseline supplies it; bare Postgres tests
-- provide the same minimal contract before applying the Bursar baseline.
CREATE OR REPLACE FUNCTION public.handle_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION auth.role() RETURNS text
LANGUAGE SQL STABLE AS $$
  SELECT coalesce(nullif(current_setting('request.jwt.claim.role', true), ''), 'service_role')
$$;

CREATE OR REPLACE FUNCTION auth.uid() RETURNS uuid
LANGUAGE SQL STABLE AS $$
  SELECT coalesce(
    nullif(current_setting('request.jwt.claim.sub', true), ''),
    current_setting('request.jwt.claims', true)::jsonb ->> 'sub'
  )::uuid
$$;

ALTER ROLE service_role BYPASSRLS;
GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role;
GRANT USAGE ON SCHEMA auth TO anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT EXECUTE ON FUNCTIONS TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO anon, authenticated;
GRANT ALL ON ALL TABLES IN SCHEMA public TO service_role;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO service_role;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO service_role;
`;

export { BOOTSTRAP_SQL };

export function migrationFiles(): string[] {
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
}

/** Truncate all bursar credit + billing tables (dynamic TRUNCATE CASCADE loop). */
export async function truncateBursarTables(pool: pg.Pool): Promise<void> {
  await pool.query(`
    DO $$
    DECLARE t text;
    BEGIN
      FOR t IN
        SELECT tablename FROM pg_tables
        WHERE schemaname = 'bursar'
          AND (tablename LIKE 'credit_%' OR tablename = 'user_credits'
               OR tablename LIKE 'billing_%')
      LOOP
        EXECUTE format('TRUNCATE TABLE bursar.%I CASCADE', t);
      END LOOP;
    EXCEPTION WHEN undefined_table THEN NULL;
    END $$;
  `);
}
