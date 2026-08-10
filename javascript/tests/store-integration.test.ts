import { afterAll, beforeAll, describe, expect, inject, it } from "vitest";
import { Decimal } from "decimal.js";
import pg from "pg";
import { CreditsService } from "../src/credits/service.js";
import {
  FeatureNotEntitledError,
  OperationNotAllowedError,
  QuotaExceededError,
  StoreError,
} from "../src/errors.js";
import { PostgresStore } from "../src/credits/postgres/store.js";
import type { BursarConfigData } from "../src/config.js";
import { TEST_TENANT_ID, applyMigrations, truncateBursarTables } from "./helpers/bootstrap.js";

// Global setup owns normalization and the Testcontainers fallback. Reading the
// raw environment here would let DATABASE_URL="" shadow the provided URL and
// silently skip this required suite.
const DATABASE_URL = inject("DATABASE_URL");
const USER_ID = "00000000-0000-0000-0000-000000000902";
const REPLAY_USER_ID = "00000000-0000-0000-0000-000000000912";
const TEAM_REPLAY_OWNER_ID = "00000000-0000-0000-0000-000000000922";
const TEAM_CONCURRENT_OWNER_ID = "00000000-0000-0000-0000-000000000923";
const TEAM_CHANGED_OWNER_ID = "00000000-0000-0000-0000-000000000924";

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

const CONCURRENCY_CONFIG = {
  version: 1,
  catalog: { default_plan: "max_two" },
  pricing: CONFIG.pricing,
  credits: CONFIG.credits,
  admission: {
    policies: {
      max_two: { max_in_flight: 2 },
      headroom: { max_in_flight: 10 },
    },
  },
  plans: {
    max_two: {
      display_name: "Max two",
      rank: 0,
      rate_card: "standard",
      allowed_operations: ["completion"],
      admission_policy: "max_two",
    },
    headroom: {
      display_name: "Headroom",
      rank: 1,
      rate_card: "standard",
      allowed_operations: ["completion"],
      admission_policy: "headroom",
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

  async function runWithConcurrentStores<T>(
    workerCount: number,
    operation: (workerStore: PostgresStore, workerIndex: number) => Promise<T>,
  ): Promise<T[]> {
    const workerStores = Array.from(
      { length: workerCount },
      () =>
        new PostgresStore({
          postgres: DATABASE_URL!,
          tenantId: TEST_TENANT_ID,
          providerEnvironment: "test",
          maxConnections: 1,
          applicationName: "bursar-js-concurrency-test",
        }),
    );
    try {
      return await Promise.all(
        workerStores.map((workerStore, workerIndex) => operation(workerStore, workerIndex)),
      );
    } finally {
      await Promise.all(workerStores.map((workerStore) => workerStore.close()));
    }
  }

  async function financialSnapshot(userId: string) {
    const result = await pool.query<{
      balance: string;
      ledger_total: string;
      usage_entries: number;
      usage_charges: number;
      usage_keys: number;
    }>(
      `SELECT
         account.balance,
         COALESCE((
           SELECT sum(entry.amount)
           FROM bursar.credit_ledger_entries AS entry
           WHERE entry.account_id = account.id
         ), 0) AS ledger_total,
         (
           SELECT count(*)::int
           FROM bursar.credit_ledger_entries AS entry
           WHERE entry.account_id = account.id
             AND entry.kind = 'usage'
         ) AS usage_entries,
         (
           SELECT count(*)::int
           FROM bursar.credit_usage_charges AS charge
           WHERE charge.account_id = account.id
         ) AS usage_charges,
         (
           SELECT count(DISTINCT charge.idempotency_key)::int
           FROM bursar.credit_usage_charges AS charge
           WHERE charge.account_id = account.id
         ) AS usage_keys
       FROM bursar.credit_accounts AS account
       WHERE account.tenant_id = $1::uuid
         AND account.subject_id = $2::uuid
         AND account.account_kind = 'personal'`,
      [TEST_TENANT_ID, userId],
    );
    const row = result.rows[0];
    if (row === undefined) throw new Error(`missing credit account for ${userId}`);
    return row;
  }

  async function activeLeaseSnapshot(userId: string) {
    const result = await pool.query<{
      balance: string;
      active_count: number;
      reserved_total: string;
    }>(
      `SELECT
         account.balance,
         count(lease.id) FILTER (
           WHERE lease.status = 'active' AND lease.expires_at > now()
         )::int AS active_count,
         COALESCE(sum(lease.reserved_amount) FILTER (
           WHERE lease.status = 'active' AND lease.expires_at > now()
         ), 0) AS reserved_total
       FROM bursar.credit_accounts AS account
       LEFT JOIN bursar.credit_leases AS lease
         ON lease.account_id = account.id
       WHERE account.tenant_id = $1::uuid
         AND account.subject_id = $2::uuid
         AND account.account_kind = 'personal'
       GROUP BY account.id, account.balance`,
      [TEST_TENANT_ID, userId],
    );
    const row = result.rows[0];
    if (row === undefined) throw new Error(`missing credit account for ${userId}`);
    return row;
  }

  async function teamCreationSnapshot(idempotencyKey: string) {
    const result = await pool.query<{
      team_id: string;
      name: string;
      account_id: string;
      balance: string;
      team_count: number;
      member_count: number;
      initial_grant_count: number;
      tenant_subject_count: number;
      tenant_team_count: number;
      tenant_member_count: number;
      tenant_account_count: number;
      tenant_ledger_count: number;
    }>(
      `SELECT
         team.id::text AS team_id,
         team.name,
         account.id::text AS account_id,
         account.balance::text AS balance,
         (
           SELECT count(*)::int
           FROM bursar.credit_teams AS matching_team
           WHERE matching_team.tenant_id = $1::uuid
             AND matching_team.creation_idempotency_key = $2
         ) AS team_count,
         (
           SELECT count(*)::int
           FROM bursar.credit_team_members AS member
           WHERE member.team_id = team.id
         ) AS member_count,
         (
           SELECT count(*)::int
           FROM bursar.credit_ledger_entries AS entry
           WHERE entry.account_id = account.id
             AND entry.kind = 'grant'
             AND entry.operation = 'team_initial_grant'
         ) AS initial_grant_count,
         (
           SELECT count(*)::int
           FROM bursar.subjects AS subject
           WHERE subject.tenant_id = $1::uuid
         ) AS tenant_subject_count,
         (
           SELECT count(*)::int
           FROM bursar.credit_teams AS tenant_team
           WHERE tenant_team.tenant_id = $1::uuid
         ) AS tenant_team_count,
         (
           SELECT count(*)::int
           FROM bursar.credit_team_members AS tenant_member
           WHERE tenant_member.tenant_id = $1::uuid
         ) AS tenant_member_count,
         (
           SELECT count(*)::int
           FROM bursar.credit_accounts AS tenant_account
           WHERE tenant_account.tenant_id = $1::uuid
         ) AS tenant_account_count,
         (
           SELECT count(*)::int
           FROM bursar.credit_ledger_entries AS tenant_entry
           WHERE tenant_entry.tenant_id = $1::uuid
         ) AS tenant_ledger_count
       FROM bursar.credit_teams AS team
       JOIN bursar.credit_accounts AS account
         ON account.subject_id = team.subject_id
        AND account.account_kind = 'team'
       WHERE team.tenant_id = $1::uuid
         AND team.creation_idempotency_key = $2`,
      [TEST_TENANT_ID, idempotencyKey],
    );
    const row = result.rows[0];
    if (row === undefined) throw new Error(`missing team for ${idempotencyKey}`);
    return row;
  }

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
    expect(aggregate.totalCreditsConsumed.toString()).toBe("17");
    expect(aggregate.activeUsers).toBe(2);

    const team = await store.createTeam(USER_ID, "SDK integration team", {
      idempotencyKey: "team:create:sdk-integration",
      initialBalance: new Decimal(10),
    });
    expect(team.idempotent).toBe(false);
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

  it("prevents concurrent unique-key deductions from double-spending", async () => {
    const userId = "00000000-0000-0000-0000-000000000925";
    const service = new CreditsService(store);
    await service.publishAndActivateCatalog(structuredClone(CONCURRENCY_CONFIG));
    await service.addCredits(userId, new Decimal(5), {
      type: "purchase",
      idempotencyKey: "concurrent-double-spend-funding",
    });

    const results = await runWithConcurrentStores(12, (workerStore, workerIndex) =>
      workerStore.deductWithAllowance(userId, new Decimal(1), {
        operation: "completion",
        idempotencyKey: `concurrent-double-spend:${workerIndex}`,
      }),
    );
    const successes = results.filter((result) => result.error === null);
    const failures = results.filter((result) => result.error !== null);

    expect(successes).toHaveLength(5);
    expect(failures).toHaveLength(7);
    expect(new Set(failures.map((result) => result.error))).toEqual(
      new Set(["insufficient_credits"]),
    );
    expect(new Set(successes.map((result) => result.entryId)).size).toBe(5);
    expect(new Set(successes.map((result) => result.balanceAfter?.toString()))).toEqual(
      new Set(["0", "1", "2", "3", "4"]),
    );

    const snapshot = await financialSnapshot(userId);
    expect(new Decimal(snapshot.balance).toString()).toBe("0");
    expect(new Decimal(snapshot.ledger_total).equals(snapshot.balance)).toBe(true);
    expect(snapshot.usage_entries).toBe(5);
    expect(snapshot.usage_charges).toBe(5);
    expect(snapshot.usage_keys).toBe(5);
  }, 60_000);

  it("replays one logical debit under concurrent same-key deductions", async () => {
    const userId = "00000000-0000-0000-0000-000000000926";
    const service = new CreditsService(store);
    await service.publishAndActivateCatalog(structuredClone(CONCURRENCY_CONFIG));
    await service.addCredits(userId, new Decimal(10), {
      type: "purchase",
      idempotencyKey: "concurrent-replay-funding",
    });

    const results = await runWithConcurrentStores(12, (workerStore) =>
      workerStore.deductWithAllowance(userId, new Decimal(2), {
        operation: "completion",
        idempotencyKey: "concurrent-replay-one-debit",
      }),
    );

    expect(results.every((result) => result.error === null)).toBe(true);
    expect(new Set(results.map((result) => result.entryId)).size).toBe(1);
    expect(new Set(results.map((result) => result.usageChargeId)).size).toBe(1);
    expect(results.filter((result) => !result.idempotent)).toHaveLength(1);
    expect(results.filter((result) => result.idempotent)).toHaveLength(11);
    expect(new Set(results.map((result) => result.balanceAfter?.toString()))).toEqual(
      new Set(["8"]),
    );

    const snapshot = await financialSnapshot(userId);
    expect(new Decimal(snapshot.balance).toString()).toBe("8");
    expect(new Decimal(snapshot.ledger_total).equals(snapshot.balance)).toBe(true);
    expect(snapshot.usage_entries).toBe(1);
    expect(snapshot.usage_charges).toBe(1);
    expect(snapshot.usage_keys).toBe(1);
  }, 60_000);

  it("enforces maxConcurrent and headroom under concurrent lease admission", async () => {
    const maxConcurrentUser = "00000000-0000-0000-0000-000000000927";
    const headroomUser = "00000000-0000-0000-0000-000000000928";
    const service = new CreditsService(store);
    await service.publishAndActivateCatalog(structuredClone(CONCURRENCY_CONFIG));
    await service.addCredits(maxConcurrentUser, new Decimal(100), {
      type: "purchase",
      idempotencyKey: "concurrent-lease-limit-funding",
    });
    await service.addCredits(headroomUser, new Decimal(5), {
      type: "purchase",
      idempotencyKey: "concurrent-lease-headroom-funding",
    });
    await service.setUserPlan(headroomUser, "headroom");

    const limitedResults = await runWithConcurrentStores(12, (workerStore, workerIndex) =>
      workerStore.createLease(maxConcurrentUser, new Decimal(1), "completion", {
        idempotencyKey: `concurrent-lease-limit:${workerIndex}`,
        floor: new Decimal(0),
        maxConcurrent: 2,
        ttlSeconds: 60,
      }),
    );
    const limitedSuccesses = limitedResults.filter((result) => result.error === null);
    const limitedFailures = limitedResults.filter((result) => result.error !== null);
    expect(limitedSuccesses).toHaveLength(2);
    expect(new Set(limitedSuccesses.map((result) => result.leaseId)).size).toBe(2);
    expect(limitedFailures).toHaveLength(10);
    expect(new Set(limitedFailures.map((result) => result.error))).toEqual(
      new Set(["max_concurrent_reached"]),
    );

    const headroomResults = await runWithConcurrentStores(12, (workerStore, workerIndex) =>
      workerStore.createLease(headroomUser, new Decimal(2), "completion", {
        idempotencyKey: `concurrent-lease-headroom:${workerIndex}`,
        floor: new Decimal(0),
        maxConcurrent: 10,
        ttlSeconds: 60,
      }),
    );
    const headroomSuccesses = headroomResults.filter((result) => result.error === null);
    const headroomFailures = headroomResults.filter((result) => result.error !== null);
    expect(headroomSuccesses).toHaveLength(2);
    expect(new Set(headroomSuccesses.map((result) => result.leaseId)).size).toBe(2);
    expect(headroomFailures).toHaveLength(10);
    expect(new Set(headroomFailures.map((result) => result.error))).toEqual(
      new Set(["insufficient_headroom"]),
    );

    const limitedSnapshot = await activeLeaseSnapshot(maxConcurrentUser);
    expect(new Decimal(limitedSnapshot.balance).toString()).toBe("100");
    expect(limitedSnapshot.active_count).toBe(2);
    expect(new Decimal(limitedSnapshot.reserved_total).toString()).toBe("2");
    const headroomSnapshot = await activeLeaseSnapshot(headroomUser);
    expect(new Decimal(headroomSnapshot.balance).toString()).toBe("5");
    expect(headroomSnapshot.active_count).toBe(2);
    expect(new Decimal(headroomSnapshot.reserved_total).toString()).toBe("4");

    const limitedAvailability = await store.getAvailable(maxConcurrentUser);
    expect(limitedAvailability.available.toString()).toBe("98");
    expect(limitedAvailability.reserved.toString()).toBe("2");
    const headroomAvailability = await store.getAvailable(headroomUser);
    expect(headroomAvailability.available.toString()).toBe("1");
    expect(headroomAvailability.reserved.toString()).toBe("4");
  }, 60_000);

  it("replays team creation as one team, account, membership, and initial grant", async () => {
    await new CreditsService(store).publishAndActivateCatalog(CONFIG);
    const idempotencyKey = "team:create:replay";

    const first = await store.createTeam(TEAM_REPLAY_OWNER_ID, "Replay-safe team", {
      idempotencyKey,
      initialBalance: new Decimal("9.000"),
    });
    const beforeReplay = await teamCreationSnapshot(idempotencyKey);
    const replay = await store.createTeam(TEAM_REPLAY_OWNER_ID, "Replay-safe team", {
      idempotencyKey,
      initialBalance: new Decimal("9"),
    });

    expect(first.idempotent).toBe(false);
    expect(replay).toEqual({ ...first, idempotent: true });
    const afterReplay = await teamCreationSnapshot(idempotencyKey);
    expect(afterReplay).toEqual(beforeReplay);
    expect(afterReplay).toMatchObject({
      team_id: first.teamId,
      name: "Replay-safe team",
      balance: "9.000000",
      team_count: 1,
      member_count: 1,
      initial_grant_count: 1,
    });
  });

  it("rejects changed team-creation replays without persistent side effects", async () => {
    await new CreditsService(store).publishAndActivateCatalog(CONFIG);
    const idempotencyKey = "team:create:conflict";
    await store.createTeam(TEAM_REPLAY_OWNER_ID, "Conflict-safe team", {
      idempotencyKey,
      initialBalance: new Decimal(7),
    });
    const before = await teamCreationSnapshot(idempotencyKey);

    const changedName = store.createTeam(TEAM_REPLAY_OWNER_ID, "Changed team", {
      idempotencyKey,
      initialBalance: new Decimal(7),
    });
    await expect(changedName).rejects.toBeInstanceOf(StoreError);
    await expect(changedName).rejects.toThrow("idempotency_conflict");

    const changedBalance = store.createTeam(TEAM_REPLAY_OWNER_ID, "Conflict-safe team", {
      idempotencyKey,
      initialBalance: new Decimal(8),
    });
    await expect(changedBalance).rejects.toBeInstanceOf(StoreError);
    await expect(changedBalance).rejects.toThrow("idempotency_conflict");

    const changedOwner = store.createTeam(TEAM_CHANGED_OWNER_ID, "Conflict-safe team", {
      idempotencyKey,
      initialBalance: new Decimal(7),
    });
    await expect(changedOwner).rejects.toBeInstanceOf(StoreError);
    await expect(changedOwner).rejects.toThrow("idempotency_conflict");

    expect(await teamCreationSnapshot(idempotencyKey)).toEqual(before);
  });

  it("creates one logical team under concurrent same-key requests", async () => {
    await new CreditsService(store).publishAndActivateCatalog(CONFIG);
    const idempotencyKey = "team:create:concurrent";
    const workerCount = 12;
    let waiting = 0;
    let releaseStart = () => {};
    const start = new Promise<void>((resolve) => {
      releaseStart = resolve;
    });

    const results = await runWithConcurrentStores(workerCount, async (workerStore) => {
      waiting += 1;
      if (waiting === workerCount) releaseStart();
      await start;
      return workerStore.createTeam(TEAM_CONCURRENT_OWNER_ID, "Concurrent team", {
        idempotencyKey,
        initialBalance: new Decimal(5),
      });
    });

    expect(new Set(results.map((result) => result.teamId)).size).toBe(1);
    expect(results.filter((result) => !result.idempotent)).toHaveLength(1);
    expect(results.filter((result) => result.idempotent)).toHaveLength(workerCount - 1);
    await expect(teamCreationSnapshot(idempotencyKey)).resolves.toMatchObject({
      team_id: results[0]?.teamId,
      name: "Concurrent team",
      balance: "5.000000",
      team_count: 1,
      member_count: 1,
      initial_grant_count: 1,
    });
  }, 60_000);
});
