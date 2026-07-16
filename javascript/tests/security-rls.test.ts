/**
 * RLS / privilege-lockdown tests against a real Postgres (P0.2).
 *
 * Mirror of `python/tests/test_security_rls.py` — see that file's module
 * docstring for the full rationale. In short: `store-integration.test.ts`
 * connects as the DSN's admin user (a superuser) via `pg.Pool`, which bypasses
 * every `REVOKE`/RLS check Postgres has, so bursar's actual privilege
 * lockdown has never been exercised by any JS test either. This file
 * connects as the literal Postgres API roles (`SET LOCAL ROLE`, mirroring
 * what PostgREST does per request) with `request.jwt.claim.*` GUCs set the
 * way a real JWT would. Bursar is backend-only, so every API role is denied.
 */
import { describe, it, expect, beforeAll, afterAll, inject } from "vitest";
import { randomUUID } from "crypto";
import pg from "pg";
import { BOOTSTRAP_SQL, applyMigrations } from "./helpers/bootstrap.js";

const DATABASE_URL = process.env.DATABASE_URL ?? inject("DATABASE_URL");

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

/** Grant credits through the trusted direct database connection (runAs rolls back). */
async function grantCredits(pool: pg.Pool, userId: string, amount: number): Promise<void> {
  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    await client.query(`INSERT INTO public."user" (id) VALUES ($1) ON CONFLICT (id) DO NOTHING`, [
      userId,
    ]);
    await client.query("SELECT bursar.credits_add($1, $2, 'purchase', '{}'::jsonb, NULL)", [
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
      sql: "SELECT bursar.credits_add($1, 10, 'purchase', '{}'::jsonb, NULL)",
      params: [randomUUID()],
    },
    deduct_with_allowance: {
      sql: "SELECT bursar.deduct_with_allowance($1, 10)",
      params: [randomUUID()],
    },
    refund_credits: {
      sql: "SELECT bursar.refund_credits($1)",
      params: [randomUUID()],
    },
    create_team: {
      sql: "SELECT bursar.create_team('t', 100)",
      params: [],
    },
    deduct_team: {
      sql: "SELECT bursar.deduct_team($1, $2, 10)",
      params: [randomUUID(), randomUUID()],
    },
    set_active_bursar_config: {
      sql: "SELECT bursar.set_active_bursar_config('{}'::jsonb)",
      params: [],
    },
    spend_by_model: {
      sql: "SELECT * FROM bursar.spend_by_model(now() - interval '1 day', now())",
      params: [],
    },
  };
}

describe.runIf(DATABASE_URL)("RLS / privilege lockdown (real Postgres 16)", () => {
  let pool: pg.Pool;

  beforeAll(async () => {
    pool = new pg.Pool({ connectionString: DATABASE_URL, max: 3 });
    await pool.query(BOOTSTRAP_SQL);
    await applyMigrations(pool);
  }, 60000);

  afterAll(async () => {
    if (pool) await pool.end();
  });

  describe("RPC privilege lockdown — Bursar is inaccessible to every Data API role", () => {
    for (const [name, call] of Object.entries(rpcCalls())) {
      for (const role of ["anon", "authenticated", "service_role"]) {
        it(`${role} is denied EXECUTE on ${name}`, async () => {
          await expect(runAs(pool, role, call.sql, call.params, { jwtRole: role })).rejects.toThrow(
            /permission denied/i,
          );
        });
      }
    }
  });

  describe("raw Bursar tables are inaccessible to Data API roles", () => {
    it("authenticated cannot query a Bursar table, even for its own subject", async () => {
      const userId = randomUUID();
      await grantCredits(pool, userId, 10);
      await expect(
        runAs(
          pool,
          "authenticated",
          "SELECT user_id FROM bursar.user_credits WHERE user_id = $1",
          [userId],
          {
            jwtRole: "authenticated",
            jwtSub: userId,
          },
        ),
      ).rejects.toThrow(/permission denied/i);
    });
  });

  describe("Schema-drift guard", () => {
    it("every Bursar function is revoked from every Data API role", async () => {
      const { rows } = await pool.query(`
        SELECT p.proname,
               has_function_privilege('anon', p.oid, 'EXECUTE') AS anon_exec,
               has_function_privilege('authenticated', p.oid, 'EXECUTE') AS auth_exec
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'bursar'
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
        `Bursar function(s) callable by anon/authenticated without an explicit REVOKE: ${leaks.join(", ")}. ` +
          "Every mutating or reading RPC must end with " +
          "`REVOKE EXECUTE ON FUNCTION bursar.<name>(...) FROM PUBLIC, anon, authenticated;` " +
          "qualified with its exact signature (see 002_credit_rpcs.sql for the pattern).",
      ).toEqual([]);
    });
  });
});
