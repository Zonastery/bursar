import { afterAll, beforeAll, describe, expect, inject, it } from "vitest";
import Decimal from "decimal.js";
import pg from "pg";
import { CreditsService } from "../src/credits/service.js";
import {
  FeatureNotEntitledError,
  OperationNotAllowedError,
  QuotaExceededError,
} from "../src/errors.js";
import { PostgresStore } from "../src/credits/postgres/store.js";
import type { BursarConfigData } from "../src/config.js";
import { TEST_TENANT_ID, applyMigrations, truncateBursarTables } from "./helpers/bootstrap.js";

const DATABASE_URL = process.env.DATABASE_URL ?? inject("DATABASE_URL");
const USER_ID = "00000000-0000-0000-0000-000000000902";
const REPLAY_USER_ID = "00000000-0000-0000-0000-000000000912";

const CONFIG = {
  version: 1,
  catalog: { default_plan: "pro" },
  pricing: {
    operations: {
      completion: {
        measures: {
          input_tokens: { unit: "token" },
          output_tokens: { unit: "token" },
        },
        dimensions: { model: { type: "string", required: false } },
      },
      free_export: { measures: { calls: { unit: "call" } }, dimensions: {} },
      internal_free: { measures: { calls: { unit: "call" } }, dimensions: {} },
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
          free_export: {
            rules: [],
            unmatched: { action: "charge", charge: { type: "flat", amount: "0" } },
          },
          internal_free: {
            rules: [],
            unmatched: { action: "charge", charge: { type: "flat", amount: "0" } },
          },
        },
      },
    },
  },
  credits: {
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
      rank: 1,
      rate_card: "standard",
      allowed_operations: ["completion", "free_export"],
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
      rank: 0,
      rate_card: "standard",
      allowed_operations: ["completion"],
    },
  },
} satisfies BursarConfigData;

describe.runIf(DATABASE_URL)("PostgresStore integration — public configuration", () => {
  const pool = new pg.Pool({ connectionString: DATABASE_URL!, max: 2 });
  const store = new PostgresStore({
    postgres: pool,
    tenantId: TEST_TENANT_ID,
    providerEnvironment: "test",
  });

  beforeAll(async () => {
    await applyMigrations(pool);
    await truncateBursarTables(pool);
  }, 60_000);
  afterAll(async () => pool.end());

  it("publishes the public config, charges a generic operation, and preserves bucket order", async () => {
    const startedAt = new Date(Date.now() - 1_000);
    const service = new CreditsService(store);
    await service.publishAndActivateCatalog(CONFIG);
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
      { idempotencyKey: "public-config-charge-1", feature: "premium_tools" },
    );
    expect(result.amount.toString()).toBe("16");
    const freeResult = await service.deduct(
      USER_ID,
      { operation: "free_export", measures: { calls: 1 }, dimensions: {} },
      { idempotencyKey: "public-config-free-usage" },
    );
    expect(freeResult.amount.toString()).toBe("0");
    await expect(
      service.deduct(
        USER_ID,
        { operation: "internal_free", measures: { calls: 1 }, dimensions: {} },
        { idempotencyKey: "public-config-free-not-allowed" },
      ),
    ).rejects.toBeInstanceOf(OperationNotAllowedError);
    const persistedUsage = await pool.query(
      `SELECT payload.measures, payload.dimensions
       FROM bursar.credit_usage_charges AS charge
       JOIN bursar.usage_charge_payloads AS payload
         ON payload.charge_id = charge.id
        AND payload.event_at = charge.event_at
       WHERE charge.ledger_entry_id = $1::uuid`,
      [result.entryId],
    );
    expect(persistedUsage.rows[0]?.measures).toEqual({
      input_tokens: 2,
      output_tokens: 4,
    });
    expect(persistedUsage.rows[0]?.dimensions).toMatchObject({
      model: "premium-x",
    });
    expect(result.usageChargeId).toBeTruthy();
    await expect(
      service.deduct(
        USER_ID,
        {
          operation: "completion",
          measures: { input_tokens: 2, output_tokens: 0 },
          dimensions: { model: "premium-x" },
        },
        { idempotencyKey: "public-config-charge-quota-block" },
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
        {
          idempotencyKey: "public-config-charge-entitlement-block",
          feature: "missing_feature",
        },
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
      idempotencyKey: "targeted-bucket-debit",
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
    const chargePage = await service.listUsageCharges(USER_ID, {
      fromDate: startedAt,
      toDate: new Date(Date.now() + 1_000),
      limit: 200,
    });
    expect(chargePage.items).toContainEqual(
      expect.objectContaining({
        operation: "completion",
        idempotencyKey: "public-config-charge-1",
      }),
    );
    expect(chargePage.items).toContainEqual(
      expect.objectContaining({
        operation: "free_export",
        idempotencyKey: "public-config-free-usage",
      }),
    );
    expect(
      chargePage.items
        .find((charge) => charge.idempotencyKey === "public-config-free-usage")
        ?.charged.toString(),
    ).toBe("0");
    const recordedCharge = chargePage.items.find(
      (charge) => charge.idempotencyKey === "public-config-charge-1",
    );
    expect(recordedCharge?.charged.toString()).toBe("16");
    if (result.entryId === null) throw new Error("expected a monetary ledger entry");
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
    const firstTeamCharge = await store.deductTeam(team.teamId, REPLAY_USER_ID, new Decimal(2), {
      idempotencyKey: "team-charge-1",
      metadata: { operation: "completion" },
    });
    if (firstTeamCharge.error !== null) throw new Error(firstTeamCharge.error);
    expect(firstTeamCharge.teamBalanceAfter.toString()).toBe("8");
    const cappedTeamCharge = await store.deductTeam(team.teamId, REPLAY_USER_ID, new Decimal(2), {
      idempotencyKey: "team-charge-2",
      metadata: { operation: "completion" },
    });
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
    expect(swept.expiredByBucket?.grant?.toString()).toBe("3");

    const activeBeforeDraft = await store.getActiveCatalog();
    const draftConfig = structuredClone(CONFIG);
    draftConfig.plans.pro.display_name = "Pro v2";
    const draftId = await service.publishCatalogDraft(draftConfig, "Pro v2 draft");
    expect(draftId).not.toBe("");
    expect((await store.getActiveCatalog())?.version).toBe(activeBeforeDraft?.version);
    const history = await store.getCatalogHistory();
    const draft = history.find((revision) => revision.id === draftId);
    expect(draft?.active).toBe(false);
    expect(history.some((revision) => revision.active)).toBe(true);
    expect((await store.getCatalogRevision(draft!.version))?.config).toMatchObject({
      plans: { pro: { display_name: "Pro v2" } },
    });
    const sourcePlanId = (await store.getUserPlan(USER_ID)).planId;
    expect(sourcePlanId).not.toBeNull();
    await service.activateCatalogRevision(draft!.version);
    expect((await store.getActiveCatalog())?.version).toBe(draft!.version);
    const targetPlanResult = await pool.query<{ id: string }>(
      `SELECT plan.id
       FROM bursar.catalog_plans AS plan
       JOIN bursar.catalog_revisions AS revision
         ON revision.id = plan.catalog_revision_id
       WHERE plan.tenant_id = $1::uuid
         AND plan.plan_key = 'pro'
         AND revision.revision_no = $2`,
      [TEST_TENANT_ID, draft!.version],
    );
    const targetPlanId = targetPlanResult.rows[0]?.id;
    expect(targetPlanId).toBeDefined();
    const migration = await service.startPlanMigration(sourcePlanId, targetPlanId!);
    const batch = await service.migratePlanBatch(migration.migrationId);
    expect(batch.done).toBe(true);
    expect((await store.getUserPlan(USER_ID)).planId).toBe(targetPlanId);
  });
});
