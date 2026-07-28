import { afterAll, beforeAll, describe, expect, inject, it } from "vitest";
import Decimal from "decimal.js";
import pg from "pg";
import { CreditsService } from "../src/credits/service.js";
import { FeatureNotEntitledError, QuotaExceededError } from "../src/errors.js";
import { PostgresStore } from "../src/credits/postgres/store.js";
import { BOOTSTRAP_SQL, applyMigrations } from "./helpers/bootstrap.js";

const DATABASE_URL = process.env.DATABASE_URL ?? inject("DATABASE_URL");
const USER_ID = "00000000-0000-0000-0000-000000000902";
const REPLAY_USER_ID = "00000000-0000-0000-0000-000000000912";

const CONFIG = {
  version: 1,
  pricing: {
    operations: {
      completion: {
        measures: {
          input_tokens: { unit: "token" },
          output_tokens: { unit: "token" },
        },
        dimensions: { model: { type: "string", required: false } },
      },
    },
    rate_cards: {
      standard: {
        operations: {
          completion: {
            rules: [
              {
                when: { model: { op: "prefix", value: "premium-" } },
                charge: {
                  type: "expression",
                  formula: "input_tokens * 2 + output_tokens * 3",
                },
              },
            ],
            unmatched: {
              action: "charge",
              charge: {
                type: "expression",
                formula: "input_tokens + output_tokens",
              },
            },
          },
        },
      },
    },
  },
  credits: {
    accounting: { unit: "credit", scale: 6, rounding: "half_up" },
    buckets: {
      grant: {
        priority: 10,
        expiry: {
          type: "after_grant",
          interval: { unit: "day", count: 7 },
          timezone: "UTC",
        },
      },
      purchased: {
        priority: 20,
        expiry: { type: "never" },
      },
    },
    default_bucket: "purchased",
  },
  entitlements: {
    features: {
      premium_tools: { type: "boolean", default: false },
    },
  },
  plans: {
    pro: {
      display_name: "Pro",
      rate_card: "standard",
      allowed_operations: ["completion"],
      features: { premium_tools: true },
      quotas: {
        input_budget: {
          operation: "completion",
          measure: "input_tokens",
          limit: "3",
          window: {
            type: "calendar",
            unit: "month",
            count: 1,
            timezone: "UTC",
          },
          enforcement: "block",
          emit_at_percent: [50, 100],
        },
      },
    },
    basic: {
      display_name: "Basic",
      rate_card: "standard",
      allowed_operations: ["completion"],
    },
  },
};

describe.runIf(DATABASE_URL)("PostgresStore integration — public configuration", () => {
  const pool = new pg.Pool({ connectionString: DATABASE_URL!, max: 2 });
  const store = new PostgresStore(DATABASE_URL!, pool);

  beforeAll(async () => {
    await pool.query(BOOTSTRAP_SQL);
    await applyMigrations(pool);
    await pool.query("INSERT INTO auth.users (id) VALUES ($1) ON CONFLICT DO NOTHING", [USER_ID]);
    await pool.query('INSERT INTO public."user" (id) VALUES ($1) ON CONFLICT DO NOTHING', [
      USER_ID,
    ]);
  }, 60_000);
  afterAll(async () => pool.end());

  it("publishes the public config, charges a generic operation, and preserves bucket order", async () => {
    const startedAt = new Date(Date.now() - 1_000);
    const service = new CreditsService(store);
    await service.publishPricingFromDict(CONFIG);
    await service.setUserPlan(USER_ID, "pro");
    await service.setUserPlan(REPLAY_USER_ID, "pro");
    const first = await service.addCredits(REPLAY_USER_ID, new Decimal(25), {
      type: "purchase",
      idempotencyKey: "integration:add-replay",
    });
    const replay = await service.addCredits(REPLAY_USER_ID, new Decimal(25), {
      type: "purchase",
      idempotencyKey: "integration:add-replay",
    });
    expect(replay.entryId).toBe(first.entryId);
    expect((await service.getBalance(REPLAY_USER_ID)).balance.toString()).toBe("25");
    expect((await service.getBalance(REPLAY_USER_ID)).lifetimePurchased.toString()).toBe("25");

    await service.addCredits(USER_ID, new Decimal(10), {
      type: "purchase",
      bucket: "grant",
      idempotencyKey: "store-integration-grant",
    });
    await service.addCredits(USER_ID, new Decimal(10), {
      type: "purchase",
      bucket: "purchased",
      idempotencyKey: "store-integration-purchased",
    });

    const result = await service.deduct(
      USER_ID,
      {
        operation: "completion",
        measures: { input_tokens: 2, output_tokens: 4 },
        dimensions: { model: "premium-x" },
      },
      "public-config-charge-1",
      undefined,
      "premium_tools",
    );
    expect(result.amount.toString()).toBe("16");
    const persistedUsage = await pool.query(
      `SELECT measures, dimensions
       FROM bursar.credit_usage_charges
       WHERE ledger_entry_id = $1::uuid`,
      [result.entryId],
    );
    expect(persistedUsage.rows[0]?.measures).toEqual({
      input_tokens: 2,
      output_tokens: 4,
    });
    expect(persistedUsage.rows[0]?.dimensions).toMatchObject({
      model: "premium-x",
    });
    await expect(
      service.deduct(
        USER_ID,
        {
          operation: "completion",
          measures: { input_tokens: 2, output_tokens: 0 },
          dimensions: { model: "premium-x" },
        },
        "public-config-charge-quota-block",
      ),
    ).rejects.toBeInstanceOf(QuotaExceededError);
    await expect(
      service.deduct(
        USER_ID,
        {
          operation: "completion",
          measures: { input_tokens: 0, output_tokens: 0 },
          dimensions: { model: "premium-x" },
        },
        "public-config-charge-entitlement-block",
        undefined,
        "missing_feature",
      ),
    ).rejects.toBeInstanceOf(FeatureNotEntitledError);
    const quotaState = await service.getQuotaState(USER_ID, "input_budget");
    expect(quotaState).toHaveLength(1);
    expect(quotaState[0]?.consumed.toString()).toBe("2");
    expect(quotaState[0]?.remaining.toString()).toBe("1");
    const quotaEvents = await service.listQuotaEvents(USER_ID);
    expect(
      quotaEvents.some((event) => event.eventType === "threshold" && event.thresholdPercent === 50),
    ).toBe(true);
    expect(quotaEvents.some((event) => event.eventType === "blocked")).toBe(true);
    const legacyLimit = await service.checkFeatureLimit(USER_ID, "input_budget");
    expect(legacyLimit).toMatchObject({
      limited: true,
      limit: 3,
      used: 2,
      remaining: 1,
      action: "deny",
    });

    const firstLease = await service.reserve(
      REPLAY_USER_ID,
      {
        operation: "completion",
        measures: { input_tokens: 2, output_tokens: 0 },
        dimensions: { model: "standard" },
      },
      { idempotencyKey: "quota-lease-1" },
    );
    const renewedLease = await service.renew(REPLAY_USER_ID, firstLease.leaseId, 1_200);
    expect(new Date(renewedLease.expiresAt).getTime()).toBeGreaterThanOrEqual(
      new Date(firstLease.expiresAt).getTime(),
    );
    await expect(
      service.reserve(
        REPLAY_USER_ID,
        {
          operation: "completion",
          measures: { input_tokens: 2, output_tokens: 0 },
          dimensions: { model: "standard" },
        },
        { idempotencyKey: "quota-lease-2" },
      ),
    ).rejects.toBeInstanceOf(QuotaExceededError);
    await service.release(REPLAY_USER_ID, firstLease.leaseId);
    const replacementLease = await service.reserve(
      REPLAY_USER_ID,
      {
        operation: "completion",
        measures: { input_tokens: 2, output_tokens: 0 },
        dimensions: { model: "standard" },
      },
      { idempotencyKey: "quota-lease-3" },
    );
    await service.setUserPlan(REPLAY_USER_ID, "basic");
    await service.settle(
      REPLAY_USER_ID,
      replacementLease.leaseId,
      {
        operation: "completion",
        measures: { input_tokens: 1, output_tokens: 0 },
        dimensions: { model: "standard" },
      },
      { idempotencyKey: "quota-lease-settle-3" },
    );

    await service.addCredits(USER_ID, new Decimal(5), {
      type: "purchase",
      bucket: "grant",
      idempotencyKey: "targeted-bucket-credit",
    });
    await service.deductCredits(USER_ID, new Decimal(5), {
      bucket: "grant",
      entryType: "adjustment",
    });
    const buckets = await service.getBucketBalances(USER_ID);
    expect(
      buckets.buckets.find((bucket) => bucket.bucketKey === "purchased")?.balance.toString(),
    ).toBe("4");
    expect(buckets.buckets.find((bucket) => bucket.bucketKey === "grant")?.balance.toString()).toBe(
      "0",
    );
    expect((await service.getBalance(USER_ID)).lifetimePurchased.toString()).toBe("25");

    const usagePage = await service.listUsageEntries(USER_ID, {
      fromDate: startedAt,
      toDate: new Date(Date.now() + 1_000),
      limit: 10,
    });
    expect(usagePage.items.map((entry) => entry.entryId)).toContain(result.entryId);
    expect(await service.getLedgerEntry(USER_ID, result.entryId)).toMatchObject({
      entryId: result.entryId,
      entryType: "usage",
    });
    const futureUsage = await service.listUsageEntries(USER_ID, {
      fromDate: new Date(Date.now() + 60_000),
      limit: 10,
    });
    expect(futureUsage.items).toHaveLength(0);

    const aggregate = await service.aggregateStats(startedAt, new Date(Date.now() + 1_000));
    expect(aggregate.totalCreditsConsumed.greaterThanOrEqualTo(17)).toBe(true);
    expect(aggregate.activeUsers).toBe(2);

    const team = await store.createTeam(USER_ID, "SDK integration team", new Decimal(10));
    await store.addTeamMember(team.teamId, REPLAY_USER_ID, "member", new Decimal(3));
    const firstTeamCharge = await store.deductTeam(
      team.teamId,
      REPLAY_USER_ID,
      new Decimal(2),
      { operation: "completion" },
      "team-charge-1",
    );
    expect(firstTeamCharge.teamBalanceAfter.toString()).toBe("8");
    const cappedTeamCharge = await store.deductTeam(
      team.teamId,
      REPLAY_USER_ID,
      new Decimal(2),
      { operation: "completion" },
      "team-charge-2",
    );
    expect(cappedTeamCharge.error).toBe("member_spend_cap_exceeded");
    expect(await store.getTeamBalance(team.teamId)).toMatchObject({
      teamId: team.teamId,
      memberCount: 2,
    });
    const members = await store.getTeamMembers(team.teamId);
    expect(members.find((member) => member.userId === REPLAY_USER_ID)?.totalSpent.toString()).toBe(
      "2",
    );

    const expiring = await service.addCredits(USER_ID, new Decimal(3), {
      type: "purchase",
      bucket: "grant",
      expiresAt: new Date(Date.now() + 60_000),
      idempotencyKey: "expiring-credit",
    });
    const setupClient = await pool.connect();
    try {
      await setupClient.query("BEGIN");
      await setupClient.query("SELECT set_config('bursar.mutation_context', 'internal', true)");
      await setupClient.query(
        `UPDATE bursar.credit_lots
         SET expires_at = now() - interval '1 second'
         WHERE source_entry_id = $1::uuid`,
        [expiring.entryId],
      );
      await setupClient.query("COMMIT");
    } catch (error) {
      await setupClient.query("ROLLBACK");
      throw error;
    } finally {
      setupClient.release();
    }
    const preview = await store.sweepExpiredCredits(true, USER_ID);
    expect(preview).toMatchObject({ expiredCount: 1, dryRun: true });
    expect(preview.expiredAmount.toString()).toBe("3");
    const swept = await store.sweepExpiredCredits(false, USER_ID);
    expect(swept).toMatchObject({ expiredCount: 1, dryRun: false });
    expect(swept.expiredByBucket?.grant.toString()).toBe("3");

    const activeBeforeDraft = await store.getActivePricing();
    const draftConfig = structuredClone(CONFIG);
    draftConfig.plans.pro.display_name = "Pro v2";
    const draftId = await service.publishPricingDraft(draftConfig, "Pro v2 draft");
    expect(draftId).not.toBe("");
    expect((await store.getActivePricing())?.version).toBe(activeBeforeDraft?.version);
    const history = await store.getPricingHistory();
    const draft = history.find((revision) => revision.id === draftId);
    expect(draft?.active).toBe(false);
    expect(history.some((revision) => revision.active)).toBe(true);
    expect((await store.getBursarConfig(draft!.version))?.config).toMatchObject({
      plans: { pro: { display_name: "Pro v2" } },
    });
    await service.activatePricing(draft!.version);
    expect((await store.getActivePricing())?.version).toBe(draft!.version);
    const migration = await service.migratePlanUsers("pro", draft!.version);
    expect(migration.targetConfigVersion).toBe(draft!.version);
    expect(migration.targetPlanId).not.toBe("");
  });
});
