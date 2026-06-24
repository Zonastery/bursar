import { describe, it, expect, beforeAll, afterAll, afterEach } from "vitest";
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
  models: {
    "gpt-4": "input_tokens * 0.01 + output_tokens * 0.03",
    _default: "input_tokens * 0.001 + output_tokens * 0.003",
  },
  tools: { _default: "tool_calls * 0" },
  minBalance: 5,
};

const PG_USER = "00000000-0000-0000-0000-000000000001";
const PG_USER2 = "00000000-0000-0000-0000-000000000099";
const PLAN_UUID = "00000000-0000-0000-0000-0000000000a1";

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
          CREATE TABLE IF NOT EXISTS auth.users (id uuid PRIMARY KEY);
          CREATE ROLE anon;
          CREATE ROLE authenticated;
          CREATE FUNCTION auth.uid() RETURNS uuid
          LANGUAGE SQL IMMUTABLE AS $func$ SELECT '00000000-0000-0000-0000-000000000000'::uuid $func$;
          INSERT INTO auth.users (id) VALUES ('00000000-0000-0000-0000-000000000001') ON CONFLICT DO NOTHING;
          INSERT INTO auth.users (id) VALUES ('00000000-0000-0000-0000-000000000099') ON CONFLICT DO NOTHING;
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

  // ── Basic lifecycle ─────────────────────────────────────────────────

  it("setup is idempotent", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    const r1 = await store.setup();
    expect(r1.success).toBe(true);
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

    await expect(() => manager.deduct(PG_USER2, METRICS)).rejects.toThrow();
  });

  it("reserve and deduct flow", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    const manager = new CreditManager(store);
    await manager.publishPricingFromDict(TEST_PRICING);

    await manager.addCredits(PG_USER, 100);

    const reserve = await manager.reserveCredits(PG_USER, 30, "usage");
    expect(reserve.amount).toBe(30);

    const over = await manager.reserveCredits(PG_USER, 999, "usage");
    expect(over.reservationId).toBeTruthy();
    expect(over.amount).toBeGreaterThan(0);
  });

  // ── Idempotency ─────────────────────────────────────────────────────

  it("deduct with same idempotency key returns idempotent=true", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    const manager = new CreditManager(store);
    await manager.publishPricingFromDict(TEST_PRICING);
    await manager.addCredits(PG_USER, 100);

    const r1 = await manager.deduct(PG_USER, METRICS, "idem-deduct-1");
    expect(r1.idempotent).toBe(false);

    const r2 = await manager.deduct(PG_USER, METRICS, "idem-deduct-1");
    expect(r2.idempotent).toBe(true);
    expect(r2.balanceAfter).toBe(r1.balanceAfter);
  });

  it("different idempotency keys produce separate deductions", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    const manager = new CreditManager(store);
    await manager.publishPricingFromDict(TEST_PRICING);
    await manager.addCredits(PG_USER, 100);

    await manager.deduct(PG_USER, METRICS, "idem-a");
    const r2 = await manager.deduct(PG_USER, METRICS, "idem-b");
    expect(r2.idempotent).toBe(false);
    expect(r2.balanceAfter).toBe(100 - EXPECTED_COST * 2);
  });

  // ── Fixed job deductions ────────────────────────────────────────────

  it("deductFixed deducts fixed cost", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    const manager = new CreditManager(store);
    const config: PricingConfigData = {
      models: { _default: "input_tokens * 1" },
      fixed: { batch_gen: 75 },
    };
    await manager.publishPricingFromDict(config);
    await manager.addCredits(PG_USER, 100);

    const result = await manager.deductFixed(PG_USER, "batch_gen");
    expect(result.amount).toBe(-75);

    const balance = await manager.getBalance(PG_USER);
    expect(balance.balance).toBe(25);
  });

  // ── Pricing config round-trip ───────────────────────────────────────

  it("publishPricing stores and getActivePricing retrieves through store", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    const config: PricingConfigData = {
      models: { custom: "input_tokens * 0.5" },
      minBalance: 3,
    };
    const id = await store.setActivePricing(config);
    expect(id).toBeTruthy();

    const result = await store.getActivePricing();
    expect(result).not.toBeNull();
    expect(result!.config.models["custom"]).toBe("input_tokens * 0.5");
  });

  it("publishPricingFromDict → loadPricingFromStore round-trips engine", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    const manager = new CreditManager(store);

    await manager.publishPricingFromDict(TEST_PRICING);
    await manager.addCredits(PG_USER, 100);
    await manager.deduct(PG_USER, METRICS, "rtx_1");

    const m2 = new CreditManager(store);
    await m2.loadPricingFromStore();
    await m2.addCredits(PG_USER, 50);
    const result = await m2.deduct(
      PG_USER,
      { model: "gpt-4", inputTokens: 200, outputTokens: 0 },
      "rtx_2",
    );
    expect(result.amount).toBe(-2); // 200 * 0.01 = 2

    const balance = await m2.getBalance(PG_USER);
    expect(balance.balance).toBe(100 - EXPECTED_COST + 50 - 2);
  });

  // ── Plan allowance ──────────────────────────────────────────────────

  it("plan allowance covers full cost, skips balance deduct", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    await pool.query(
      `INSERT INTO public.credit_plans (id, name, free_allowance) VALUES ($1, 'Free', 100)`,
      [PLAN_UUID],
    );

    await store.addCredits(PG_USER, 10, "adjustment");
    const planResult = await store.setUserPlan(PG_USER, PLAN_UUID);
    expect(planResult.planId).toBe(PLAN_UUID);

    const userPlan = await store.getUserPlan(PG_USER);
    expect(userPlan.planId).toBe(PLAN_UUID);

    const allowanceBefore = await store.checkAllowance(PG_USER);
    expect(allowanceBefore.allowanceRemaining).toBe(100);

    const config: PricingConfigData = {
      models: { _default: "input_tokens * 1" },
      plans: { free: { id: PLAN_UUID, name: "Free", freeAllowance: 100 } },
    };
    const manager = new CreditManager(store);
    await manager.publishPricingFromDict(config);

    const result = await manager.deduct(PG_USER, { inputTokens: 5 }, "plan-ded-1");
    expect(result.amount).toBe(0);
    expect(result.transactionId).toBe("");

    const balance = await manager.getBalance(PG_USER);
    expect(balance.balance).toBe(10);

    const allowance = await store.checkAllowance(PG_USER);
    expect(allowance.allowanceRemaining).toBe(95);
  });

  it("plan allowance partially covers, deducts remainder from balance", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    await pool.query(
      `INSERT INTO public.credit_plans (id, name, free_allowance) VALUES ($1, 'Starter', 10)`,
      [PLAN_UUID],
    );

    await store.addCredits(PG_USER, 100, "adjustment");
    const planResult = await store.setUserPlan(PG_USER, PLAN_UUID);
    expect(planResult.planId).toBe(PLAN_UUID);

    const allowanceBefore = await store.checkAllowance(PG_USER);
    expect(allowanceBefore.allowanceRemaining).toBe(10);

    const config: PricingConfigData = {
      models: { _default: "input_tokens * 1" },
      plans: { starter: { id: PLAN_UUID, name: "Starter", freeAllowance: 10 } },
    };
    const manager = new CreditManager(store);
    await manager.publishPricingFromDict(config);

    const result = await manager.deduct(PG_USER, { inputTokens: 25 }, "plan-ded-2");
    expect(result.amount).toBe(-15);
    expect(result.transactionId).toBeTruthy();

    const balance = await manager.getBalance(PG_USER);
    expect(balance.balance).toBe(85);

    const allowance = await store.checkAllowance(PG_USER);
    expect(allowance.allowanceRemaining).toBe(0);
  });

  // ── Refunds ─────────────────────────────────────────────────────────

  it("refunds a full deduction and restores balance", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    const manager = new CreditManager(store);
    await manager.publishPricingFromDict(TEST_PRICING);
    await manager.addCredits(PG_USER, 100);

    const deduct = await manager.deduct(PG_USER, METRICS, "refund-tx-1");
    expect(deduct.amount).toBe(-EXPECTED_COST);

    const refund = await manager.refundCredits(deduct.transactionId);
    expect(refund.error).toBeUndefined();
    expect(refund.amount).toBe(EXPECTED_COST);

    const balance = await manager.getBalance(PG_USER);
    expect(balance.balance).toBe(100);
  });

  it("partial refund restores partial amount", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    const manager = new CreditManager(store);
    await manager.publishPricingFromDict(TEST_PRICING);
    await manager.addCredits(PG_USER, 100);

    const deduct = await manager.deduct(PG_USER, METRICS, "refund-tx-2");
    const refund = await manager.refundCredits(deduct.transactionId, 1);
    expect(refund.error).toBeUndefined();
    expect(refund.amount).toBe(1);

    const balance = await manager.getBalance(PG_USER);
    expect(balance.balance).toBe(100 - EXPECTED_COST + 1);
  });

  it("double refund returns error", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    const manager = new CreditManager(store);
    await manager.publishPricingFromDict(TEST_PRICING);
    await manager.addCredits(PG_USER, 100);

    const deduct = await manager.deduct(PG_USER, METRICS, "refund-tx-3");
    const first = await manager.refundCredits(deduct.transactionId);
    expect(first.error).toBeUndefined();

    const second = await manager.refundCredits(deduct.transactionId);
    expect(second.error).toBe("already_refunded");
  });

  it("refund of unknown transaction returns error", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    const manager = new CreditManager(store);
    await manager.publishPricingFromDict(TEST_PRICING);

    const result = await manager.refundCredits("00000000-0000-0000-0000-000000000999");
    expect(result.error).toBe("transaction_not_found");
  });

  // ── Credit expiry ───────────────────────────────────────────────────

  it("credits with 1s TTL expire on sweep", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    const manager = new CreditManager(store);
    await manager.publishPricingFromDict(TEST_PRICING);

    await manager.addCredits(PG_USER, 100, "purchase", null, new Date(Date.now() + 1));
    await new Promise((r) => setTimeout(r, 50));

    const result = await manager.sweepExpiredCredits();
    expect(result.expiredCount).toBe(1);
    expect(result.expiredAmount).toBe(100);

    const balance = await manager.getBalance(PG_USER);
    expect(balance.balance).toBe(0);
  });

  it("dryRun reports without modifying balance", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    const manager = new CreditManager(store);
    await manager.publishPricingFromDict(TEST_PRICING);

    await manager.addCredits(PG_USER, 100, "purchase", null, new Date(Date.now() + 1));
    await new Promise((r) => setTimeout(r, 50));

    const result = await manager.sweepExpiredCredits(true);
    expect(result.expiredCount).toBe(1);
    expect(result.dryRun).toBe(true);

    const balance = await manager.getBalance(PG_USER);
    expect(balance.balance).toBe(100);
  });

  it("credits without expiry never expire", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    const manager = new CreditManager(store);
    await manager.publishPricingFromDict(TEST_PRICING);
    await manager.addCredits(PG_USER, 100);

    const result = await manager.sweepExpiredCredits();
    expect(result.expiredCount).toBe(0);
    expect(result.expiredAmount).toBe(0);
  });

  // ── Team pools ──────────────────────────────────────────────────────

  it("create team with initial balance", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    const team = await store.createTeam("Dev Team", 500);
    expect(team.teamId).toBeTruthy();

    const balance = await store.getTeamBalance(team.teamId);
    expect(balance.balance).toBe(500);
    expect(balance.name).toBe("Dev Team");
  });

  it("add team member and deduct from team pool", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    const manager = new CreditManager(store);
    await manager.publishPricingFromDict({ models: { _default: "input_tokens * 1" } });

    await manager.addCredits(PG_USER, 10);
    const team = await store.createTeam("Pool", 500);
    await store.addTeamMember(team.teamId, PG_USER, "member");

    const result = await manager.deductTeam(team.teamId, PG_USER, { inputTokens: 150 });
    expect(result.amount).toBe(-150);
    expect(result.teamBalanceAfter).toBe(350);
    expect(result.transactionId).toBeTruthy();
  });

  it("deductTeam insufficient balance returns error", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    const manager = new CreditManager(store);
    await manager.publishPricingFromDict({ models: { _default: "input_tokens * 1" } });

    await manager.addCredits(PG_USER, 10);
    const team = await store.createTeam("Poor Pool", 10);
    await store.addTeamMember(team.teamId, PG_USER, "member");

    const result = await manager.deductTeam(team.teamId, PG_USER, { inputTokens: 100 });
    expect(result.error).toBe("insufficient_team_balance");
  });

  it("deductTeam user not in team returns error", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    const manager = new CreditManager(store);
    await manager.publishPricingFromDict({ models: { _default: "input_tokens * 1" } });

    const team = await store.createTeam("Closed Team", 500);
    const result = await manager.deductTeam(team.teamId, PG_USER, { inputTokens: 10 });
    expect(result.error).toBe("user_not_in_team");
  });

  // ── Spend caps ──────────────────────────────────────────────────────

  it("daily deny cap blocks deduction", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    const manager = new CreditManager(store);
    await manager.publishPricingFromDict({ models: { _default: "input_tokens * 1" } });
    await manager.addCredits(PG_USER, 100);

    await pool.query(
      `INSERT INTO public.credit_spend_caps (user_id, cap_type, cap_limit, action) VALUES ($1, 'daily', 10, 'deny')`,
      [PG_USER],
    );

    await expect(() => manager.deduct(PG_USER, { inputTokens: 11 }, "cap-test-1")).rejects.toThrow(
      "Spend cap exceeded",
    );
  });

  it("spend cap allows deduction within limit", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    const manager = new CreditManager(store);
    await manager.publishPricingFromDict({ models: { _default: "input_tokens * 1" } });
    await manager.addCredits(PG_USER, 100);

    await pool.query(
      `INSERT INTO public.credit_spend_caps (user_id, cap_type, cap_limit, action) VALUES ($1, 'daily', 100, 'deny')`,
      [PG_USER],
    );

    const result = await manager.deduct(PG_USER, { inputTokens: 5 }, "cap-test-2");
    expect(result.transactionId).toBeTruthy();
  });

  // ── Usage analytics ─────────────────────────────────────────────────

  it("spendByUser returns correct totals", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    const manager = new CreditManager(store);
    await manager.publishPricingFromDict(TEST_PRICING);
    await manager.addCredits(PG_USER, 500);
    await manager.addCredits(PG_USER2, 500);
    await manager.deduct(PG_USER, METRICS, "analytics-1");
    await manager.deduct(
      PG_USER2,
      { model: "gpt-4", inputTokens: 200, outputTokens: 0 },
      "analytics-2",
    );

    const now = new Date();
    const rows = await manager.spendByUser(
      new Date(now.getTime() - 1000),
      new Date(now.getTime() + 1000),
    );
    const row1 = rows.find((r) => r.userId === PG_USER);
    expect(row1).toBeDefined();
    expect(row1!.totalSpend).toBe(EXPECTED_COST);
    expect(row1!.transactionCount).toBe(1);

    const row2 = rows.find((r) => r.userId === PG_USER2);
    expect(row2).toBeDefined();
    expect(row2!.totalSpend).toBe(2); // 200 * 0.01 = 2
  });

  it("spendByModel returns model breakdown", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    const manager = new CreditManager(store);
    await manager.publishPricingFromDict(TEST_PRICING);
    await manager.addCredits(PG_USER, 500);
    await manager.deduct(PG_USER, METRICS, "analytics-3");

    const now = new Date();
    const rows = await manager.spendByModel(
      new Date(now.getTime() - 1000),
      new Date(now.getTime() + 1000),
    );
    const row = rows.find((r) => r.model === "gpt-4");
    expect(row).toBeDefined();
    expect(row!.totalSpend).toBeGreaterThanOrEqual(EXPECTED_COST);
  });

  it("topUsers returns top spenders", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    const manager = new CreditManager(store);
    await manager.publishPricingFromDict(TEST_PRICING);
    await manager.addCredits(PG_USER, 500);
    await manager.addCredits(PG_USER2, 500);
    await manager.deduct(PG_USER, METRICS, "analytics-4");
    await manager.deduct(
      PG_USER2,
      { model: "gpt-4", inputTokens: 300, outputTokens: 0 },
      "analytics-5",
    );

    const now = new Date();
    const rows = await manager.topUsers(
      2,
      new Date(now.getTime() - 1000),
      new Date(now.getTime() + 1000),
    );
    expect(rows).toHaveLength(2);
    expect(rows[0].totalSpend).toBeGreaterThanOrEqual(rows[1].totalSpend);
  });

  it("dailySpend returns bucketed results", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    const manager = new CreditManager(store);
    await manager.publishPricingFromDict(TEST_PRICING);
    await manager.addCredits(PG_USER, 500);
    await manager.deduct(PG_USER, METRICS, "analytics-6");

    const now = new Date();
    const rows = await manager.dailySpend(
      new Date(now.getTime() - 86400000),
      new Date(now.getTime() + 86400000),
    );
    expect(rows.length).toBeGreaterThanOrEqual(1);
    expect(rows[0].totalSpend).toBeGreaterThan(0);
  });

  it("aggregateStats returns aggregate data", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    const manager = new CreditManager(store);
    await manager.publishPricingFromDict(TEST_PRICING);
    await manager.addCredits(PG_USER, 500);
    await manager.deduct(PG_USER, METRICS, "analytics-7");

    const now = new Date();
    const stats = await manager.aggregateStats(
      new Date(now.getTime() - 1000),
      new Date(now.getTime() + 1000),
    );
    expect(stats.totalCreditsConsumed).toBeGreaterThan(0);
    expect(stats.activeUsers).toBeGreaterThanOrEqual(1);
    expect(stats.topModel).toBeTruthy();
  });

  it("analytics queries return empty results for empty window", async () => {
    const store = new PostgresStore(DATABASE_URL!, pg.Pool);
    const empty = new Date("2020-01-01");
    const emptyEnd = new Date("2020-01-02");

    const stats = await store.aggregateStats(empty, emptyEnd);
    expect(stats.totalCreditsConsumed).toBe(0);
    expect(stats.activeUsers).toBe(0);
    expect(stats.topModel).toBe("");

    const byUser = await store.spendByUser(empty, emptyEnd);
    expect(byUser).toHaveLength(0);

    const byModel = await store.spendByModel(empty, emptyEnd);
    expect(byModel).toHaveLength(0);

    const top = await store.topUsers(5, empty, emptyEnd);
    expect(top).toHaveLength(0);

    const daily = await store.dailySpend(empty, emptyEnd);
    expect(daily).toHaveLength(0);
  });
});
