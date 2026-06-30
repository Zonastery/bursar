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
    await pool.query(`INSERT INTO auth.users (id) VALUES ($1), ($2) ON CONFLICT DO NOTHING`, [
      PG_USER,
      PG_USER2,
    ]);
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
    await store.deductWithAllowance(PG_USER, D("2.5"), {
      idempotencyKey: "list-1",
      model: "gpt-4",
    });
    await store.deductWithAllowance(PG_USER, D("3.5"), {
      idempotencyKey: "list-2",
      model: "claude-3",
    });
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
    await store.deductWithAllowance(PG_USER2, D("3.5"), {
      idempotencyKey: "sbu-2",
      model: "gpt-4",
    });

    const now = new Date();
    const rows = await store.spendByUser(
      new Date(now.getTime() - 60000),
      new Date(now.getTime() + 60000),
    );
    const u1 = rows.find((r) => r.userId === PG_USER);
    const u2 = rows.find((r) => r.userId === PG_USER2);
    expect(u1!.totalSpend.toString()).toBe("2.5");
    expect(u2!.totalSpend.toString()).toBe("3.5");
  });

  // ── JI1: Analytics — spendByModel ──────────────────────────────────
  it("JI1 — spendByModel returns both models with exact Decimal totals", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    await store.addCredits(PG_USER, D(500), "purchase");
    await store.deductWithAllowance(PG_USER, D("1.5"), { idempotencyKey: "sbm-1", model: "gpt-4" });
    await store.deductWithAllowance(PG_USER, D("2.5"), {
      idempotencyKey: "sbm-2",
      model: "claude-3",
    });

    const now = new Date();
    const rows = await store.spendByModel(
      new Date(now.getTime() - 60000),
      new Date(now.getTime() + 60000),
    );
    const gpt4 = rows.find((r) => r.model === "gpt-4");
    const claude3 = rows.find((r) => r.model === "claude-3");
    expect(gpt4).toBeDefined();
    expect(claude3).toBeDefined();
    expect(gpt4!.totalSpend.toString()).toBe("1.5");
    expect(claude3!.totalSpend.toString()).toBe("2.5");
  });

  // ── JI2: Analytics — topUsers ───────────────────────────────────────
  it("JI2 — topUsers returns limit=2 ordered by descending spend", async () => {
    // Need 3 users — seed PG_USER3 into auth.users first
    const PG_USER3 = "00000000-0000-0000-0000-000000000003";
    await pool.query(`INSERT INTO auth.users (id) VALUES ($1) ON CONFLICT DO NOTHING`, [PG_USER3]);
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    await store.addCredits(PG_USER, D(500), "purchase");
    await store.addCredits(PG_USER2, D(500), "purchase");
    await store.addCredits(PG_USER3, D(500), "purchase");

    // PG_USER spends 30, PG_USER2 spends 10, PG_USER3 spends 20
    await store.deductWithAllowance(PG_USER, D("30"), { idempotencyKey: "tu-1" });
    await store.deductWithAllowance(PG_USER2, D("10"), { idempotencyKey: "tu-2" });
    await store.deductWithAllowance(PG_USER3, D("20"), { idempotencyKey: "tu-3" });

    const now = new Date();
    const rows = await store.topUsers(
      2,
      new Date(now.getTime() - 60000),
      new Date(now.getTime() + 60000),
    );
    expect(rows).toHaveLength(2);
    // First row must be the biggest spender
    expect(rows[0].totalSpend.gte(rows[1].totalSpend)).toBe(true);
    expect(rows[0].userId).toBe(PG_USER);
    expect(rows[1].userId).toBe(PG_USER3);
  });

  // ── JI3: Analytics — dailySpend ────────────────────────────────────
  it("JI3 — dailySpend returns at least one entry with non-zero spend", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    await store.addCredits(PG_USER, D(100), "purchase");
    await store.deductWithAllowance(PG_USER, D("5"), { idempotencyKey: "ds-1" });

    const now = new Date();
    const rows = await store.dailySpend(
      new Date(now.getTime() - 60000),
      new Date(now.getTime() + 60000),
    );
    expect(rows.length).toBeGreaterThan(0);
    const nonZero = rows.filter((r) => r.totalSpend.gt(0));
    expect(nonZero.length).toBeGreaterThan(0);
    // date key should be a non-empty string
    expect(nonZero[0].date.length).toBeGreaterThan(0);
  });

  // ── JI4: Analytics — aggregateStats ────────────────────────────────
  it("JI4 — aggregateStats returns non-zero totalCreditsConsumed and activeUsers", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    await store.addCredits(PG_USER, D(100), "purchase");
    await store.deductWithAllowance(PG_USER, D("7"), { idempotencyKey: "as-1" });

    const now = new Date();
    const stats = await store.aggregateStats(
      new Date(now.getTime() - 60000),
      new Date(now.getTime() + 60000),
    );
    expect(stats.totalCreditsConsumed.gt(0)).toBe(true);
    expect(stats.activeUsers).toBeGreaterThan(0);
  });

  // ── JI5: Analytics — listUsageEvents ───────────────────────────────
  it("JI5 — listUsageEvents returns events for the correct userId and amount", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    await store.addCredits(PG_USER, D(100), "purchase");
    await store.deductWithAllowance(PG_USER, D("3.5"), { idempotencyKey: "lue-1" });

    const now = new Date();
    const result = await store.listUsageEvents(PG_USER, {
      fromDate: new Date(now.getTime() - 60000),
      toDate: new Date(now.getTime() + 60000),
    });
    expect(result.items.length).toBeGreaterThan(0);
    const evt = result.items[0];
    expect(evt.userId).toBe(PG_USER);
    // usage transactions are stored with a negative amount
    expect(evt.amount.abs().toString()).toBe("3.5");
  });

  // ── JI6: Cap deny does NOT consume allowance ────────────────────────
  it("JI6 — cap deny does not consume allowance window usage", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    const PLAN_JI6 = "00000000-0000-0000-0000-0000000000b1";
    // Plan allowance of 5: cost=20, so v_consume=5, v_net=15.
    // Cap limit of 10: 0 (prior spend) + 15 (net) > 10 → cap fires.
    // The SQL BEGIN block increments the usage window by 5, then the RAISE rolls
    // it back — so allowanceRemaining stays at 5 (unchanged from the initial 5).
    await pool.query(
      `INSERT INTO public.credit_plans (id, name, free_allowance, plan_key) VALUES ($1, 'PlanJI6', 5, $2)`,
      [PLAN_JI6, PLAN_JI6],
    );
    await store.addCredits(PG_USER, D(1000), "purchase");
    await store.setUserPlan(PG_USER, PLAN_JI6);

    await pool.query(
      `INSERT INTO public.credit_spend_caps (user_id, cap_type, cap_limit, action) VALUES ($1, 'daily', 10, 'deny')`,
      [PG_USER],
    );

    const r = await store.deductWithAllowance(PG_USER, D("20"), { idempotencyKey: "ji6-1" });
    expect(r.error).toBe("cap_reached");

    // Allowance window should NOT have been touched (rolled back on RAISE)
    const plan = await store.checkAllowance(PG_USER);
    expect(plan.allowanceRemaining.toString()).toBe("5");
  });

  // ── JI7: Refund does NOT restore allowance ──────────────────────────
  it("JI7 — refund does not restore plan allowance", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    const PLAN_JI7 = "00000000-0000-0000-0000-0000000000b2";
    // Plan allowance of 5 — cost of 20 will consume the 5 from allowance then
    // take 15 from balance. This ensures the transaction has a real balance debit
    // (amount > 0) so the refund succeeds, while allowanceConsumed > 0.
    await pool.query(
      `INSERT INTO public.credit_plans (id, name, free_allowance, plan_key) VALUES ($1, 'PlanJI7', 5, $2)`,
      [PLAN_JI7, PLAN_JI7],
    );
    await store.addCredits(PG_USER, D(500), "purchase");
    await store.setUserPlan(PG_USER, PLAN_JI7);

    // Check allowance before deduction
    const before = await store.checkAllowance(PG_USER);

    // Deduct 20: allowance(5) covers first 5, balance pays the remaining 15
    const deduct = await store.deductWithAllowance(PG_USER, D("20"), { idempotencyKey: "ji7-1" });
    expect(deduct.error).toBeUndefined();
    expect(deduct.allowanceConsumed.gt(0)).toBe(true);
    expect(deduct.amount.gt(0)).toBe(true); // some balance was actually deducted

    // Note allowance state after deduction
    const afterDeduct = await store.checkAllowance(PG_USER);

    // Refund the balance portion
    const refund = await store.refundCredits(deduct.transactionId);
    expect(refund.error).toBeUndefined();

    // Allowance remaining should NOT be restored — should stay the same as afterDeduct
    const afterRefund = await store.checkAllowance(PG_USER);
    expect(afterRefund.allowanceRemaining.toString()).toBe(
      afterDeduct.allowanceRemaining.toString(),
    );
    // And it should be less than the original before value
    expect(afterRefund.allowanceRemaining.lt(before.allowanceRemaining)).toBe(true);
  });

  // ── JI8: Sweep when balance < total expired ─────────────────────────
  it("JI8 — sweep with partially-used expired credits leaves non-negative balance", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    // 100 credits already expired
    await store.addCredits(PG_USER, D(100), "purchase", null, new Date(Date.now() - 1000));
    // 50 credits with no expiry
    await store.addCredits(PG_USER, D(50), "purchase");
    // Deduct 80 (comes from expired pool first)
    await store.deductWithAllowance(PG_USER, D(80), { idempotencyKey: "ji8-1" });

    await store.sweepExpiredCredits();

    const bal = (await store.getBalance(PG_USER)).balance;
    expect(bal.gte(0)).toBe(true);
    expect(bal.lte(50)).toBe(true);
  });

  // ── JI9: listUserTransactions — type filter ─────────────────────────
  it("JI9 — listUserTransactions type filter isolates usage vs purchase", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    await store.addCredits(PG_USER, D(100), "purchase");
    await store.deductWithAllowance(PG_USER, D("5"), { idempotencyKey: "ji9-usage" });

    const usageOnly = await store.listUserTransactions(PG_USER, { types: ["usage"] });
    expect(usageOnly.items.every((t) => t.type === "usage")).toBe(true);
    expect(usageOnly.items.length).toBeGreaterThan(0);

    const purchaseOnly = await store.listUserTransactions(PG_USER, { types: ["purchase"] });
    expect(purchaseOnly.items.every((t) => t.type === "purchase")).toBe(true);
    expect(purchaseOnly.items.length).toBeGreaterThan(0);
  });

  // ── JI10: aggregateStats Decimal precision ─────────────────────────
  it("JI10 — aggregateStats totalCreditsConsumed exact Decimal precision", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    await store.addCredits(PG_USER, D(100), "purchase");
    await store.deductWithAllowance(PG_USER, D("0.1000"), { idempotencyKey: "ji10-a" });
    await store.deductWithAllowance(PG_USER, D("0.2000"), { idempotencyKey: "ji10-b" });
    await store.deductWithAllowance(PG_USER, D("0.1500"), { idempotencyKey: "ji10-c" });

    const now = new Date();
    const stats = await store.aggregateStats(
      new Date(now.getTime() - 60000),
      new Date(now.getTime() + 60000),
    );
    expect(stats.totalCreditsConsumed.equals(D("0.4500"))).toBe(true);
  });

  // ── H4: RPC atomicity — cap-deny must NOT consume allowance ─────────
  it("H4 — deductWithAllowance cap deny does not consume allowance", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    const PLAN_H4 = "00000000-0000-0000-0000-0000000000c1";
    // Plan with monthly allowance of 10.
    await pool.query(
      `INSERT INTO public.credit_plans (id, name, free_allowance, plan_key) VALUES ($1, 'PlanH4', 10, $2)`,
      [PLAN_H4, PLAN_H4],
    );
    await store.addCredits(PG_USER, D(50), "purchase");
    await store.setUserPlan(PG_USER, PLAN_H4);

    // Set a deny spend cap at 8. Attempt deduction of 20: allowance covers 10,
    // net = 10. Cap check: 0 + 10 > 8 → deny fires before any allowance is consumed.
    await pool.query(
      `INSERT INTO public.credit_spend_caps (user_id, cap_type, cap_limit, action) VALUES ($1, 'daily', 8, 'deny')`,
      [PG_USER],
    );

    const r = await store.deductWithAllowance(PG_USER, D("20"), { idempotencyKey: "h4-deny" });
    expect(r.error).toBe("cap_reached");

    // Allowance window must be 0 — no allowance leaked on the failed attempt.
    const allowance = await store.checkAllowance(PG_USER);
    expect(allowance.allowanceRemaining.toString()).toBe("10");

    // Confirm a normal deduction of 5 still works (5 net <= 8 cap limit).
    const ok = await store.deductWithAllowance(PG_USER, D("5"), { idempotencyKey: "h4-ok" });
    expect(ok.error).toBeUndefined();
  });

  // ── H6: Decimal round-trip precision ────────────────────────────────
  it("H6 — decimal amounts survive Postgres round-trip with 4dp precision", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);

    await store.addCredits(PG_USER, D("0.0001"), "purchase");
    let bal = await store.getBalance(PG_USER);
    expect(bal.balance.toFixed(4)).toBe("0.0001");

    await store.addCredits(PG_USER, D("0.1234"), "purchase");
    bal = await store.getBalance(PG_USER);
    expect(bal.balance.toFixed(4)).toBe("0.1235");

    const deduct = await store.deductWithAllowance(PG_USER, D("0.0001"), {
      idempotencyKey: "h6-deduct",
    });
    expect(deduct.error).toBeUndefined();
    bal = await store.getBalance(PG_USER);
    expect(bal.balance.toFixed(4)).toBe("0.1234");
  });

  // ── H7: Migration idempotency ────────────────────────────────────────
  it("H7 — running setup() twice on same database does not error", async () => {
    // PostgresStore.setup() intentionally throws (H17) — it does not run
    // migrations itself. The underlying SQL migrations must be idempotent.
    // Apply them twice and confirm no error, then verify basic store operations
    // still work on the resulting schema.
    await expect(applyMigrations(pool)).resolves.toBeUndefined();
    await expect(applyMigrations(pool)).resolves.toBeUndefined();

    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    await store.addCredits(PG_USER, D(10), "purchase");
    const bal = await store.getBalance(PG_USER);
    expect(bal.balance.gte(D(10))).toBe(true);
  });

  // ── H10: MemoryStore vs PostgresStore parity ────────────────────────
  it("H10 — MemoryStore and PostgresStore produce identical results for same operations", async () => {
    const USER_H10 = "00000000-0000-0000-0000-000000000010";
    // Full database cleanup before test to guarantee no leftover state from prior
    // tests (belt-and-suspenders — afterEach should already handle this, but CI
    // parallelism across workers can create races on shared Postgres access).
    await pool.query("DELETE FROM public.credit_reservations");
    await pool.query("DELETE FROM public.credit_team_members");
    await pool.query("DELETE FROM public.credit_teams");
    await pool.query("DELETE FROM public.credit_usage_window");
    await pool.query("DELETE FROM public.credit_transactions");
    await pool.query("DELETE FROM public.credit_spend_caps");
    await pool.query("UPDATE public.user_credits SET plan_id = NULL");
    await pool.query("DELETE FROM public.user_credits");
    await pool.query("DELETE FROM public.credit_plans");
    await pool.query(`INSERT INTO auth.users (id) VALUES ($1) ON CONFLICT DO NOTHING`, [USER_H10]);
    // Signup bonus trigger on auth.users INSERT grants 50 free credits. Clear it
    // so the test starts from a known zero balance. Delete transactions first
    // (FK credit_transactions.user_id -> user_credits.user_id).
    await pool.query(`DELETE FROM public.credit_transactions WHERE user_id = $1`, [USER_H10]);
    await pool.query(`DELETE FROM public.user_credits WHERE user_id = $1`, [USER_H10]);

    const pgStore = new PostgresStore(DATABASE_URL!, pg.Pool);
    const memStore = new MemoryStore();

    // Run the same sequence on both stores and capture the idempotent-replay result.
    const run = async (store: PostgresStore | MemoryStore) => {
      await store.addCredits(USER_H10, D("10.0000"), "purchase");
      await store.deductWithAllowance(USER_H10, D("3.0000"), { idempotencyKey: "h10-a" });
      await store.deductWithAllowance(USER_H10, D("3.0000"), { idempotencyKey: "h10-b" });
      // Idempotent replay — same key as the previous call.
      const replay = await store.deductWithAllowance(USER_H10, D("3.0000"), {
        idempotencyKey: "h10-b",
      });
      return replay;
    };

    const pgReplay = await run(pgStore);
    const memReplay = await run(memStore);

    // Both replays must be flagged as idempotent.
    expect(pgReplay.idempotent).toBe(true);
    expect(memReplay.idempotent).toBe(true);

    // Both balances must be 4.0000 (10 - 3 - 3; idempotent replay does not charge again).
    const pgBal = await pgStore.getBalance(USER_H10);
    const memBal = await memStore.getBalance(USER_H10);
    expect(pgBal.balance.toFixed(4)).toBe("4.0000");
    expect(memBal.balance.toFixed(4)).toBe("4.0000");
    expect(pgBal.balance.toFixed(4)).toBe(memBal.balance.toFixed(4));
  });

  // ── H12: TOCTOU — concurrent cap check + deduct ─────────────────────
  it("H12 — concurrent deductions cannot bypass spend cap", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    await store.addCredits(PG_USER, D(50), "purchase");

    // Daily deny cap of 10.
    await pool.query(
      `INSERT INTO public.credit_spend_caps (user_id, cap_type, cap_limit, action) VALUES ($1, 'daily', 10, 'deny')`,
      [PG_USER],
    );

    // 10 concurrent deductions of 2 each (total possible: 10 × 2 = 20, cap = 10).
    const results = await Promise.all(
      Array.from({ length: 10 }, (_, i) =>
        store.deductWithAllowance(PG_USER, D("2.0000"), { idempotencyKey: `h12-${i}` }),
      ),
    );

    const succeeded = results.filter((r) => !r.error);
    const capReached = results.filter((r) => r.error === "cap_reached");

    // Exactly 5 succeed (5 × 2 = 10 = cap limit), the remaining 5 hit cap_reached.
    expect(succeeded.length).toBe(5);
    expect(capReached.length).toBe(5);

    const finalBal = (await store.getBalance(PG_USER)).balance;
    expect(finalBal.toString()).toBe("40");
  }, 30000);

  // ── M10: Concurrent team deductions ─────────────────────────────────
  it("M10 — concurrent team deductions from different users do not over-spend", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);

    // Seed 20 distinct users.
    const teamUsers: string[] = [];
    for (let i = 0; i < 20; i++) {
      const uid = `00000000-0000-0000-0000-0000000001${String(i).padStart(2, "0")}`;
      teamUsers.push(uid);
    }
    await pool.query(
      `INSERT INTO auth.users (id) SELECT unnest($1::uuid[]) ON CONFLICT DO NOTHING`,
      [teamUsers],
    );
    for (const uid of teamUsers) {
      await store.addCredits(uid, D(1), "adjustment");
    }

    const team = await store.createTeam("ConcurrentPool", D(20));
    for (const uid of teamUsers) {
      await store.addTeamMember(team.teamId, uid, "member");
    }

    // 20 concurrent deductions of 3 each against a pool of 20.
    const results = await Promise.all(
      teamUsers.map((uid) => store.deductTeam(team.teamId, uid, D("3.0000"))),
    );

    const succeeded = results.filter((r) => !r.error);
    const failed = results.filter((r) => !!r.error);

    // floor(20 / 3) = 6 succeed; remaining 14 fail with insufficient balance.
    expect(succeeded.length).toBe(6);
    expect(failed.length).toBe(14);

    const teamBal = (await store.getTeamBalance(team.teamId)).balance;
    expect(teamBal.gte(0)).toBe(true);
  }, 30000);
});
