import { afterAll, beforeAll, describe, expect, inject, it } from "vitest";
import { Decimal } from "decimal.js";
import pg from "pg";
import { CreditsService } from "../src/credits/service.js";
import { CreditEventEmitter } from "../src/credits/events.js";
import {
  FeatureNotEntitledError,
  CapReachedError,
  LeaseExpiredError,
  OperationNotAllowedError,
  QuotaExceededError,
  StoreError,
  InsufficientCreditsError,
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
const REPORT_USER_ID = "00000000-0000-0000-0000-000000000942";

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

  it("maps durable usage into financial reporting views", async () => {
    const service = new CreditsService(store);
    await service.publishAndActivateCatalog(CONFIG);
    await service.setUserPlan(REPORT_USER_ID, "pro");
    await service.addCredits(REPORT_USER_ID, new Decimal(20), {
      type: "purchase",
      idempotencyKey: "reporting-funding",
    });
    const usage = await service.deduct(
      REPORT_USER_ID,
      {
        operation: "completion",
        measures: { input_tokens: 1, output_tokens: 2 },
        dimensions: { model: "reporting-model" },
      },
      { idempotencyKey: "reporting-deduction" },
    );
    expect(usage.amount.toString()).toBe("3");
    const recorded = await service.recordUsage(
      REPORT_USER_ID,
      {
        operation: "completion",
        measures: { input_tokens: 1, output_tokens: 1 },
        dimensions: { model: "external-model" },
      },
      { idempotencyKey: "reporting-external-usage" },
    );
    expect(recorded.error).toBeNull();

    const start = new Date(Date.now() - 60_000);
    const end = new Date(Date.now() + 60_000);
    await expect(store.spendByUser(start, end)).resolves.toEqual(
      expect.arrayContaining([
        expect.objectContaining({ userId: REPORT_USER_ID, entryCount: expect.any(Number) }),
      ]),
    );
    await expect(store.spendByModel(start, end)).resolves.toEqual(
      expect.arrayContaining([expect.objectContaining({ model: "reporting-model" })]),
    );
    await expect(store.topUsers(10, start, end)).resolves.toEqual(
      expect.arrayContaining([expect.objectContaining({ userId: REPORT_USER_ID })]),
    );
    await expect(store.dailySpend(start, end)).resolves.toEqual(
      expect.arrayContaining([expect.objectContaining({ entryCount: expect.any(Number) })]),
    );
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

  it("replays lease settlement and rejects changed payloads without extra accounting", async () => {
    const userId = "00000000-0000-0000-0000-000000000929";
    const service = new CreditsService(store);
    await service.publishAndActivateCatalog(structuredClone(CONFIG));
    await service.addCredits(userId, new Decimal("10"), {
      type: "purchase",
      idempotencyKey: "settlement-replay-funding",
    });

    const lease = await service.reserve(userId, new Decimal("3"), {
      idempotencyKey: "settlement-replay-reserve",
      ttl: 60,
    });
    const first = await service.settle(userId, lease.leaseId, new Decimal("2"), {
      idempotencyKey: "settlement-replay-settle",
    });
    const replay = await service.settle(userId, lease.leaseId, new Decimal("2"), {
      idempotencyKey: "settlement-replay-settle",
    });

    expect(first.error).toBeNull();
    expect(first.idempotent).toBe(false);
    expect(first.amount.toString()).toBe("2");
    expect(first.balanceAfter?.toString()).toBe("8");
    expect(replay).toMatchObject({
      entryId: first.entryId,
      usageChargeId: first.usageChargeId,
      idempotent: true,
      error: null,
    });
    expect(replay.amount.toString()).toBe("2");
    expect(replay.balanceAfter?.toString()).toBe("8");

    await expect(
      service.settle(userId, lease.leaseId, new Decimal("2.5"), {
        idempotencyKey: "settlement-replay-settle",
      }),
    ).rejects.toBeInstanceOf(StoreError);

    const snapshot = await financialSnapshot(userId);
    expect(new Decimal(snapshot.balance).toString()).toBe("8");
    expect(new Decimal(snapshot.ledger_total).toString()).toBe("8");
    expect(snapshot.usage_entries).toBe(1);
    expect(snapshot.usage_charges).toBe(1);
    expect(snapshot.usage_keys).toBe(1);

    const leaseRow = await pool.query<{
      status: string;
      reserved_amount: string;
      settled_amount: string;
    }>(
      `SELECT status, reserved_amount, settled_amount
       FROM bursar.credit_leases
       WHERE id = $1::uuid`,
      [lease.leaseId],
    );
    expect(leaseRow.rows[0]).toEqual({
      status: "settled",
      reserved_amount: "3.000000",
      settled_amount: "2.000000",
    });
  });

  it("expires a lease, releases its reservation, and permits re-admission", async () => {
    const userId = "00000000-0000-0000-0000-000000000930";
    const service = new CreditsService(store);
    await service.publishAndActivateCatalog(structuredClone(CONFIG));
    await service.addCredits(userId, new Decimal("4"), {
      type: "purchase",
      idempotencyKey: "lease-expiry-funding",
    });

    const lease = await service.reserve(userId, new Decimal("3"), {
      idempotencyKey: "lease-expiry-reserve",
      ttl: 60,
    });
    expect((await store.getAvailable(userId)).available.toString()).toBe("1");
    expect((await store.getAvailable(userId)).reserved.toString()).toBe("3");

    const expiryClient = await pool.connect();
    try {
      await expiryClient.query("BEGIN");
      await expiryClient.query("SELECT set_config('bursar.mutation_context', 'internal', true)");
      const expired = await expiryClient.query(
        `UPDATE bursar.credit_leases
         SET created_at = created_at - interval '2 minutes',
             expires_at = now() - interval '1 second'
         WHERE id = $1::uuid`,
        [lease.leaseId],
      );
      expect(expired.rowCount).toBe(1);
      await expiryClient.query("COMMIT");
    } catch (error) {
      await expiryClient.query("ROLLBACK");
      throw error;
    } finally {
      expiryClient.release();
    }
    await expect(store.expireLeases(25)).resolves.toBe(1);

    expect((await store.getAvailable(userId)).available.toString()).toBe("4");
    expect((await store.getAvailable(userId)).reserved.toString()).toBe("0");
    const expiredLease = await pool.query<{ status: string }>(
      "SELECT status FROM bursar.credit_leases WHERE id = $1::uuid",
      [lease.leaseId],
    );
    expect(expiredLease.rows[0]?.status).toBe("expired");
    await expect(
      service.settle(userId, lease.leaseId, new Decimal("2"), {
        idempotencyKey: "lease-expiry-settle",
      }),
    ).rejects.toBeInstanceOf(LeaseExpiredError);

    const snapshot = await financialSnapshot(userId);
    expect(new Decimal(snapshot.balance).toString()).toBe("4");
    expect(new Decimal(snapshot.ledger_total).toString()).toBe("4");
    expect(snapshot.usage_entries).toBe(0);
    expect(snapshot.usage_charges).toBe(0);

    const replacement = await service.reserve(userId, new Decimal("3"), {
      idempotencyKey: "lease-expiry-replacement",
      ttl: 60,
    });
    expect(replacement.error).toBeNull();
    expect(replacement.amount.toString()).toBe("3");
    await service.release(userId, replacement.leaseId);
  });

  it("settles actual overdraft usage above the estimate within the credit line", async () => {
    const userId = "00000000-0000-0000-0000-000000000931";
    const overdraftConfig: BursarConfigData = structuredClone(CONFIG);
    overdraftConfig.credits.policies = {
      line: { type: "credit_line", limit: "5" },
    };
    overdraftConfig.plans!.pro!.credit_policy = "line";
    delete overdraftConfig.plans!.pro!.quotas;
    const service = new CreditsService(store);
    await service.publishAndActivateCatalog(overdraftConfig);
    await service.setUserPlan(userId, "pro");

    const lease = await service.reserve(userId, new Decimal("2"), {
      idempotencyKey: "overdraft-estimate-reserve",
      operationType: "completion",
      ttl: 60,
    });
    expect(lease.billingMode).toBe("overdraft");
    expect(lease.minimumBalance.toString()).toBe("-5");

    const settled = await service.settle(userId, lease.leaseId, new Decimal("4"), {
      idempotencyKey: "overdraft-estimate-settle",
    });
    expect(settled.amount.toString()).toBe("4");
    expect(settled.balanceAfter?.toString()).toBe("-4");
    expect((await service.getBalance(userId)).balance.toString()).toBe("-4");

    const snapshot = await financialSnapshot(userId);
    expect(new Decimal(snapshot.balance).toString()).toBe("-4");
    expect(new Decimal(snapshot.ledger_total).toString()).toBe("-4");
    expect(snapshot.usage_entries).toBe(1);
    expect(snapshot.usage_charges).toBe(1);
    expect(snapshot.usage_keys).toBe(1);
    const leaseRow = await pool.query<{
      status: string;
      reserved_amount: string;
      settled_amount: string;
    }>(
      `SELECT status, reserved_amount, settled_amount
       FROM bursar.credit_leases
       WHERE id = $1::uuid`,
      [lease.leaseId],
    );
    expect(leaseRow.rows[0]).toEqual({
      status: "settled",
      reserved_amount: "2.000000",
      settled_amount: "4.000000",
    });
  });

  it("consumes allowance first, then debits the purchased bucket for the remainder", async () => {
    const userId = "00000000-0000-0000-0000-000000000932";
    const allowanceConfig: BursarConfigData = structuredClone(CONFIG);
    allowanceConfig.plans!.pro!.credit_allowance = {
      amount: "5",
      priority: 5,
      window: { type: "calendar", unit: "month", count: 1, timezone: "UTC" },
    };
    const service = new CreditsService(store);
    await service.publishAndActivateCatalog(allowanceConfig);
    await service.setUserPlan(userId, "pro");
    await service.addCredits(userId, new Decimal("3"), {
      type: "purchase",
      bucket: "purchased",
      idempotencyKey: "allowance-partial-funding",
    });

    const usage = {
      operation: "completion",
      measures: { input_tokens: 0, output_tokens: 4 },
      dimensions: { model: "standard" },
    };
    const allowanceOnly = await service.deduct(userId, usage, {
      idempotencyKey: "allowance-only-charge",
    });
    const allowanceThenBucket = await service.deduct(userId, usage, {
      idempotencyKey: "allowance-partial-charge",
    });

    expect(allowanceOnly.amount.toString()).toBe("0");
    expect(allowanceOnly.allowanceConsumed.toString()).toBe("4");
    expect(allowanceThenBucket.amount.toString()).toBe("3");
    expect(allowanceThenBucket.allowanceConsumed.toString()).toBe("1");
    expect(allowanceThenBucket.bucketBreakdown?.purchased?.toString()).toBe("3");
    expect((await service.checkAllowance(userId))?.allowanceRemaining.toString()).toBe("0");
    expect((await service.getBalance(userId)).balance.toString()).toBe("0");

    const snapshot = await financialSnapshot(userId);
    expect(new Decimal(snapshot.balance).toString()).toBe("0");
    expect(new Decimal(snapshot.ledger_total).toString()).toBe("0");
    expect(snapshot.usage_entries).toBe(1);
    expect(snapshot.usage_charges).toBe(2);
    expect(snapshot.usage_keys).toBe(2);
  });

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

  it("applies team accounting policy through CreditsService", async () => {
    const service = new CreditsService(store);
    await service.publishAndActivateCatalog(CONFIG);
    await service.setUserPlan(USER_ID, "pro");
    await service.setUserPlan(REPLAY_USER_ID, "pro");

    const team = await store.createTeam(USER_ID, "Service team", {
      idempotencyKey: "team:create:service-policy",
      initialBalance: new Decimal(10),
    });
    await store.addTeamMember(team.teamId, REPLAY_USER_ID, "member", new Decimal(3));

    const free = await service.deductTeam(
      team.teamId,
      REPLAY_USER_ID,
      { operation: "free_export", measures: { calls: 1 }, dimensions: {} },
      { idempotencyKey: "team-service-free" },
    );
    expect(free).toMatchObject({ teamId: team.teamId, amount: new Decimal(0) });
    expect(free.teamBalanceAfter?.toString()).toBe("10");

    const charged = await service.deductTeam(
      team.teamId,
      REPLAY_USER_ID,
      {
        operation: "completion",
        measures: { input_tokens: 1, output_tokens: 1 },
        dimensions: { model: "standard" },
      },
      { idempotencyKey: "team-service-charge" },
    );
    expect(charged.amount.toString()).toBe("2");
    expect(charged.teamBalanceAfter?.toString()).toBe("8");

    await expect(
      service.deductTeam(
        team.teamId,
        REPLAY_USER_ID,
        {
          operation: "completion",
          measures: { input_tokens: 1, output_tokens: 1 },
          dimensions: { model: "standard" },
        },
        { idempotencyKey: "team-service-cap" },
      ),
    ).rejects.toBeInstanceOf(CapReachedError);
    await expect(store.getTeamBalance(team.teamId)).resolves.toMatchObject({
      teamId: team.teamId,
      balance: new Decimal(8),
    });

    await service.addCredits(USER_ID, new Decimal(5), {
      type: "purchase",
      idempotencyKey: "service-refund-funding",
    });
    const usage = await service.deduct(
      USER_ID,
      { operation: "completion", measures: { input_tokens: 1, output_tokens: 1 } },
      { idempotencyKey: "service-refund-source" },
    );
    if (!usage.entryId) throw new Error("expected a refundable usage entry");
    await expect(
      service.refundCredits(usage.entryId, {
        idempotencyKey: "service-refund-1",
        reason: "integration correction",
      }),
    ).resolves.toMatchObject({ originalEntryId: usage.entryId });

    const expiring = await service.addCredits(USER_ID, new Decimal(1), {
      type: "purchase",
      bucket: "grant",
      expiresAt: new Date(Date.now() + 60_000),
      idempotencyKey: "service-expiring-credit",
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
    await expect(service.sweepExpiredCredits()).resolves.toMatchObject({
      dryRun: false,
      expiredCount: 1,
    });
  });

  it("keeps credit events observable while post-commit and team failures stay isolated", async () => {
    const events: string[] = [];
    const emitter = new CreditEventEmitter();
    emitter.on("credits.deducted", () => {
      events.push("deducted");
    });
    emitter.on("credits.revoked", () => {
      events.push("revoked");
    });
    const service = new CreditsService(store, null, emitter);
    await service.publishAndActivateCatalog(CONFIG);
    const removeHook = service.addPostDeductionHook(async () => {
      throw new Error("post-commit observer unavailable");
    });

    await service.addCredits(USER_ID, new Decimal(5), {
      type: "purchase",
      idempotencyKey: "observable-funding",
    });
    const deduction = await service.deduct(
      USER_ID,
      { operation: "completion", measures: { input_tokens: 1, output_tokens: 1 } },
      { idempotencyKey: "observable-deduction" },
    );
    expect(deduction.amount.toString()).toBe("2");
    removeHook();
    expect(events).toContain("deducted");

    await expect(service.revokeCreditsByEntryType(USER_ID, "purchase")).resolves.toMatchObject({
      userId: USER_ID,
      entryType: "purchase",
    });
    expect(events).toContain("revoked");

    const emptyTeam = await store.createTeam(TEAM_REPLAY_OWNER_ID, "Empty service team", {
      idempotencyKey: "team:service-empty",
      initialBalance: new Decimal(0),
    });
    await store.addTeamMember(emptyTeam.teamId, REPLAY_USER_ID, "member", new Decimal(3));
    await expect(
      service.deductTeam(
        emptyTeam.teamId,
        REPLAY_USER_ID,
        { operation: "completion", measures: { input_tokens: 1, output_tokens: 1 } },
        { idempotencyKey: "team-service-insufficient" },
      ),
    ).rejects.toBeInstanceOf(InsufficientCreditsError);
  });
});
