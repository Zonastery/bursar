/**
 * RLS / privilege-lockdown tests against a real Postgres (P0.2).
 *
 * Mirror of `python/tests/test_security_rls.py` — see that file's module
 * docstring for the full rationale. In short: `store-integration.test.ts`
 * connects as the DSN's admin user (a superuser) via `pg.Pool`, which bypasses
 * every `REVOKE`/RLS check Postgres has, so bursar's actual privilege
 * lockdown has never been exercised by any JS test either. This file
 * connects as the literal Postgres roles `anon` / `authenticated` /
 * `service_role` (`SET LOCAL ROLE`, mirroring what PostgREST does per
 * request) with `request.jwt.claim.*` GUCs set the way a real JWT would.
 */
import { describe, it, expect, beforeAll, afterAll, inject } from "vitest";
import { randomUUID } from "crypto";
import { readdirSync, readFileSync } from "fs";
import { join, dirname } from "path";
import { fileURLToPath } from "url";
import pg from "pg";

const __dirname = dirname(fileURLToPath(import.meta.url));
const SQL_DIR = join(__dirname, "../../python/src/bursar/sql");
const DATABASE_URL = process.env.DATABASE_URL ?? inject("DATABASE_URL");

const BOOTSTRAP_SQL = `
DO $$ BEGIN CREATE ROLE anon NOLOGIN; EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE ROLE authenticated NOLOGIN; EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE ROLE service_role NOLOGIN; EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE SCHEMA IF NOT EXISTS auth;
CREATE TABLE IF NOT EXISTS auth.users (id uuid PRIMARY KEY);

CREATE OR REPLACE FUNCTION auth.role() RETURNS text
LANGUAGE SQL STABLE AS $func$
  SELECT coalesce(nullif(current_setting('request.jwt.claim.role', true), ''), 'service_role')
$func$;

CREATE OR REPLACE FUNCTION auth.uid() RETURNS uuid
LANGUAGE SQL STABLE AS $func$
  SELECT nullif(current_setting('request.jwt.claim.sub', true), '')::uuid
$func$;

-- Platform-level privilege defaults a real hosted Supabase project grants
-- automatically (via its own bootstrap migrations, not bursar's). Without
-- reproducing them here, \`SET ROLE service_role\` would fail on every RPC —
-- not because bursar's lockdown is broken, but because this bare Postgres
-- never gave service_role the platform privileges it has in production.
ALTER ROLE service_role BYPASSRLS;
GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role;
-- Real Supabase also grants schema access on \`auth\` to these roles (app/RPC
-- code calls auth.uid()/auth.jwt() directly, not just from within RLS policy
-- predicates — those are resolved at policy-definition time and don't need
-- this, but a direct SELECT auth.uid() from application code does).
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

function migrationFiles(): string[] {
  return readdirSync(SQL_DIR)
    .filter((f) => f.endsWith(".sql"))
    .sort();
}

async function applyMigrations(pool: pg.Pool): Promise<void> {
  for (const file of migrationFiles()) {
    await pool.query(readFileSync(join(SQL_DIR, file), "utf8"));
  }
}

/**
 * Run `sql` in a fresh transaction impersonating `role`, mirroring what
 * PostgREST does per request: `SET LOCAL ROLE` to the literal Postgres role
 * for the caller's API key, plus `request.jwt.claim.*` GUCs so
 * `auth.uid()`/`auth.role()` resolve as they would for a real request.
 * Always rolled back — this helper is for permission-check assertions, not
 * to persist state under an impersonated role.
 */
async function runAs(
  pool: pg.Pool,
  role: string,
  sql: string,
  params: unknown[] = [],
  opts: { jwtRole?: string; jwtSub?: string } = {},
): Promise<pg.QueryResult["rows"]> {
  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    // `SET LOCAL ROLE` is a utility statement (no bind-parameter support over
    // the wire protocol), so `role` must be one of our own hardcoded literals
    // (never attacker/test-input controlled) rather than a query parameter.
    await client.query(`SET LOCAL ROLE ${role}`);
    // GUCs with dynamic values (a fresh UUID per test) go through
    // set_config(), a regular function call that *does* support bind
    // parameters — `SET LOCAL x = $1` is a syntax error over pg's protocol.
    if (opts.jwtRole !== undefined) {
      await client.query("SELECT set_config('request.jwt.claim.role', $1, true)", [opts.jwtRole]);
    }
    if (opts.jwtSub !== undefined) {
      await client.query("SELECT set_config('request.jwt.claim.sub', $1, true)", [opts.jwtSub]);
    }

    // Self-verify the impersonation actually took effect before trusting
    // anything the query below returns. A silently-failed role switch would
    // look EXACTLY like an RLS/REVOKE bug from the outside: the query would
    // run as the connecting superuser (which bypasses both privilege checks
    // and RLS) and return data a real anon/authenticated caller could never
    // see. This turns that ambiguity into a clear, actionable diagnostic
    // instead of a confusing "wrong row" failure.
    const check = await client.query<{ current_user: string; uid: string | null; role: string }>(
      "SELECT current_user, auth.uid()::text AS uid, auth.role() AS role",
    );
    const { current_user: actualUser, uid: actualUid, role: actualRole } = check.rows[0];
    if (actualUser !== role) {
      throw new Error(
        `SET LOCAL ROLE did not take effect: current_user=${actualUser}, expected ${role}`,
      );
    }
    const expectedUid = opts.jwtSub ?? null;
    if (actualUid !== expectedUid) {
      throw new Error(`auth.uid() mismatch: got ${actualUid}, expected jwtSub=${expectedUid}`);
    }
    if (opts.jwtRole !== undefined && actualRole !== opts.jwtRole) {
      throw new Error(`auth.role() mismatch: got ${actualRole}, expected ${opts.jwtRole}`);
    }

    const result = await client.query(sql, params);
    return result.rows;
  } finally {
    await client.query("ROLLBACK");
    client.release();
  }
}

/** Grant credits as service_role and COMMIT (runAs always rolls back). */
async function grantCredits(pool: pg.Pool, userId: string, amount: number): Promise<void> {
  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    await client.query("SET LOCAL ROLE service_role");
    await client.query("SET LOCAL request.jwt.claim.role = 'service_role'");
    await client.query("SELECT public.credits_add($1, $2, 'purchase', '{}'::jsonb, NULL)", [
      userId,
      amount,
    ]);
    await client.query("COMMIT");
  } finally {
    client.release();
  }
}

// One representative call per RPC family (mutating writes, team ops, admin
// config, analytics reads). Argument VALUES don't need to satisfy business
// rules: Postgres checks EXECUTE privilege when resolving the call, before
// the function body ever runs, so a plausible-shaped call is enough to prove
// the REVOKE holds (or doesn't).
function rpcCalls(): Record<string, { sql: string; params: unknown[] }> {
  return {
    credits_add: {
      sql: "SELECT public.credits_add($1, 10, 'purchase', '{}'::jsonb, NULL)",
      params: [randomUUID()],
    },
    deduct_with_allowance: {
      sql: "SELECT public.deduct_with_allowance($1, 10)",
      params: [randomUUID()],
    },
    refund_credits: {
      sql: "SELECT public.refund_credits($1)",
      params: [randomUUID()],
    },
    create_team: {
      sql: "SELECT public.create_team('t', 100)",
      params: [],
    },
    deduct_team: {
      sql: "SELECT public.deduct_team($1, $2, 10)",
      params: [randomUUID(), randomUUID()],
    },
    set_active_pricing_config: {
      sql: "SELECT public.set_active_pricing_config('{}'::jsonb)",
      params: [],
    },
    spend_by_model: {
      sql: "SELECT * FROM public.spend_by_model(now() - interval '1 day', now())",
      params: [],
    },
  };
}

describe.runIf(DATABASE_URL)("RLS / privilege lockdown (real Postgres 16)", () => {
  let pool: pg.Pool;

  beforeAll(async () => {
    pool = new pg.Pool({ connectionString: DATABASE_URL });
    await pool.query(BOOTSTRAP_SQL);
    await applyMigrations(pool);
  }, 60000);

  afterAll(async () => {
    if (pool) await pool.end();
  });

  describe("RPC privilege lockdown — anon/authenticated denied, service_role allowed", () => {
    for (const [name, call] of Object.entries(rpcCalls())) {
      for (const role of ["anon", "authenticated"]) {
        it(`${role} is denied EXECUTE on ${name}`, async () => {
          await expect(runAs(pool, role, call.sql, call.params, { jwtRole: role })).rejects.toThrow(
            /permission denied/i,
          );
        });
      }

      it(`service_role is allowed to call ${name}`, async () => {
        // Must not throw a permission-denied error. The call may still fail
        // for a business reason (e.g. refunding a nonexistent transaction) —
        // that's a different error, not a privilege denial. Also fail
        // explicitly (not just "didn't match permission denied") on an
        // undefined-function error: a typo'd/renamed RPC in rpcCalls()
        // would otherwise silently pass this test while giving zero real
        // signal about the lockdown.
        try {
          await runAs(pool, "service_role", call.sql, call.params, { jwtRole: "service_role" });
        } catch (err) {
          expect(String(err)).not.toMatch(/permission denied/i);
          expect(String(err)).not.toMatch(/does not exist/i);
        }
      });
    }
  });

  describe("RLS tenant isolation on the raw tables", () => {
    it("authenticated user cannot see another user's credits", async () => {
      const userA = randomUUID();
      const userB = randomUUID();
      await grantCredits(pool, userA, 10);
      await grantCredits(pool, userB, 20);

      const rowsOther = await runAs(
        pool,
        "authenticated",
        "SELECT user_id, balance FROM public.user_credits WHERE user_id = $1",
        [userB],
        { jwtRole: "authenticated", jwtSub: userA },
      );
      expect(rowsOther).toEqual([]);

      const rowsOwn = await runAs(
        pool,
        "authenticated",
        "SELECT user_id, balance FROM public.user_credits WHERE user_id = $1",
        [userA],
        { jwtRole: "authenticated", jwtSub: userA },
      );
      expect(rowsOwn).toHaveLength(1);
      expect(rowsOwn[0].user_id).toBe(userA);
    });

    it("authenticated user cannot see another user's transactions", async () => {
      const userA = randomUUID();
      const userB = randomUUID();
      await grantCredits(pool, userA, 10);
      await grantCredits(pool, userB, 20);

      const rowsOther = await runAs(
        pool,
        "authenticated",
        "SELECT user_id FROM public.credit_transactions WHERE user_id = $1",
        [userB],
        { jwtRole: "authenticated", jwtSub: userA },
      );
      expect(rowsOther).toEqual([]);
    });

    it("anon with no jwt sub sees no rows", async () => {
      // anon has table-level SELECT (platform default) but no `sub` claim,
      // so auth.uid() is NULL and the RLS predicate (auth.uid() = user_id)
      // can never match — anon sees nothing, regardless of how many rows
      // exist.
      await grantCredits(pool, randomUUID(), 10);

      const rows = await runAs(pool, "anon", "SELECT user_id FROM public.user_credits", [], {
        jwtRole: "anon",
      });
      expect(rows).toEqual([]);
    });
  });

  describe("Schema-drift guard", () => {
    it("every non-trigger public function is revoked from anon and authenticated", async () => {
      const { rows } = await pool.query(`
        SELECT p.proname,
               has_function_privilege('anon', p.oid, 'EXECUTE') AS anon_exec,
               has_function_privilege('authenticated', p.oid, 'EXECUTE') AS auth_exec
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public'
          -- Trigger functions (e.g. grant_signup_bonus, handle_updated_at)
          -- are invoked internally by the executor on DML, never called
          -- directly by a role, so they're intentionally not part of the RPC
          -- surface REVOKE covers.
          AND p.prorettype != 'trigger'::regtype
        ORDER BY p.proname
      `);

      expect(rows.length).toBeGreaterThan(0);
      const leaks = rows.filter((r) => r.anon_exec || r.auth_exec).map((r) => r.proname);
      expect(
        leaks,
        `function(s) callable by anon/authenticated without an explicit REVOKE: ${leaks.join(", ")}. ` +
          "Every mutating or reading RPC must end with " +
          "`REVOKE EXECUTE ON FUNCTION public.<name>(...) FROM PUBLIC, anon, authenticated;` " +
          "qualified with its exact signature (see 002_credit_rpcs.sql for the pattern).",
      ).toEqual([]);
    });
  });
});
