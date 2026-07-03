import { describe, it, expect, beforeAll, beforeEach, afterAll, afterEach } from "vitest";
import { readdirSync, readFileSync } from "fs";
import { join, dirname } from "path";
import { fileURLToPath } from "url";
import Decimal from "decimal.js";
import pg from "pg";
import { PostgresStore } from "../src/stores/postgres-store.js";
import { MemoryStore } from "../src/stores/memory-store.js";
import { CreditManager } from "../src/manager.js";
import { InsufficientCreditsError } from "../src/errors.js";
import { CreditEventEmitter } from "../src/stores/events.js";
import type { CreditEvent } from "../src/stores/events.js";
import { resolveAllowanceWindow } from "../src/allowance.js";
import type { AllowancePeriod } from "../src/allowance.js";
import type { PricingConfigData } from "../src/types.js";

const ALLOWANCE_PERIODS: AllowancePeriod[] = ["calendar_month", "rolling_30d", "anniversary"];

const __dirname = dirname(fileURLToPath(import.meta.url));
const SQL_DIR = join(__dirname, "../../python/src/ducto/sql");
const DATABASE_URL = process.env.DATABASE_URL;

const D = (n: number | string) => new Decimal(n);

const PG_USER = "00000000-0000-0000-0000-000000000001";
const PG_USER2 = "00000000-0000-0000-0000-000000000099";
const PLAN_UUID = "00000000-0000-0000-0000-0000000000a1";
const PG_USER3 = "00000000-0000-0000-0000-000000000003";
const PG_USER4 = "00000000-0000-0000-0000-000000000004";
const PG_USER5 = "00000000-0000-0000-0000-000000000005";
const PG_USER6 = "00000000-0000-0000-0000-000000000006";
const PG_USER7 = "00000000-0000-0000-0000-000000000007";
const PG_USER8 = "00000000-0000-0000-0000-000000000008";
const PG_USER9 = "00000000-0000-0000-0000-000000000009";
const PG_USER10 = "00000000-0000-0000-0000-000000000010";
const PG_USER11 = "00000000-0000-0000-0000-000000000011";
const PG_USER12 = "00000000-0000-0000-0000-000000000012";
const PG_USER13 = "00000000-0000-0000-0000-000000000013";
// ── New for lazyExpiry / grantSubscriptionCycle real-Postgres coverage ──
const PG_USER14 = "00000000-0000-0000-0000-000000000014";
const PG_USER15 = "00000000-0000-0000-0000-000000000015";
const PG_USER16 = "00000000-0000-0000-0000-000000000016";
const PG_USER17 = "00000000-0000-0000-0000-000000000017";

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
    // WS9 / WS10 / WS3 tests below use several more fixed user UUIDs — seed them
    // all up front (auth.users FK is required before any user_credits row can
    // reference them via team/plan features).
    await pool.query(
      `INSERT INTO auth.users (id) SELECT unnest($1::uuid[]) ON CONFLICT DO NOTHING`,
      [
        [
          PG_USER3,
          PG_USER4,
          PG_USER5,
          PG_USER6,
          PG_USER7,
          PG_USER8,
          PG_USER9,
          PG_USER10,
          PG_USER11,
          PG_USER12,
          PG_USER13,
          // lazyExpiry / addCredits-idempotency / scoped-sweep coverage below.
          PG_USER14,
          PG_USER15,
          PG_USER16,
          PG_USER17,
        ],
      ],
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
    const store = new PostgresStore(DATABASE_URL!, pool);
    await expect(store.setup()).rejects.toThrow(/migrat/i);
  });

  // ── deductWithAllowance basics ──────────────────────────────────────
  it("charges net amount and parses NUMERIC as exact Decimal", async () => {
    const store = new PostgresStore(DATABASE_URL!, pool);
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
    const store = new PostgresStore(DATABASE_URL!, pool);
    await store.addCredits(PG_USER, D(100), "purchase");
    const r = await store.deductWithAllowance(PG_USER, D("0.4"), { idempotencyKey: "sub-1" });
    expect(r.amount.toString()).toBe("0.4");
    expect((await store.getBalance(PG_USER)).balance.toString()).toBe("99.6");
  });

  it("insufficient credits returns error envelope (no throw)", async () => {
    const store = new PostgresStore(DATABASE_URL!, pool);
    await store.addCredits(PG_USER, D(1), "purchase");
    const r = await store.deductWithAllowance(PG_USER, D(50), { minBalance: D(0) });
    expect(r.error).toBe("insufficient_credits");
    expect((await store.getBalance(PG_USER)).balance.toString()).toBe("1");
  });

  // ── Idempotency replay ──────────────────────────────────────────────
  it("deductWithAllowance with same key replays original (one debit)", async () => {
    const store = new PostgresStore(DATABASE_URL!, pool);
    await store.addCredits(PG_USER, D(100), "purchase");

    const r1 = await store.deductWithAllowance(PG_USER, D(10), { idempotencyKey: "idem-x" });
    expect(r1.idempotent).toBe(false);
    const r2 = await store.deductWithAllowance(PG_USER, D(10), { idempotencyKey: "idem-x" });
    expect(r2.idempotent).toBe(true);
    expect((await store.getBalance(PG_USER)).balance.toString()).toBe("90");
  });

  it("different keys produce separate deductions", async () => {
    const store = new PostgresStore(DATABASE_URL!, pool);
    await store.addCredits(PG_USER, D(100), "purchase");
    await store.deductWithAllowance(PG_USER, D(10), { idempotencyKey: "a" });
    const r2 = await store.deductWithAllowance(PG_USER, D(10), { idempotencyKey: "b" });
    expect(r2.idempotent).toBe(false);
    expect((await store.getBalance(PG_USER)).balance.toString()).toBe("80");
  });

  // ── Concurrency / double-spend (THE acceptance-gating test) ─────────
  it("N concurrent deductWithAllowance never over-spends (C2)", async () => {
    const store = new PostgresStore(DATABASE_URL!, pool);
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
    const store = new PostgresStore(DATABASE_URL!, pool);
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
    const store = new PostgresStore(DATABASE_URL!, pool);
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
    const store = new PostgresStore(DATABASE_URL!, pool);
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
    const store = new PostgresStore(DATABASE_URL!, pool);
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
    const store = new PostgresStore(DATABASE_URL!, pool);
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

  // ── Feature limits (per-feature invocation-count limits) ─────────────
  //
  // NOTE: these tests exercise the `feature`/`featureMaxCalls`/`featureAction`/
  // `featurePeriodStart`/`featurePeriodEnd` trailing params on
  // `deduct_with_allowance`/`create_lease` (already present in
  // `009_deduct_and_leases.sql` at the time this was written) and the
  // `check_feature_limit` RPC (added by a new `..._feature_limits.sql`
  // migration, landing in parallel on the SQL track). If that migration/RPC
  // hasn't landed yet when this suite runs against a live Postgres, the
  // `checkFeatureLimit` test below will fail with an RPC-not-found error —
  // that is a landing-order issue for the SQL track, not a JS-side bug.
  describe("Feature limits (per-feature invocation-count limits)", () => {
    const monthStart = new Date(Date.UTC(new Date().getUTCFullYear(), new Date().getUTCMonth(), 1));
    const monthEnd = new Date(
      Date.UTC(new Date().getUTCFullYear(), new Date().getUTCMonth() + 1, 1),
    );

    it("deny action: under limit succeeds, at limit blocks with feature_limit_reached", async () => {
      const store = new PostgresStore(DATABASE_URL!, pool);
      await store.addCredits(PG_USER, D(100), "purchase");
      const featureLimit = { maxCalls: 2, period: "monthly" as const, action: "deny" as const };
      const opts = { feature: "export", featureLimit, featurePeriodStart: monthStart };

      const r1 = await store.deductWithAllowance(PG_USER, D(1), {
        ...opts,
        idempotencyKey: "fl-1",
      });
      expect(r1.error).toBeUndefined();
      const r2 = await store.deductWithAllowance(PG_USER, D(1), {
        ...opts,
        idempotencyKey: "fl-2",
      });
      expect(r2.error).toBeUndefined();
      // Third call: count (2) >= maxCalls (2) → deny, nothing further debited.
      const r3 = await store.deductWithAllowance(PG_USER, D(1), {
        ...opts,
        idempotencyKey: "fl-3",
      });
      expect(r3.error).toBe("feature_limit_reached");
      expect((await store.getBalance(PG_USER)).balance.toString()).toBe("98");
    });

    it("warn action: breach surfaces featureLimitWarning, never blocks", async () => {
      const store = new PostgresStore(DATABASE_URL!, pool);
      await store.addCredits(PG_USER, D(100), "purchase");
      const featureLimit = { maxCalls: 1, period: "monthly" as const, action: "warn" as const };
      const opts = { feature: "export", featureLimit, featurePeriodStart: monthStart };

      await store.deductWithAllowance(PG_USER, D(1), { ...opts, idempotencyKey: "fl-warn-1" });
      const r2 = await store.deductWithAllowance(PG_USER, D(1), {
        ...opts,
        idempotencyKey: "fl-warn-2",
      });
      expect(r2.error).toBeUndefined();
      expect(r2.featureLimitWarning).toBe("warn");
    });

    it("createLease: deny-only admission blocks once the limit is reached", async () => {
      const store = new PostgresStore(DATABASE_URL!, pool);
      await store.addCredits(PG_USER, D(100), "purchase");
      const featureLimit = { maxCalls: 1, period: "monthly" as const, action: "deny" as const };
      const opts = { feature: "export", featureLimit, featurePeriodStart: monthStart };
      await store.deductWithAllowance(PG_USER, D(1), { ...opts, idempotencyKey: "fl-lease-1" });
      const lease = await store.createLease(PG_USER, D(1), "usage", opts);
      expect(lease.error).toBe("feature_limit_reached");
    });

    it("release_lease and refund_credits do NOT restore quota", async () => {
      const store = new PostgresStore(DATABASE_URL!, pool);
      await store.addCredits(PG_USER, D(100), "purchase");
      const featureLimit = { maxCalls: 1, period: "monthly" as const, action: "deny" as const };
      const opts = { feature: "export", featureLimit, featurePeriodStart: monthStart };

      // Reserve then release near the limit — never counted, nothing to undo.
      const lease = await store.createLease(PG_USER, D(1), "usage");
      await store.releaseLease(PG_USER, lease.leaseId);
      const stillOk = await store.deductWithAllowance(PG_USER, D(1), {
        ...opts,
        idempotencyKey: "fl-release-1",
      });
      expect(stillOk.error).toBeUndefined();

      // Refund of the counted deduction does not free up quota.
      const refund = await store.refundCredits(stillOk.transactionId);
      expect(refund.error).toBeUndefined();
      const blocked = await store.deductWithAllowance(PG_USER, D(1), {
        ...opts,
        idempotencyKey: "fl-release-2",
      });
      expect(blocked.error).toBe("feature_limit_reached");
    });

    it("checkFeatureLimit: advisory count, no side effects", async () => {
      const store = new PostgresStore(DATABASE_URL!, pool);
      await store.addCredits(PG_USER, D(100), "purchase");
      await store.deductWithAllowance(PG_USER, D(1), {
        feature: "export",
        idempotencyKey: "fl-check-1",
      });
      await store.deductWithAllowance(PG_USER, D(1), {
        feature: "export",
        idempotencyKey: "fl-check-2",
      });

      const result = await store.checkFeatureLimit(PG_USER, "export", 5, monthStart, monthEnd);
      expect(result.used).toBe(2);
      expect(result.remaining).toBe(3);
    });

    it("concurrency: exactly N succeed under limit N", async () => {
      const store = new PostgresStore(DATABASE_URL!, pool);
      await store.addCredits(PG_USER, D(1000), "purchase");
      const featureLimit = { maxCalls: 5, period: "monthly" as const, action: "deny" as const };

      const results = await Promise.all(
        Array.from({ length: 10 }, (_, i) =>
          store.deductWithAllowance(PG_USER, D(1), {
            feature: "export",
            featureLimit,
            featurePeriodStart: monthStart,
            idempotencyKey: `fl-conc-${i}`,
          }),
        ),
      );
      const succeeded = results.filter((r) => !r.error);
      const denied = results.filter((r) => r.error === "feature_limit_reached");
      expect(succeeded.length).toBe(5);
      expect(denied.length).toBe(5);
    }, 30000);
  });

  // ── Refunds ─────────────────────────────────────────────────────────
  it("full refund restores balance; over-refund and duplicate rejected", async () => {
    const store = new PostgresStore(DATABASE_URL!, pool);
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
    const store = new PostgresStore(DATABASE_URL!, pool);
    await store.addCredits(PG_USER, D(100), "purchase");
    const deduct = await store.deductWithAllowance(PG_USER, D(50), { idempotencyKey: "ref-2" });

    expect((await store.refundCredits(deduct.transactionId, D(20))).error).toBeUndefined();
    expect((await store.refundCredits(deduct.transactionId, D(20))).error).toBeUndefined();
    const third = await store.refundCredits(deduct.transactionId, D(20));
    expect(third.error).toBe("over_refund");
  });

  it("refund of a purchase (non-debit) is rejected", async () => {
    const store = new PostgresStore(DATABASE_URL!, pool);
    const add = await store.addCredits(PG_USER, D(100), "purchase");
    const refund = await store.refundCredits(add.transactionId);
    expect(refund.error).toBe("over_refund");
  });

  // ── Expiry double-sweep (H4) ────────────────────────────────────────
  it("expired credits sweep once; second sweep reports zero (H4)", async () => {
    const store = new PostgresStore(DATABASE_URL!, pool);
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
    const store = new PostgresStore(DATABASE_URL!, pool);
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
    const store = new PostgresStore(DATABASE_URL!, pool);
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
    const store = new PostgresStore(DATABASE_URL!, pool);
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
    const store = new PostgresStore(DATABASE_URL!, pool);
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
    const store = new PostgresStore(DATABASE_URL!, pool);
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
    const store = new PostgresStore(DATABASE_URL!, pool);
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
    const store = new PostgresStore(DATABASE_URL!, pool);
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
    const store = new PostgresStore(DATABASE_URL!, pool);
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
    const store = new PostgresStore(DATABASE_URL!, pool);
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
    const store = new PostgresStore(DATABASE_URL!, pool);
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
    const store = new PostgresStore(DATABASE_URL!, pool);
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
    const store = new PostgresStore(DATABASE_URL!, pool);
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
    const store = new PostgresStore(DATABASE_URL!, pool);
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
    const store = new PostgresStore(DATABASE_URL!, pool);
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
    const store = new PostgresStore(DATABASE_URL!, pool);

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

    const store = new PostgresStore(DATABASE_URL!, pool);
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

    const pgStore = new PostgresStore(DATABASE_URL!, pool);
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
    const store = new PostgresStore(DATABASE_URL!, pool);
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
    const store = new PostgresStore(DATABASE_URL!, pool);

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

  // ── Lazy expiry: scoped sweep isolation via the real expire_credits RPC ──
  it("sweepExpiredCredits(dryRun, userId) scopes to exactly that user, leaving other users' expired grants untouched", async () => {
    const store = new PostgresStore(DATABASE_URL!, pool);
    // A short-lived FUTURE expiresAt, real wall-clock wait — this database is
    // shared with other suites/tests (possibly a concurrently-running Python
    // integration run against the same cluster) that may have tiers
    // configured globally at any given moment, in which case a *past*
    // expiresAt would be rejected outright (invalid_expires_at); a
    // future-then-elapsed expiresAt is valid either way, exactly like the
    // "Credit tiers — real Postgres" block's own sweep test below.
    await store.addCredits(PG_USER14, D(40), "purchase", null, new Date(Date.now() + 500));
    await store.addCredits(PG_USER15, D(60), "purchase", null, new Date(Date.now() + 500));
    await new Promise((resolve) => setTimeout(resolve, 800));

    const swept = await store.sweepExpiredCredits(false, PG_USER14);
    expect(swept.expiredAmount.toString()).toBe("40");
    expect((await store.getBalance(PG_USER14)).balance.toString()).toBe("0");

    // User B's expired grant is UNTOUCHED — the scoped sweep never looked at it.
    expect((await store.getBalance(PG_USER15)).balance.toString()).toBe("60");

    // A global sweep (or an individually-scoped one) still reaches user B.
    const globalSweep = await store.sweepExpiredCredits(false);
    expect(globalSweep.expiredAmount.toString()).toBe("60");
    expect((await store.getBalance(PG_USER15)).balance.toString()).toBe("0");
    // Release the connection immediately rather than waiting on pg's idle
    // timeout — this file already creates 60+ short-lived PostgresStore pools
    // and a full run comfortably exceeds Postgres's default max_connections
    // if they're all left open simultaneously.
    await store.close();
  }, 10000);

  // ── addCredits idempotency via the real credits_add RPC ──────────────
  it("addCredits with the same idempotencyKey replays the original grant exactly once (credits_add RPC)", async () => {
    const store = new PostgresStore(DATABASE_URL!, pool);

    const r1 = await store.addCredits(
      PG_USER16,
      D(100),
      "purchase",
      undefined,
      undefined,
      undefined,
      "evt-1",
    );
    const r2 = await store.addCredits(
      PG_USER16,
      D(100),
      "purchase",
      undefined,
      undefined,
      undefined,
      "evt-1",
    );

    expect(r2.transactionId).toBe(r1.transactionId);
    expect((await store.getBalance(PG_USER16)).balance.toString()).toBe("100");

    const { rows } = await pool.query(
      `SELECT id FROM public.credit_transactions
       WHERE user_id = $1 AND metadata->>'idempotency_key' = $2`,
      [PG_USER16, "evt-1"],
    );
    expect(rows).toHaveLength(1);
    await store.close();
  });

  // ── lazyExpiry: true end-to-end via CreditManager + real PostgresStore ──
  it("lazyExpiry: true transparently sweeps a user's own expired credits before every read/spend call, no explicit sweep anywhere", async () => {
    const store = new PostgresStore(DATABASE_URL!, pool);
    const manager = new CreditManager(store, undefined, undefined, { lazyExpiry: true });

    // 50 permanent + 100 that expires shortly. A short-lived FUTURE
    // expiresAt + a real wall-clock wait (rather than an already-past
    // expiresAt) keeps this valid regardless of whether some tier happens to
    // be configured globally at this moment on this shared database — see
    // the scoped-sweep test above for the same reasoning.
    await store.addCredits(PG_USER17, D(50), "purchase");
    await store.addCredits(PG_USER17, D(100), "purchase", null, new Date(Date.now() + 500));
    await new Promise((resolve) => setTimeout(resolve, 800));

    // No manual sweepExpiredCredits() call anywhere in this test — every one
    // of these calls must transparently sweep user17's own expired grant
    // first and reflect the TRUE post-expiry balance of 50.
    expect((await manager.getBalance(PG_USER17)).balance.toString()).toBe("50");
    expect((await manager.getAvailable(PG_USER17)).available.toString()).toBe("50");

    const tooMuch = await manager.canAfford(PG_USER17, D(80));
    expect(tooMuch.affordable).toBe(false);
    expect(tooMuch.spendable.toString()).toBe("50");

    const justRight = await manager.canAfford(PG_USER17, D(50));
    expect(justRight.affordable).toBe(true);

    // reserve() (admission) never sizes a hold against the already-expired
    // grant — requesting more than the legitimately-remaining 50 fails.
    await expect(manager.reserve(PG_USER17, D(80))).rejects.toThrow(InsufficientCreditsError);

    // The 50 that legitimately remains is still spendable end-to-end
    // (reserve -> settle), landing the balance at exactly 0.
    const lease = await manager.reserve(PG_USER17, D(50));
    expect(lease.error).toBeUndefined();
    const settled = await manager.settle(PG_USER17, lease.leaseId, D(50));
    expect(settled.error).toBeUndefined();
    expect((await manager.getBalance(PG_USER17)).balance.toString()).toBe("0");
    await store.close();
  }, 10000);
});

// ───────────────────────────────────────────────────────────────────────────
// Configurable allowance window (WS9) — real Postgres. Covers gaps identified
// after the store-level `checkAllowance(userId, periodStart?)` threading and
// the new `CreditManager.checkAllowance` passthrough: allowance_period/
// plan_assigned_at round-trip, window isolation via explicit periodStart on
// deductWithAllowance/createLease/settleLease, rolling_30d/anniversary plans
// driven through the MANAGER (not just the raw store), and a direct live-DB
// regression guard for the positional-argument bug (a missing p_skip_allowance
// placeholder previously shifted periodStart into the wrong SQL parameter
// slot — only ever caught by a MOCKED test before this).
// ───────────────────────────────────────────────────────────────────────────
describe.runIf(DATABASE_URL)("Configurable allowance window (WS9) — real Postgres", () => {
  let pool: pg.Pool;

  beforeAll(async () => {
    pool = new pg.Pool({ connectionString: DATABASE_URL });
    await pool.query(BOOTSTRAP_SQL);
    await applyMigrations(pool);
    await pool.query(
      `INSERT INTO auth.users (id) SELECT unnest($1::uuid[]) ON CONFLICT DO NOTHING`,
      [
        [
          PG_USER,
          PG_USER2,
          PG_USER3,
          PG_USER4,
          PG_USER5,
          PG_USER6,
          PG_USER7,
          PG_USER8,
          PG_USER9,
          PG_USER10,
          PG_USER11,
          PG_USER12,
          PG_USER13,
        ],
      ],
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

  // ── 1/2: getUserPlan + plan-sync round-trip allowance_period ────────
  it("getUserPlan returns allowancePeriod and planAssignedAt for a real Postgres row", async () => {
    const store = new PostgresStore(DATABASE_URL!, pool);
    await pool.query(
      `INSERT INTO public.credit_plans (id, name, free_allowance, plan_key, allowance_period)
       VALUES ($1, 'Rolling', 20, $2, 'rolling_30d')`,
      [PLAN_UUID, PLAN_UUID],
    );
    const before = new Date();
    await store.setUserPlan(PG_USER, PLAN_UUID);
    const after = new Date();

    const plan = await store.getUserPlan(PG_USER);
    expect(plan.allowancePeriod).toBe("rolling_30d");
    expect(plan.planAssignedAt).not.toBeNull();
    expect(plan.planAssignedAt!.getTime()).toBeGreaterThanOrEqual(before.getTime() - 1000);
    expect(plan.planAssignedAt!.getTime()).toBeLessThanOrEqual(after.getTime() + 1000);
  });

  it.each(ALLOWANCE_PERIODS)(
    "plan-sync persists allowance_period=%s to credit_plans via setActivePricing",
    async (allowancePeriod) => {
      const store = new PostgresStore(DATABASE_URL!, pool);
      const planKey = `sync-${allowancePeriod}`;
      const config: PricingConfigData = {
        models: { _default: "1" },
        plans: {
          [planKey]: {
            id: planKey,
            name: `Plan ${allowancePeriod}`,
            freeAllowance: D(15),
            allowancePeriod,
          },
        },
      };
      await store.setActivePricing(config);
      await store.setUserPlan(PG_USER, planKey);

      const plan = await store.getUserPlan(PG_USER);
      expect(plan.allowancePeriod).toBe(allowancePeriod);

      const raw = await pool.query(
        `SELECT allowance_period FROM public.credit_plans WHERE plan_key = $1`,
        [planKey],
      );
      expect((raw.rows[0] as { allowance_period: string }).allowance_period).toBe(allowancePeriod);
    },
  );

  // ── 3: deductWithAllowance periodStart isolates usage windows ───────
  it("deductWithAllowance with explicit periodStart isolates usage into that window", async () => {
    const store = new PostgresStore(DATABASE_URL!, pool);
    await pool.query(
      `INSERT INTO public.credit_plans (id, name, free_allowance, plan_key, allowance_period)
       VALUES ($1, 'RollingIso', 10, $2, 'rolling_30d')`,
      [PLAN_UUID, PLAN_UUID],
    );
    await store.addCredits(PG_USER, D(1000), "purchase");
    await store.setUserPlan(PG_USER, PLAN_UUID);

    const day1 = new Date("2026-01-01T00:00:00.000Z");
    const day31 = new Date("2026-01-31T00:00:00.000Z");

    // Window 1: consume the full allowance.
    const r1 = await store.deductWithAllowance(PG_USER, D(10), {
      idempotencyKey: "iso-1",
      periodStart: day1,
    });
    expect(r1.allowanceConsumed.toString()).toBe("10");
    expect((await store.checkAllowance(PG_USER, day1)).allowanceRemaining.toString()).toBe("0");

    // A different periodStart is a DIFFERENT window — full allowance again.
    expect((await store.checkAllowance(PG_USER, day31)).allowanceRemaining.toString()).toBe("10");
    const r2 = await store.deductWithAllowance(PG_USER, D(10), {
      idempotencyKey: "iso-2",
      periodStart: day31,
    });
    expect(r2.allowanceConsumed.toString()).toBe("10");

    // Window 1 remains exhausted — the two windows are isolated from each other.
    expect((await store.checkAllowance(PG_USER, day1)).allowanceRemaining.toString()).toBe("0");
  });

  // ── 4: createLease/settleLease periodStart isolates usage windows ───
  it("createLease/settleLease with explicit periodStart isolates usage into that window", async () => {
    const store = new PostgresStore(DATABASE_URL!, pool);
    await pool.query(
      `INSERT INTO public.credit_plans (id, name, free_allowance, plan_key, allowance_period)
       VALUES ($1, 'RollingLease', 10, $2, 'rolling_30d')`,
      [PLAN_UUID, PLAN_UUID],
    );
    await store.addCredits(PG_USER, D(1000), "purchase");
    await store.setUserPlan(PG_USER, PLAN_UUID);

    const day1 = new Date("2026-01-01T00:00:00.000Z");
    const day31 = new Date("2026-01-31T00:00:00.000Z");

    const lease1 = await store.createLease(PG_USER, D(10), "usage", { periodStart: day1 });
    const s1 = await store.settleLease(PG_USER, lease1.leaseId, D(10), { periodStart: day1 });
    expect(s1.allowanceConsumed.toString()).toBe("10");
    expect((await store.checkAllowance(PG_USER, day1)).allowanceRemaining.toString()).toBe("0");

    // A different window gets its own full allowance.
    const lease2 = await store.createLease(PG_USER, D(10), "usage", { periodStart: day31 });
    const s2 = await store.settleLease(PG_USER, lease2.leaseId, D(10), { periodStart: day31 });
    expect(s2.allowanceConsumed.toString()).toBe("10");
    expect((await store.checkAllowance(PG_USER, day1)).allowanceRemaining.toString()).toBe("0");
    expect((await store.checkAllowance(PG_USER, day31)).allowanceRemaining.toString()).toBe("0");
  });

  // ── 5: rolling_30d plan through manager.deduct(), backdated anchor ───
  it("manager.deduct() on a rolling_30d plan resolves the window from a backdated plan_assigned_at", async () => {
    const store = new PostgresStore(DATABASE_URL!, pool);
    const manager = new CreditManager(store);
    const config: PricingConfigData = {
      models: { _default: "input_tokens * 1" },
      plans: {
        rolling: {
          id: "rolling",
          name: "Rolling",
          freeAllowance: D(10),
          allowancePeriod: "rolling_30d",
        },
      },
    };
    await store.setActivePricing(config);
    await manager.publishPricingFromDict(config);
    await manager.addCredits(PG_USER3, 1000);
    await manager.setUserPlan(PG_USER3, "rolling");

    // Backdate plan_assigned_at by 35 days — puts "now" into the SECOND 30-day
    // window, so the allowance must have reset to full (unlike a calendar_month
    // plan, which would key off the current calendar month instead).
    await pool.query(
      `UPDATE public.user_credits SET plan_assigned_at = now() - interval '35 days' WHERE user_id = $1`,
      [PG_USER3],
    );

    const d1 = await manager.deduct(PG_USER3, { inputTokens: 4 });
    expect(d1.allowanceConsumed.toString()).toBe("4");
    const allowance = await manager.checkAllowance(PG_USER3);
    expect(allowance.allowanceRemaining.toString()).toBe("6");

    // Cross-check: the window manager.deduct() actually used is anchored 35 days
    // ago, NOT the current calendar month a calendar_month plan would use.
    const plan = await store.getUserPlan(PG_USER3);
    const resolved = resolveAllowanceWindow(new Date(), "rolling_30d", plan.planAssignedAt);
    const calendarWindow = resolveAllowanceWindow(new Date(), "calendar_month", null);
    expect(resolved.start.toISOString()).not.toBe(calendarWindow.start.toISOString());
  });

  // ── 6: anniversary plan through manager.reserve()/settle() ──────────
  it("manager.reserve()/settle() on an anniversary plan resolves the window from a backdated plan_assigned_at", async () => {
    const store = new PostgresStore(DATABASE_URL!, pool);
    const manager = new CreditManager(store);
    const config: PricingConfigData = {
      models: { _default: "input_tokens * 1" },
      plans: {
        anniv: {
          id: "anniv",
          name: "Anniversary",
          freeAllowance: D(10),
          allowancePeriod: "anniversary",
        },
      },
    };
    await store.setActivePricing(config);
    await manager.publishPricingFromDict(config);
    await manager.addCredits(PG_USER4, 1000);
    await manager.setUserPlan(PG_USER4, "anniv");

    // Backdate 35 days so the anniversary reset (monthly, on the anchor's
    // day-of-month) has already occurred — the allowance should be fresh.
    await pool.query(
      `UPDATE public.user_credits SET plan_assigned_at = now() - interval '35 days' WHERE user_id = $1`,
      [PG_USER4],
    );

    const lease = await manager.reserve(PG_USER4, { inputTokens: 6 });
    const settled = await manager.settle(PG_USER4, lease.leaseId, { inputTokens: 6 });
    expect(settled.allowanceConsumed.toString()).toBe("6");
    const allowance = await manager.checkAllowance(PG_USER4);
    expect(allowance.allowanceRemaining.toString()).toBe("4");
  });

  // ── 7: manager.checkAllowance() cross-checked against resolveAllowanceWindow ──
  it.each(["rolling_30d", "anniversary"] as const satisfies readonly AllowancePeriod[])(
    "manager.checkAllowance() for a %s plan matches resolveAllowanceWindow and reflects partial usage",
    async (allowancePeriod) => {
      const store = new PostgresStore(DATABASE_URL!, pool);
      const manager = new CreditManager(store);
      const planKey = `ca-${allowancePeriod}`;
      const config: PricingConfigData = {
        models: { _default: "input_tokens * 1" },
        plans: {
          [planKey]: {
            id: planKey,
            name: planKey,
            freeAllowance: D(20),
            allowancePeriod,
          },
        },
      };
      await store.setActivePricing(config);
      await manager.publishPricingFromDict(config);
      await manager.addCredits(PG_USER5, 1000);
      await manager.setUserPlan(PG_USER5, planKey);

      const plan = await store.getUserPlan(PG_USER5);
      const anchor = plan.planAssignedAt;

      await manager.deduct(PG_USER5, { inputTokens: 7 });

      const now = new Date();
      const expected = resolveAllowanceWindow(now, allowancePeriod, anchor);
      const expectedPeriodEnd = new Date(expected.end.getTime() - 86_400_000);

      const result = await manager.checkAllowance(PG_USER5);
      expect(result.periodStart).toBe(expected.start.toISOString());
      expect(result.periodEnd).toBe(expectedPeriodEnd.toISOString());
      expect(result.allowanceRemaining.toString()).toBe("13");
    },
  );

  // ── 8: manager.checkAllowance() fast path (calendar_month / planless) ──
  it("manager.checkAllowance() fast path is byte-identical to store.checkAllowance() for a calendar_month plan", async () => {
    const store = new PostgresStore(DATABASE_URL!, pool);
    const manager = new CreditManager(store);
    await pool.query(
      `INSERT INTO public.credit_plans (id, name, free_allowance, plan_key, allowance_period)
       VALUES ($1, 'Cal', 15, $2, 'calendar_month')`,
      [PLAN_UUID, PLAN_UUID],
    );
    await store.addCredits(PG_USER6, D(100), "purchase");
    await store.setUserPlan(PG_USER6, PLAN_UUID);
    await store.deductWithAllowance(PG_USER6, D(5), { idempotencyKey: "fastpath-1" });

    const direct = await store.checkAllowance(PG_USER6);
    const viaManager = await manager.checkAllowance(PG_USER6);
    expect(viaManager).toEqual(direct);
  });

  it("manager.checkAllowance() fast path is byte-identical to store.checkAllowance() for a planless user", async () => {
    const store = new PostgresStore(DATABASE_URL!, pool);
    const manager = new CreditManager(store);
    await store.addCredits(PG_USER7, D(10), "purchase");

    const direct = await store.checkAllowance(PG_USER7);
    const viaManager = await manager.checkAllowance(PG_USER7);
    expect(viaManager).toEqual(direct);
  });

  // ── 9: positional-argument regression guard against REAL Postgres ───
  it("deductWithAllowance with a non-null periodStart does not throw a Postgres type-cast error (positional-arg regression guard)", async () => {
    const store = new PostgresStore(DATABASE_URL!, pool);
    await store.addCredits(PG_USER8, D(100), "purchase");

    // Before the fix, a missing p_skip_allowance positional placeholder shifted
    // periodStart into the boolean slot, causing Postgres to reject the RPC call
    // with a cast error (`invalid input syntax for type boolean`) instead of
    // silently mis-binding — so a throw here is exactly what the bug produced.
    const periodStart = new Date("2026-03-01T00:00:00.000Z");
    await expect(
      store.deductWithAllowance(PG_USER8, D(10), {
        idempotencyKey: "regress-1",
        periodStart,
      }),
    ).resolves.not.toThrow();

    const r = await store.deductWithAllowance(PG_USER8, D(5), {
      idempotencyKey: "regress-2",
      periodStart,
    });
    expect(r.error).toBeUndefined();
    expect(r.amount.toString()).toBe("5");
  });

  it("settleLease with a non-null periodStart does not throw a Postgres type-cast error (positional-arg regression guard)", async () => {
    const store = new PostgresStore(DATABASE_URL!, pool);
    await store.addCredits(PG_USER9, D(100), "purchase");
    const periodStart = new Date("2026-03-01T00:00:00.000Z");

    const lease = await store.createLease(PG_USER9, D(10), "usage", { periodStart });
    await expect(
      store.settleLease(PG_USER9, lease.leaseId, D(10), { periodStart }),
    ).resolves.not.toThrow();
  });

  // ── 10: plan-switch re-anchoring via real Postgres ───────────────────
  it("setUserPlan re-anchors plan_assigned_at on every (re-)assignment", async () => {
    const store = new PostgresStore(DATABASE_URL!, pool);
    const PLAN_X = "00000000-0000-0000-0000-0000000000d1";
    const PLAN_Y = "00000000-0000-0000-0000-0000000000d2";
    await pool.query(
      `INSERT INTO public.credit_plans (id, name, free_allowance, plan_key)
       VALUES ($1, 'X', 5, $2), ($3, 'Y', 5, $4)`,
      [PLAN_X, PLAN_X, PLAN_Y, PLAN_Y],
    );

    await store.setUserPlan(PG_USER10, PLAN_X);
    const first = (await store.getUserPlan(PG_USER10)).planAssignedAt!;

    // Postgres timestamptz resolution — sleep briefly so the second now() call
    // is guaranteed to differ from the first.
    await new Promise((r) => setTimeout(r, 10));

    await store.setUserPlan(PG_USER10, PLAN_Y);
    const second = (await store.getUserPlan(PG_USER10)).planAssignedAt!;

    expect(second.getTime()).toBeGreaterThan(first.getTime());
  });

  // ── 11: WS3 — fractional fixed job cost round-trips through Postgres JSONB ──
  it("WS3 — fractional fixed job cost round-trips through real Postgres JSONB and charges exactly", async () => {
    const store = new PostgresStore(DATABASE_URL!, pool);
    const manager = new CreditManager(store);
    const config: PricingConfigData = {
      models: { _default: "input_tokens * 1" },
      fixed: { job: 2.5 },
    };
    await store.setActivePricing(config);
    await manager.publishPricingFromDict(config);

    const stored = await store.getActivePricing();
    expect(stored?.config.fixed?.job).toBe(2.5);

    await manager.addCredits(PG_USER11, 10);
    const result = await manager.deductFixed(PG_USER11, "job");
    expect(result.amount.toString()).toBe("2.5");
    expect((await manager.getBalance(PG_USER11)).balance.toString()).toBe("7.5");
  });

  // ── 12: WS10 — manager.addCredits options-object form persists expiresAt ──
  it("WS10 — manager.addCredits options-object form persists expiresAt and sweepExpiredCredits reclaims exactly that grant", async () => {
    const store = new PostgresStore(DATABASE_URL!, pool);
    const manager = new CreditManager(store);

    await manager.addCredits(PG_USER12, D(30), {
      type: "purchase",
      metadata: { referenceType: "promo" },
      expiresAt: new Date(Date.now() - 1000),
    });
    // A second, non-expiring grant must survive the sweep untouched.
    await manager.addCredits(PG_USER12, D(20), { type: "purchase" });

    expect((await manager.getBalance(PG_USER12)).balance.toString()).toBe("50");

    const swept = await manager.sweepExpiredCredits();
    expect(swept.expiredAmount.toString()).toBe("30");
    expect((await manager.getBalance(PG_USER12)).balance.toString()).toBe("20");
  });

  // ── 13: settle_lease canonical-signature joint regression guard ─────
  it("settleLease jointly exercises floor-clamp (C1) and periodStart (WS9) against the canonical single signature", async () => {
    const store = new PostgresStore(DATABASE_URL!, pool);
    await pool.query(
      `INSERT INTO public.credit_plans (id, name, free_allowance, plan_key, allowance_period)
       VALUES ($1, 'JointGuard', 5, $2, 'rolling_30d')`,
      [PLAN_UUID, PLAN_UUID],
    );
    // Balance of 8: floor 0 means settle can debit at most 8 net regardless of
    // the lease's nominal amount — this exercises the 021 floor-clamp fix.
    await store.addCredits(PG_USER13, D(8), "purchase");
    await store.setUserPlan(PG_USER13, PLAN_UUID);

    // Lease admission only needs to cover the worst-case hold — request a small
    // amount well within `balance(8) + allowance headroom(5) = 13` so admission
    // succeeds; settle then bills the real (larger) actual cost (de-clamped).
    const periodStart = new Date("2026-05-01T00:00:00.000Z");
    const lease = await store.createLease(PG_USER13, D(5), "usage", {
      billingMode: "strict",
      floor: D(0),
      periodStart,
    });
    expect(lease.error).toBeUndefined();

    // Settle the ACTUAL cost of 20: allowance (5) covers the first 5, leaving a
    // net of 15 to debit — but the floor-clamp must cap the debit at the
    // available balance (8), so v_net clamps to 8 and allowance re-clamps too.
    const settled = await store.settleLease(PG_USER13, lease.leaseId, D(20), {
      minBalance: D(0),
      periodStart,
    });
    expect(settled.error).toBeUndefined();
    // Floor-clamp (021/C1): net debit is capped at the available balance (8),
    // NOT the full 15 (20 actual cost − 5 allowance) it would otherwise be.
    expect(settled.amount.toString()).toBe("8");
    expect(settled.balanceAfter.toString()).toBe("0");
    // Allowance still consumes its full share (5) — the clamp only bites the
    // balance-funded remainder, confirming p_period_start and the floor clamp
    // operate independently rather than one silently overriding the other.
    expect(settled.allowanceConsumed.toString()).toBe("5");

    // The allowance consumed must have come from the SAME window keyed by
    // periodStart — confirms p_period_start threaded correctly alongside the
    // floor clamp rather than one silently overriding the other.
    const allowance = await store.checkAllowance(PG_USER13, periodStart);
    expect(allowance.allowanceRemaining.toString()).toBe(
      D(5).minus(settled.allowanceConsumed).toString(),
    );
  });
});

// ───────────────────────────────────────────────────────────────────────────
// Credit tiers (010_credit_tiers.sql) — real Postgres. Mirrors the
// tiers.test.ts MemoryStore scenarios against the actual RPCs
// (credits_add / deduct_with_allowance / settle_lease / refund_credits /
// expire_credits / get_user_credit_tiers), guarded by the same
// skip-without-DATABASE_URL pattern as the rest of this file.
// ───────────────────────────────────────────────────────────────────────────
describe.runIf(DATABASE_URL)("Credit tiers — real Postgres", () => {
  let pool: pg.Pool;

  const TIER_CONFIG: PricingConfigData = {
    models: { _default: "input_tokens * 1" },
    tiers: {
      gifted: { name: "Gifted", priority: 10, expires: true, defaultTtlDays: 30 },
      purchased: { name: "Purchased", priority: 20, expires: false, isDefault: true },
    },
  };

  beforeAll(async () => {
    pool = new pg.Pool({ connectionString: DATABASE_URL });
    await pool.query(BOOTSTRAP_SQL);
    await applyMigrations(pool);
    await pool.query(
      `INSERT INTO auth.users (id) SELECT unnest($1::uuid[]) ON CONFLICT DO NOTHING`,
      [[PG_USER, PG_USER2, PG_USER3, PG_USER4, PG_USER5, PG_USER6]],
    );
  }, 60000);

  afterEach(async () => {
    if (pool) {
      await pool.query("DELETE FROM public.credit_reservations");
      await pool.query("DELETE FROM public.credit_transactions");
      await pool.query("UPDATE public.user_credits SET plan_id = NULL");
      // user_credit_tiers cascades away via ON DELETE CASCADE on user_credits.
      await pool.query("DELETE FROM public.user_credits");
      await pool.query("DELETE FROM public.credit_plans");
      // credit_tiers is upsert-only (sync_tiers_from_config never deletes a
      // stale row, matching MemoryStore's own accumulate-only semantics — see
      // tiers-adversarial.test.ts's config-drift test) — clear it explicitly
      // between tests so one test's tier config never leaks into the next.
      await pool.query("DELETE FROM public.user_credit_tiers");
      await pool.query("DELETE FROM public.credit_tiers");
    }
  });

  afterAll(async () => {
    if (pool) await pool.end();
  });

  it("addCredits into an explicit tier is reflected by getCreditTiers, sorted by priority", async () => {
    const store = new PostgresStore(DATABASE_URL!, pool);
    await store.setActivePricing(TIER_CONFIG);

    const add = await store.addCredits(PG_USER, D(20), "adjustment", null, null, "gifted");
    expect(add.tier).toBe("gifted");
    await store.addCredits(PG_USER, D(10)); // omitted → default "purchased"

    const tiers = await store.getCreditTiers(PG_USER);
    expect(tiers.tiers.map((t) => t.tierKey)).toEqual(["gifted", "purchased"]);
    expect(tiers.tiers.find((t) => t.tierKey === "gifted")?.balance.toString()).toBe("20");
    expect(tiers.tiers.find((t) => t.tierKey === "purchased")?.balance.toString()).toBe("10");
    expect(tiers.totalBalance.toString()).toBe("30");
  });

  it("deductWithAllowance drains tiers in priority order and returns an exact tierBreakdown", async () => {
    const store = new PostgresStore(DATABASE_URL!, pool);
    await store.setActivePricing(TIER_CONFIG);
    await store.addCredits(PG_USER2, D(20), "adjustment", null, null, "gifted");
    await store.addCredits(PG_USER2, D(10)); // omitted → default "purchased"

    const r = await store.deductWithAllowance(PG_USER2, D(25), { minBalance: D(0) });
    expect(r.error).toBeUndefined();
    expect(r.tierBreakdown?.gifted?.toString()).toBe("20");
    expect(r.tierBreakdown?.purchased?.toString()).toBe("5");

    const tiers = await store.getCreditTiers(PG_USER2);
    expect(tiers.tiers.find((t) => t.tierKey === "gifted")?.balance.toString()).toBe("0");
    expect(tiers.tiers.find((t) => t.tierKey === "purchased")?.balance.toString()).toBe("5");
  });

  it("settleLease applies the same tier walk as deductWithAllowance", async () => {
    const store = new PostgresStore(DATABASE_URL!, pool);
    await store.setActivePricing(TIER_CONFIG);
    await store.addCredits(PG_USER3, D(20), "adjustment", null, null, "gifted");
    await store.addCredits(PG_USER3, D(10)); // omitted → default "purchased"

    const lease = await store.createLease(PG_USER3, D(25), "usage", { floor: D(0) });
    expect(lease.error).toBeUndefined();
    const settled = await store.settleLease(PG_USER3, lease.leaseId, D(18));
    expect(settled.error).toBeUndefined();
    expect(settled.tierBreakdown?.gifted?.toString()).toBe("18");

    const tiers = await store.getCreditTiers(PG_USER3);
    expect(tiers.tiers.find((t) => t.tierKey === "gifted")?.balance.toString()).toBe("2");
    expect(tiers.tiers.find((t) => t.tierKey === "purchased")?.balance.toString()).toBe("10");
  });

  it("refundCredits restores tiers LIFO (reverse priority order)", async () => {
    const store = new PostgresStore(DATABASE_URL!, pool);
    await store.setActivePricing(TIER_CONFIG);
    await store.addCredits(PG_USER4, D(20), "adjustment", null, null, "gifted");
    await store.addCredits(PG_USER4, D(20)); // omitted → default "purchased"

    const deduct = await store.deductWithAllowance(PG_USER4, D(25), { minBalance: D(0) });
    expect(deduct.error).toBeUndefined();
    expect(deduct.tierBreakdown?.gifted?.toString()).toBe("20");
    expect(deduct.tierBreakdown?.purchased?.toString()).toBe("5");

    const refund = await store.refundCredits(deduct.transactionId);
    expect(refund.error).toBeUndefined();
    // LIFO: purchased (last drained) is restored first, then gifted.
    expect(refund.tierBreakdown?.purchased?.toString()).toBe("5");
    expect(refund.tierBreakdown?.gifted?.toString()).toBe("20");

    const tiers = await store.getCreditTiers(PG_USER4);
    expect(tiers.tiers.find((t) => t.tierKey === "gifted")?.balance.toString()).toBe("20");
    expect(tiers.tiers.find((t) => t.tierKey === "purchased")?.balance.toString()).toBe("20");
  });

  it("sweepExpiredCredits expires only the expiring tier's grant, leaving the non-expiring tier untouched", async () => {
    const store = new PostgresStore(DATABASE_URL!, pool);
    await store.setActivePricing(TIER_CONFIG);
    // Already-imminent expiresAt (must be in the future at grant time — see
    // invalid_expires_at — but elapsed by the time we sweep).
    await store.addCredits(
      PG_USER5,
      D(15),
      "adjustment",
      null,
      new Date(Date.now() + 500),
      "gifted",
    );
    await store.addCredits(PG_USER5, D(10)); // omitted → default "purchased" — never expires

    // Real wall-clock wait — there is no injectable clock over a live RPC.
    await new Promise((resolve) => setTimeout(resolve, 800));

    const swept = await store.sweepExpiredCredits(false);
    expect(swept.expiredByTier?.gifted?.toString()).toBe("15");

    const tiers = await store.getCreditTiers(PG_USER5);
    expect(tiers.tiers.find((t) => t.tierKey === "gifted")?.balance.toString()).toBe("0");
    expect(tiers.tiers.find((t) => t.tierKey === "purchased")?.balance.toString()).toBe("10");
  }, 10000);

  it("addCredits / getCreditTiers synthesize the 'default' tier when no tiers are configured", async () => {
    const store = new PostgresStore(DATABASE_URL!, pool);
    // No setActivePricing call in this test — no tiers configured at all.
    const add = await store.addCredits(PG_USER6, D(50), "purchase");
    expect(add.tier).toBe("default");

    const tiers = await store.getCreditTiers(PG_USER6);
    expect(tiers.tiers).toHaveLength(1);
    expect(tiers.tiers[0]).toMatchObject({ tierKey: "default", name: "default", priority: 0 });
    expect(tiers.tiers[0].balance.toString()).toBe("50");
    expect(tiers.totalBalance.toString()).toBe("50");
  });
});

// ───────────────────────────────────────────────────────────────────────────
// CreditManager end-to-end — credit tiers through the public manager API,
// real Postgres. The "Credit tiers — real Postgres" block above drives
// PostgresStore directly to pin down SQL/RPC behavior; this block is the
// manager-level counterpart: publishPricingFromDict, addCredits, the
// pricing-engine-driven deduct, the reserve/settle lease lifecycle,
// refundCredits, getCreditTiers, and sweepExpiredCredits exactly as an
// integrator would call them, asserting on both the returned results and the
// CreditEventEmitter events they fire. Nothing else in this suite drives
// CreditManager against a real store (every other manager test uses
// MemoryStore).
// ───────────────────────────────────────────────────────────────────────────
describe.runIf(DATABASE_URL)("CreditManager end-to-end — credit tiers, real Postgres", () => {
  let pool: pg.Pool;

  const MGR_USER1 = "00000000-0000-0000-0000-000000000301";
  const MGR_USER2 = "00000000-0000-0000-0000-000000000302";
  const MGR_USER3 = "00000000-0000-0000-0000-000000000303";
  const MGR_USER4 = "00000000-0000-0000-0000-000000000304";

  const TIER_CONFIG: PricingConfigData = {
    models: { _default: "input_tokens * 1" },
    minBalance: 0,
    tiers: {
      gifted: { name: "Gifted", priority: 10, expires: true, defaultTtlDays: 30 },
      purchased: { name: "Purchased", priority: 30, expires: false, isDefault: true },
    },
  };

  function record(emitter: CreditEventEmitter, types: string[]): CreditEvent[] {
    const events: CreditEvent[] = [];
    for (const t of types) {
      emitter.on(t as CreditEvent["type"], (e) => events.push(e));
    }
    return events;
  }

  beforeAll(async () => {
    pool = new pg.Pool({ connectionString: DATABASE_URL });
    await pool.query(BOOTSTRAP_SQL);
    await applyMigrations(pool);
    await pool.query(
      `INSERT INTO auth.users (id) SELECT unnest($1::uuid[]) ON CONFLICT DO NOTHING`,
      [[MGR_USER1, MGR_USER2, MGR_USER3, MGR_USER4]],
    );
    // These UUIDs are new to this describe block: the INSERT above fires
    // grant_signup_bonus() (001_core_schema.sql) for the first time,
    // crediting a real balance (defaults to 50 if unset) before the first
    // test runs. Wipe it (transactions first — FK from credit_transactions to
    // user_credits) so every test starts from a true zero balance — mirrors
    // the afterEach cleanup below, just run once up front too.
    await pool.query("DELETE FROM public.credit_transactions");
    await pool.query("DELETE FROM public.user_credits");
  }, 60000);

  afterEach(async () => {
    if (pool) {
      await pool.query("DELETE FROM public.credit_reservations");
      await pool.query("DELETE FROM public.credit_transactions");
      await pool.query("UPDATE public.user_credits SET plan_id = NULL");
      // user_credit_tiers cascades away via ON DELETE CASCADE on user_credits.
      await pool.query("DELETE FROM public.user_credits");
      await pool.query("DELETE FROM public.credit_plans");
      await pool.query("DELETE FROM public.user_credit_tiers");
      await pool.query("DELETE FROM public.credit_tiers");
    }
  });

  afterAll(async () => {
    if (pool) await pool.end();
  });

  it("full lifecycle: publish tiers, addCredits, deduct, refund — results and events agree with getCreditTiers at each step", async () => {
    const store = new PostgresStore(DATABASE_URL!, pool);
    const emitter = new CreditEventEmitter();
    const events = record(emitter, ["credits.added", "credits.deducted", "credits.refunded"]);
    const mgr = new CreditManager(store, undefined, emitter);
    await mgr.publishPricingFromDict(TIER_CONFIG);

    const gifted = await mgr.addCredits(MGR_USER1, D(20), { type: "purchase", tier: "gifted" });
    expect(gifted.tier).toBe("gifted");
    const purchased = await mgr.addCredits(MGR_USER1, D(50), { type: "purchase" }); // omitted -> isDefault
    expect(purchased.tier).toBe("purchased");
    expect(events.map((e) => e.type)).toEqual(["credits.added", "credits.added"]);

    const tiers = await mgr.getCreditTiers(MGR_USER1);
    expect(tiers.tiers.find((t) => t.tierKey === "gifted")?.balance.toString()).toBe("20");
    expect(tiers.tiers.find((t) => t.tierKey === "purchased")?.balance.toString()).toBe("50");
    expect(tiers.totalBalance.toString()).toBe("70");

    // Cost computed by the real pricing engine (not a raw amount) crosses the
    // tier boundary: 25 tokens @ 1/token drains gifted (20) then 5 from
    // purchased.
    events.length = 0;
    const result = await mgr.deduct(MGR_USER1, { inputTokens: 25 }, "mgr-e2e-1");
    expect(result.tierBreakdown?.gifted?.toString()).toBe("20");
    expect(result.tierBreakdown?.purchased?.toString()).toBe("5");
    expect(result.balanceAfter.toString()).toBe("45");
    expect(events.map((e) => e.type)).toContain("credits.deducted");

    const tiersAfterDeduct = await mgr.getCreditTiers(MGR_USER1);
    expect(tiersAfterDeduct.tiers.find((t) => t.tierKey === "gifted")?.balance.toString()).toBe(
      "0",
    );
    expect(tiersAfterDeduct.tiers.find((t) => t.tierKey === "purchased")?.balance.toString()).toBe(
      "45",
    );

    events.length = 0;
    const refund = await mgr.refundCredits(result.transactionId);
    expect(refund.error).toBeFalsy();
    // LIFO: purchased (last drained) is restored first, then gifted.
    expect(refund.tierBreakdown?.purchased?.toString()).toBe("5");
    expect(refund.tierBreakdown?.gifted?.toString()).toBe("20");
    expect(events.map((e) => e.type)).toContain("credits.refunded");

    const tiersAfterRefund = await mgr.getCreditTiers(MGR_USER1);
    expect(tiersAfterRefund.tiers.find((t) => t.tierKey === "gifted")?.balance.toString()).toBe(
      "20",
    );
    expect(tiersAfterRefund.tiers.find((t) => t.tierKey === "purchased")?.balance.toString()).toBe(
      "50",
    );
  });

  it("reserve/settle lease lifecycle applies the same tier walk as direct deduct", async () => {
    const store = new PostgresStore(DATABASE_URL!, pool);
    const emitter = new CreditEventEmitter();
    const events = record(emitter, ["credits.reserved", "credits.deducted"]);
    const mgr = new CreditManager(store, undefined, emitter);
    await mgr.publishPricingFromDict(TIER_CONFIG);

    const future = new Date(Date.now() + 86_400_000);
    await mgr.addCredits(MGR_USER2, D(10), { type: "purchase", tier: "gifted", expiresAt: future });
    await mgr.addCredits(MGR_USER2, D(100), { type: "purchase" });

    const lease = await mgr.reserve(MGR_USER2, { inputTokens: 15 });
    expect(events.map((e) => e.type)).toContain("credits.reserved");

    const settled = await mgr.settle(MGR_USER2, lease.leaseId, { inputTokens: 15 });
    expect(settled.tierBreakdown?.gifted?.toString()).toBe("10");
    expect(settled.tierBreakdown?.purchased?.toString()).toBe("5");

    const tiers = await mgr.getCreditTiers(MGR_USER2);
    expect(tiers.tiers.find((t) => t.tierKey === "gifted")?.balance.toString()).toBe("0");
    expect(tiers.tiers.find((t) => t.tierKey === "purchased")?.balance.toString()).toBe("95");
  });

  it("sweepExpiredCredits through the manager scopes per tier and emits credits.expired", async () => {
    const store = new PostgresStore(DATABASE_URL!, pool);
    const emitter = new CreditEventEmitter();
    const events = record(emitter, ["credits.expired"]);
    const mgr = new CreditManager(store, undefined, emitter);
    await mgr.publishPricingFromDict(TIER_CONFIG);

    // Must be in the future at grant time (invalid_expires_at) but elapsed by
    // the time we sweep — real wall-clock wait, no injectable clock over a
    // live RPC.
    await mgr.addCredits(MGR_USER3, D(15), {
      type: "purchase",
      tier: "gifted",
      expiresAt: new Date(Date.now() + 500),
    });
    await mgr.addCredits(MGR_USER3, D(10), { type: "purchase" }); // purchased — never expires

    await new Promise((resolve) => setTimeout(resolve, 800));

    const swept = await mgr.sweepExpiredCredits(false);
    expect(swept.expiredByTier?.gifted?.toString()).toBe("15");
    expect(events.map((e) => e.type)).toContain("credits.expired");

    const tiers = await mgr.getCreditTiers(MGR_USER3);
    expect(tiers.tiers.find((t) => t.tierKey === "gifted")?.balance.toString()).toBe("0");
    expect(tiers.tiers.find((t) => t.tierKey === "purchased")?.balance.toString()).toBe("10");
  }, 10000);

  it("settle() past the balance floor routes overdraft excess into the allowOverdraft tier and emits credits.overdraft", async () => {
    // The client-side config schema rejects a negative minBalance outright, so
    // overdraft can only be reached via the manager's `overdraft` policy
    // (resolved into the lease admission floor), not via pricing config.
    const store = new PostgresStore(DATABASE_URL!, pool);
    const emitter = new CreditEventEmitter();
    const events = record(emitter, ["credits.overdraft"]);
    const mgr = new CreditManager(store, undefined, emitter, {
      policy: "overdraft",
      overdraftFloor: -50,
    });
    await mgr.publishPricingFromDict({
      models: { _default: "input_tokens * 1" },
      tiers: {
        gifted: { name: "Gifted", priority: 10, expires: false, isDefault: true },
        purchased: { name: "Purchased", priority: 20, expires: false, allowOverdraft: true },
      },
    });
    await mgr.addCredits(MGR_USER4, D(10), { type: "purchase" }); // -> gifted (isDefault)
    await mgr.addCredits(MGR_USER4, D(5), { type: "purchase", tier: "purchased" });

    const lease = await mgr.reserve(MGR_USER4, { inputTokens: 40 });
    const settled = await mgr.settle(MGR_USER4, lease.leaseId, { inputTokens: 40 });
    expect(settled.tierBreakdown?.gifted?.toString()).toBe("10");
    expect(settled.tierBreakdown?.purchased?.toString()).toBe("30");
    expect(settled.balanceAfter.toString()).toBe("-25");
    expect(events.map((e) => e.type)).toContain("credits.overdraft");

    const tiers = await mgr.getCreditTiers(MGR_USER4);
    expect(tiers.tiers.find((t) => t.tierKey === "purchased")?.balance.toString()).toBe("-25");
  });
});

// ───────────────────────────────────────────────────────────────────────────
// CreditManager.grantSubscriptionCycle — real Postgres. A real bug was found
// and fixed in this exact implementation: with replacePrior:true (the
// default), a redelivered webhook (same idempotencyKey) used to wipe the
// tier's balance UNCONDITIONALLY before the idempotent grant call, so a
// replay would double-wipe a balance it had just legitimately granted down to
// zero. The fix snapshots the tier's leftover balance and lifetimePurchased
// BEFORE granting, then only performs the wipe AFTER, gated on
// `result.lifetimePurchased.minus(preLifetimePurchased).eq(amountDec)` (i.e.
// only for a genuine new grant, never a replay). This is proven against
// MemoryStore in tests/subscription-cycle.test.ts; this block proves the same
// fix against the actual credits_add / tier-balance Postgres RPCs, since a
// client/server logic mismatch could hide exactly there.
// ───────────────────────────────────────────────────────────────────────────
describe.runIf(DATABASE_URL)("CreditManager.grantSubscriptionCycle — real Postgres", () => {
  let pool: pg.Pool;
  // One store per test, closed in afterEach — this file already opens 60+
  // short-lived PostgresStore pools elsewhere, comfortably exceeding
  // Postgres's default max_connections if every one of them is left open
  // (idle connections aren't reaped by `pg` until well after this suite
  // finishes running).
  let store: PostgresStore;

  const SUB_USER1 = "00000000-0000-0000-0000-000000000401";
  const SUB_USER2 = "00000000-0000-0000-0000-000000000402";
  const SUB_USER3 = "00000000-0000-0000-0000-000000000403";
  const SUB_USER4 = "00000000-0000-0000-0000-000000000404";

  // A "subscription" tier that expires, with a defaultTtlDays fallback so the
  // replacePrior expire-adjustment (which never passes an explicit
  // expiresAt) can always resolve one, exactly like any other expiring tier
  // — mirrors tests/subscription-cycle.test.ts's SUBSCRIPTION_CONFIG.
  const SUBSCRIPTION_CONFIG: PricingConfigData = {
    models: { _default: "input_tokens * 1" },
    tiers: {
      subscription: {
        name: "Subscription",
        priority: 10,
        expires: true,
        defaultTtlDays: 30,
        isDefault: true,
      },
    },
  };

  beforeAll(async () => {
    pool = new pg.Pool({ connectionString: DATABASE_URL });
    await pool.query(BOOTSTRAP_SQL);
    await applyMigrations(pool);
    await pool.query(
      `INSERT INTO auth.users (id) SELECT unnest($1::uuid[]) ON CONFLICT DO NOTHING`,
      [[SUB_USER1, SUB_USER2, SUB_USER3, SUB_USER4]],
    );
    // Fresh UUIDs to this describe block trigger grant_signup_bonus() on
    // first INSERT (001_core_schema.sql) — wipe any resulting balance so
    // every test starts from a true zero, exactly like the "CreditManager
    // end-to-end — credit tiers" block above.
    await pool.query("DELETE FROM public.credit_transactions");
    await pool.query("DELETE FROM public.user_credits");
  }, 60000);

  beforeEach(() => {
    store = new PostgresStore(DATABASE_URL!, pool);
  });

  afterEach(async () => {
    await store.close();
    if (pool) {
      await pool.query("DELETE FROM public.credit_reservations");
      await pool.query("DELETE FROM public.credit_transactions");
      await pool.query("UPDATE public.user_credits SET plan_id = NULL");
      // user_credit_tiers cascades away via ON DELETE CASCADE on user_credits.
      await pool.query("DELETE FROM public.user_credits");
      await pool.query("DELETE FROM public.credit_plans");
      await pool.query("DELETE FROM public.user_credit_tiers");
      await pool.query("DELETE FROM public.credit_tiers");
    }
  });

  afterAll(async () => {
    if (pool) await pool.end();
  });

  it("first cycle grant increases the balance; a SAME-idempotencyKey redelivery is a full no-op (the exact regression this fix addresses)", async () => {
    const manager = new CreditManager(store);
    await manager.publishPricingFromDict(SUBSCRIPTION_CONFIG);

    const first = await manager.grantSubscriptionCycle(SUB_USER1, D(100), {
      ttlDays: 30,
      idempotencyKey: "invoice-123",
    });
    expect(first.tier).toBe("subscription");
    expect(first.amount.toString()).toBe("100");
    expect((await manager.getBalance(SUB_USER1)).balance.toString()).toBe("100");

    // Redelivery of the SAME webhook event against the REAL credits_add /
    // tier-balance RPCs: before the fix this unconditionally wiped the
    // "subscription" tier (replacePrior defaults to true) down to zero even
    // though the grant itself replayed idempotently — a redelivered webhook
    // must be a full no-op, not a balance-destroying one.
    const redelivered = await manager.grantSubscriptionCycle(SUB_USER1, D(100), {
      ttlDays: 30,
      idempotencyKey: "invoice-123",
    });
    expect(redelivered.transactionId).toBe(first.transactionId);
    expect((await manager.getBalance(SUB_USER1)).balance.toString()).toBe("100");

    // A THIRD redelivery must still be a no-op.
    await manager.grantSubscriptionCycle(SUB_USER1, D(100), {
      ttlDays: 30,
      idempotencyKey: "invoice-123",
    });
    expect((await manager.getBalance(SUB_USER1)).balance.toString()).toBe("100");
  });

  it("a genuinely new cycle (different idempotencyKey) expires the leftover balance from a prior cycle when replacePrior: true", async () => {
    const manager = new CreditManager(store);
    await manager.publishPricingFromDict(SUBSCRIPTION_CONFIG);

    await manager.grantSubscriptionCycle(SUB_USER2, D(100), {
      ttlDays: 30,
      idempotencyKey: "cycle-1",
    });
    expect((await manager.getBalance(SUB_USER2)).balance.toString()).toBe("100");

    // Cycle 2 — no usage in between, 100 left over from cycle 1, a genuinely
    // new idempotencyKey (a real new billing cycle, not a webhook replay).
    await manager.grantSubscriptionCycle(SUB_USER2, D(50), {
      ttlDays: 30,
      idempotencyKey: "cycle-2",
    });
    // The 100 leftover was expired, not stacked: balance is 50, not 150.
    expect((await manager.getBalance(SUB_USER2)).balance.toString()).toBe("50");
  });

  it("replacePrior: false stacks the new cycle on top of any leftover balance", async () => {
    const manager = new CreditManager(store);
    await manager.publishPricingFromDict(SUBSCRIPTION_CONFIG);

    await manager.grantSubscriptionCycle(SUB_USER3, D(100), {
      ttlDays: 30,
      idempotencyKey: "cycle-1",
    });
    await manager.grantSubscriptionCycle(SUB_USER3, D(50), {
      ttlDays: 30,
      replacePrior: false,
      idempotencyKey: "cycle-2",
    });
    expect((await manager.getBalance(SUB_USER3)).balance.toString()).toBe("150");
  });

  it("assigns planKey via setUserPlan and emits credits.cycle_renewed against the real store", async () => {
    const emitter = new CreditEventEmitter();
    const events: CreditEvent[] = [];
    emitter.on("credits.cycle_renewed", (e) => events.push(e));
    const manager = new CreditManager(store, undefined, emitter);
    await manager.publishPricingFromDict(SUBSCRIPTION_CONFIG);

    // set_user_plan resolves plan_key -> credit_plans.id server-side, so the
    // plan must actually exist (unlike MemoryStore, where planId IS the raw
    // plan_key string).
    const SUB_PLAN = "00000000-0000-0000-0000-0000000000e1";
    await pool.query(
      `INSERT INTO public.credit_plans (id, name, free_allowance, plan_key) VALUES ($1, 'Pro', 0, $2)`,
      [SUB_PLAN, "pro-monthly"],
    );

    await manager.grantSubscriptionCycle(SUB_USER4, D(100), {
      ttlDays: 30,
      planKey: "pro-monthly",
    });

    expect((await manager.getUserPlan(SUB_USER4)).planId).toBe(SUB_PLAN);
    expect(events).toHaveLength(1);
    expect(events[0].userId).toBe(SUB_USER4);
    expect(events[0].data?.tier).toBe("subscription");
  });
});
