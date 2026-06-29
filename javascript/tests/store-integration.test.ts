import { describe, it, expect, beforeAll, afterAll, afterEach } from "vitest";
import { readdirSync, readFileSync } from "fs";
import { join, dirname } from "path";
import { fileURLToPath } from "url";
import Decimal from "decimal.js";
import pg from "pg";
import { PostgresStore } from "../src/stores/postgres-store.js";
import { MemoryStore } from "../src/stores/memory-store.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const SQL_DIR = join(__dirname, "../../python/src/ducto/sql");
const DATABASE_URL = process.env.DATABASE_URL;

const D = (n: number | string) => new Decimal(n);

const PG_USER = "00000000-0000-0000-0000-000000000001";
const PG_USER2 = "00000000-0000-0000-0000-000000000099";
const PLAN_UUID = "00000000-0000-0000-0000-0000000000a1";

// ───────────────────────────────────────────────────────────────────────────
// MemoryStore concurrency — always runs (no DB required). Asserts the C2 fix
// holds under a real Promise.all: no double-spend, balance never negative.
// ───────────────────────────────────────────────────────────────────────────
describe("MemoryStore concurrency (double-spend guard, C2)", () => {
  it("N concurrent deductWithAllowance never over-spends", async () => {
    const store = new MemoryStore();
    await store.addCredits(PG_USER, D(5));

    const results = await Promise.all(
      Array.from({ length: 20 }, () => store.deductWithAllowance(PG_USER, D(1))),
    );
    const succeeded = results.filter((r) => !r.error);
    expect(succeeded).toHaveLength(5);

    const balance = (await store.getBalance(PG_USER)).balance;
    expect(balance.gte(0)).toBe(true);
    expect(balance.toString()).toBe("0");

    const totalDebited = succeeded.reduce((sum, r) => sum.plus(r.amount), D(0));
    expect(totalDebited.lte(5)).toBe(true);
  });

  it("idempotency replay under concurrency → exactly one debit", async () => {
    const store = new MemoryStore();
    await store.addCredits(PG_USER, D(100));

    const results = await Promise.all(
      Array.from({ length: 16 }, () =>
        store.deductWithAllowance(PG_USER, D(10), { idempotencyKey: "shared" }),
      ),
    );
    const realDebits = results.filter((r) => !r.idempotent && !r.error);
    expect(realDebits).toHaveLength(1);
    expect((await store.getBalance(PG_USER)).balance.toString()).toBe("90");
  });
});

// ───────────────────────────────────────────────────────────────────────────
// Real Postgres integration. Runs only when DATABASE_URL is present, but when
// it IS present it RUNS (not skips). When absent we log a visible skip notice.
// Run a local pg16: `docker run -d -e POSTGRES_PASSWORD=ducto -e POSTGRES_DB=ducto
//   -p 55432:5432 postgres:16` then
//   DATABASE_URL=postgresql://postgres:ducto@localhost:55432/ducto npx vitest run
// ───────────────────────────────────────────────────────────────────────────
if (!DATABASE_URL) {
  console.warn(
    "[store-integration] SKIPPING PostgresStore integration tests: DATABASE_URL is not set. " +
      "Start postgres:16 on a non-default port and export DATABASE_URL to run them.",
  );
}

const BOOTSTRAP_SQL = `
-- Roles are cluster-global, so creating them must be idempotent: the suite may
-- run twice against the same cluster, or share a cluster with the Python suite.
DO $$ BEGIN CREATE ROLE anon NOLOGIN; EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE ROLE authenticated NOLOGIN; EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE SCHEMA IF NOT EXISTS auth;
CREATE TABLE IF NOT EXISTS auth.users (id uuid PRIMARY KEY);

CREATE OR REPLACE FUNCTION auth.role() RETURNS text
LANGUAGE SQL IMMUTABLE AS $func$ SELECT 'service_role'::text $func$;

CREATE OR REPLACE FUNCTION auth.uid() RETURNS uuid
LANGUAGE SQL IMMUTABLE AS $func$ SELECT '00000000-0000-0000-0000-000000000000'::uuid $func$;
`;

function migrationFiles(): string[] {
  return readdirSync(SQL_DIR)
    .filter((f) => f.endsWith(".sql"))
    .sort();
}

async function applyMigrations(pool: pg.Pool): Promise<void> {
  for (const file of migrationFiles()) {
    const sql = readFileSync(join(SQL_DIR, file), "utf8");
    await pool.query(sql);
  }
}

describe.runIf(DATABASE_URL)("PostgresStore integration (real Postgres 16)", () => {
  let pool: pg.Pool;

  beforeAll(async () => {
    pool = new pg.Pool({ connectionString: DATABASE_URL });
    await pool.query(BOOTSTRAP_SQL);
    await applyMigrations(pool);
    // credit_team_members.user_id FKs into auth.users — seed the test users.
    await pool.query(
      `INSERT INTO auth.users (id) VALUES ($1), ($2) ON CONFLICT DO NOTHING`,
      [PG_USER, PG_USER2],
    );
  }, 60000);

  afterEach(async () => {
    if (pool) {
      await pool.query("DELETE FROM public.credit_reservations");
      await pool.query("DELETE FROM public.credit_team_members");
      await pool.query("DELETE FROM public.credit_teams");
      await pool.query("DELETE FROM public.credit_usage_window");
      await pool.query("DELETE FROM public.credit_transactions");
      await pool.query("DELETE FROM public.credit_spend_caps");
      await pool.query("UPDATE public.user_credits SET plan_id = NULL");
      await pool.query("DELETE FROM public.user_credits");
      await pool.query("DELETE FROM public.credit_plans");
    }
  });

  afterAll(async () => {
    if (pool) await pool.end();
  });

  // ── Migration idempotency ───────────────────────────────────────────
  it("migrations are idempotent (running twice succeeds)", async () => {
    // Re-applying all migrations (CREATE OR REPLACE / IF NOT EXISTS) must succeed.
    await expect(applyMigrations(pool)).resolves.toBeUndefined();
    await expect(applyMigrations(pool)).resolves.toBeUndefined();
  });

  it("PostgresStore.setup() refuses to fake success (H17)", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    await expect(store.setup()).rejects.toThrow(/migrat/i);
  });

  // ── deductWithAllowance basics ──────────────────────────────────────
  it("charges net amount and parses NUMERIC as exact Decimal", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    await store.addCredits(PG_USER, D(100), "purchase");

    const r = await store.deductWithAllowance(PG_USER, D("2.5"), { idempotencyKey: "ded-1" });
    expect(r.error).toBeUndefined();
    expect(r.amount.toString()).toBe("2.5");
    expect(r.balanceAfter.toString()).toBe("97.5");
    expect(r.idempotent).toBe(false);

    const balance = await store.getBalance(PG_USER);
    expect(balance.balance.toString()).toBe("97.5");
  });

  it("sub-credit charge is not truncated to zero (H1)", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    await store.addCredits(PG_USER, D(100), "purchase");
    const r = await store.deductWithAllowance(PG_USER, D("0.4"), { idempotencyKey: "sub-1" });
    expect(r.amount.toString()).toBe("0.4");
    expect((await store.getBalance(PG_USER)).balance.toString()).toBe("99.6");
  });

  it("insufficient credits returns error envelope (no throw)", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    await store.addCredits(PG_USER, D(1), "purchase");
    const r = await store.deductWithAllowance(PG_USER, D(50), { minBalance: D(0) });
    expect(r.error).toBe("insufficient_credits");
    expect((await store.getBalance(PG_USER)).balance.toString()).toBe("1");
  });

  // ── Idempotency replay ──────────────────────────────────────────────
  it("deductWithAllowance with same key replays original (one debit)", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    await store.addCredits(PG_USER, D(100), "purchase");

    const r1 = await store.deductWithAllowance(PG_USER, D(10), { idempotencyKey: "idem-x" });
    expect(r1.idempotent).toBe(false);
    const r2 = await store.deductWithAllowance(PG_USER, D(10), { idempotencyKey: "idem-x" });
    expect(r2.idempotent).toBe(true);
    expect((await store.getBalance(PG_USER)).balance.toString()).toBe("90");
  });

  it("different keys produce separate deductions", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    await store.addCredits(PG_USER, D(100), "purchase");
    await store.deductWithAllowance(PG_USER, D(10), { idempotencyKey: "a" });
    const r2 = await store.deductWithAllowance(PG_USER, D(10), { idempotencyKey: "b" });
    expect(r2.idempotent).toBe(false);
    expect((await store.getBalance(PG_USER)).balance.toString()).toBe("80");
  });

  // ── Concurrency / double-spend (THE acceptance-gating test) ─────────
  it("N concurrent deductWithAllowance never over-spends (C2)", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    // Balance covers only 5 of 20 one-credit charges, floor 0.
    await store.addCredits(PG_USER, D(5), "purchase");

    const results = await Promise.all(
      Array.from({ length: 20 }, (_, i) =>
        store.deductWithAllowance(PG_USER, D(1), {
          idempotencyKey: `conc-${i}`,
          minBalance: D(0),
        }),
      ),
    );

    const succeeded = results.filter((r) => !r.error);
    const failed = results.filter((r) => r.error === "insufficient_credits");
    expect(succeeded.length).toBe(5);
    expect(failed.length).toBe(15);

    const balance = (await store.getBalance(PG_USER)).balance;
    expect(balance.gte(0)).toBe(true);
    expect(balance.toString()).toBe("0");

    const totalDebited = succeeded.reduce((s, r) => s.plus(r.amount), D(0));
    expect(totalDebited.lte(5)).toBe(true);
  }, 30000);

  it("idempotency replay under concurrency → one debit (C2 + H16)", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    await store.addCredits(PG_USER, D(100), "purchase");

    const results = await Promise.all(
      Array.from({ length: 12 }, () =>
        store.deductWithAllowance(PG_USER, D(10), { idempotencyKey: "race-key" }),
      ),
    );
    const realDebits = results.filter((r) => !r.idempotent && !r.error);
    expect(realDebits.length).toBe(1);
    expect((await store.getBalance(PG_USER)).balance.toString()).toBe("90");
  }, 30000);

  // ── Allowance + cap semantics through the RPC ───────────────────────
  it("plan allowance fully covers cost, no balance debit; window incremented", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    await pool.query(
      `INSERT INTO public.credit_plans (id, name, free_allowance, plan_key) VALUES ($1, 'Free', 100, $2)`,
      [PLAN_UUID, PLAN_UUID],
    );
    await store.addCredits(PG_USER, D(10), "adjustment");
    await store.setUserPlan(PG_USER, PLAN_UUID);

    const r = await store.deductWithAllowance(PG_USER, D(5), { idempotencyKey: "plan-1" });
    expect(r.error).toBeUndefined();
    expect(r.amount.toString()).toBe("0");
    expect(r.allowanceConsumed.toString()).toBe("5");
    expect((await store.getBalance(PG_USER)).balance.toString()).toBe("10");
    expect((await store.checkAllowance(PG_USER)).allowanceRemaining.toString()).toBe("95");
  });

  it("plan allowance partial, remainder charged to balance", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    await pool.query(
      `INSERT INTO public.credit_plans (id, name, free_allowance, plan_key) VALUES ($1, 'Starter', 10, $2)`,
      [PLAN_UUID, PLAN_UUID],
    );
    await store.addCredits(PG_USER, D(100), "adjustment");
    await store.setUserPlan(PG_USER, PLAN_UUID);

    const r = await store.deductWithAllowance(PG_USER, D(25), { idempotencyKey: "plan-2" });
    expect(r.amount.toString()).toBe("15");
    expect(r.allowanceConsumed.toString()).toBe("10");
    expect((await store.getBalance(PG_USER)).balance.toString()).toBe("85");
  });

  it("deny spend cap aborts with cap_reached (allowance not consumed)", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    await store.addCredits(PG_USER, D(1000), "purchase");
    await pool.query(
      `INSERT INTO public.credit_spend_caps (user_id, cap_type, cap_limit, action) VALUES ($1, 'daily', 10, 'deny')`,
      [PG_USER],
    );
    const r = await store.deductWithAllowance(PG_USER, D(20), { idempotencyKey: "cap-1" });
    expect(r.error).toBe("cap_reached");
    expect((await store.getBalance(PG_USER)).balance.toString()).toBe("1000");
  });

  it("cap accumulates across prior window spend", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    await store.addCredits(PG_USER, D(1000), "purchase");
    await pool.query(
      `INSERT INTO public.credit_spend_caps (user_id, cap_type, cap_limit, action) VALUES ($1, 'daily', 30, 'deny')`,
      [PG_USER],
    );
    const a = await store.deductWithAllowance(PG_USER, D(20), { idempotencyKey: "acc-1" });
    expect(a.error).toBeUndefined();
    const b = await store.deductWithAllowance(PG_USER, D(20), { idempotencyKey: "acc-2" });
    expect(b.error).toBe("cap_reached");
  });

  // ── Reserve / deduct two-phase (C3) ─────────────────────────────────
  it("deductCredits clamps to the reserved ceiling (C3)", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    await store.addCredits(PG_USER, D(100), "purchase");

    const reserve = await store.reserveCredits(PG_USER, D(10), "usage", null, D(0));
    expect(reserve.error).toBeUndefined();
    const deduct = await store.deductCredits(PG_USER, reserve.reservationId, D(1000));
    expect(deduct.error).toBeUndefined();
    expect(deduct.amount.toString()).toBe("-10");
    expect((await store.getBalance(PG_USER)).balance.toString()).toBe("90");
  });

  it("reserve rejects (does not cap) below min_balance (C3 parity)", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    await store.addCredits(PG_USER, D(20), "purchase");
    const reserve = await store.reserveCredits(PG_USER, D(10), "usage", null, D(15));
    expect(reserve.error).toBe("insufficient_credits");
  });

  // ── Refunds ─────────────────────────────────────────────────────────
  it("full refund restores balance; over-refund and duplicate rejected", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    await store.addCredits(PG_USER, D(100), "purchase");
    const deduct = await store.deductWithAllowance(PG_USER, D(30), { idempotencyKey: "ref-1" });

    const over = await store.refundCredits(deduct.transactionId, D(1000));
    expect(over.error).toBe("over_refund");

    const refund = await store.refundCredits(deduct.transactionId);
    expect(refund.error).toBeUndefined();
    expect(refund.amount.toString()).toBe("30");
    expect((await store.getBalance(PG_USER)).balance.toString()).toBe("100");

    const dup = await store.refundCredits(deduct.transactionId);
    expect(dup.error).toBe("already_refunded");
  });

  it("cumulative partial refunds, then over-refund rejected", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    await store.addCredits(PG_USER, D(100), "purchase");
    const deduct = await store.deductWithAllowance(PG_USER, D(50), { idempotencyKey: "ref-2" });

    expect((await store.refundCredits(deduct.transactionId, D(20))).error).toBeUndefined();
    expect((await store.refundCredits(deduct.transactionId, D(20))).error).toBeUndefined();
    const third = await store.refundCredits(deduct.transactionId, D(20));
    expect(third.error).toBe("over_refund");
  });

  it("refund of a purchase (non-debit) is rejected", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    const add = await store.addCredits(PG_USER, D(100), "purchase");
    const refund = await store.refundCredits(add.transactionId);
    expect(refund.error).toBe("over_refund");
  });

  // ── Expiry double-sweep (H4) ────────────────────────────────────────
  it("expired credits sweep once; second sweep reports zero (H4)", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    await store.addCredits(PG_USER, D(100), "purchase", null, new Date(Date.now() - 1000));

    const first = await store.sweepExpiredCredits();
    expect(first.expiredAmount.toString()).toBe("100");
    expect((await store.getBalance(PG_USER)).balance.toString()).toBe("0");

    // Add fresh credits; a second sweep must NOT re-claw the already-swept grant.
    await store.addCredits(PG_USER, D(50), "purchase");
    const second = await store.sweepExpiredCredits();
    expect(second.expiredAmount.toString()).toBe("0");
    expect((await store.getBalance(PG_USER)).balance.toString()).toBe("50");
  });

  // ── Team pools + idempotency (H12) ──────────────────────────────────
  it("deductTeam idempotency key prevents double-charge (H12)", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    // credit_team_members.user_id FKs into user_credits — ensure the row exists.
    await store.addCredits(PG_USER, D(10), "adjustment");
    const team = await store.createTeam("Pool", D(500));
    await store.addTeamMember(team.teamId, PG_USER, "member");

    const r1 = await store.deductTeam(team.teamId, PG_USER, D(50), null, "team-key-1");
    expect(r1.error).toBeUndefined();
    const r2 = await store.deductTeam(team.teamId, PG_USER, D(50), null, "team-key-1");
    expect(r2.error).toBeUndefined();
    // Pool debited once: 500 - 50 = 450.
    expect((await store.getTeamBalance(team.teamId)).balance.toString()).toBe("450");
  });

  // ── Analytics list RPCs return all rows ─────────────────────────────
  it("listUserTransactions returns all rows with NUMERIC parsed as Decimal", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    await store.addCredits(PG_USER, D(1000), "purchase");
    await store.deductWithAllowance(PG_USER, D("2.5"), { idempotencyKey: "list-1", model: "gpt-4" });
    await store.deductWithAllowance(PG_USER, D("3.5"), { idempotencyKey: "list-2", model: "claude-3" });
    await store.addCredits(PG_USER2, D(10), "purchase");

    const result = await store.listUserTransactions(PG_USER);
    expect(result.total).toBe(3);
    expect(result.items).toHaveLength(3);
    const usage = result.items.filter((t) => t.type === "usage");
    expect(usage).toHaveLength(2);
    // Other user not included.
    const other = await store.listUserTransactions(PG_USER2, { types: ["usage"] });
    expect(other.total).toBe(0);
  });

  it("spendByUser returns all rows as exact Decimal", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    await store.addCredits(PG_USER, D(500), "purchase");
    await store.addCredits(PG_USER2, D(500), "purchase");
    await store.deductWithAllowance(PG_USER, D("2.5"), { idempotencyKey: "sbu-1", model: "gpt-4" });
    await store.deductWithAllowance(PG_USER2, D("3.5"), { idempotencyKey: "sbu-2", model: "gpt-4" });

    const now = new Date();
    const rows = await store.spendByUser(new Date(now.getTime() - 60000), new Date(now.getTime() + 60000));
    const u1 = rows.find((r) => r.userId === PG_USER);
    const u2 = rows.find((r) => r.userId === PG_USER2);
    expect(u1!.totalSpend.toString()).toBe("2.5");
    expect(u2!.totalSpend.toString()).toBe("3.5");
  });
});
