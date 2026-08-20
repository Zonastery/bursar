import { Decimal } from "decimal.js";
import pg from "pg";
import { afterAll, beforeAll, beforeEach, describe, expect, inject, it, vi } from "vitest";

import { Bursar } from "../src/bursar.js";
import type { BursarConfigData } from "../src/config.js";
import { CreditEventEmitter } from "../src/credits/events.js";
import { PostgresStore } from "../src/credits/postgres/store.js";
import { CreditsService } from "../src/credits/service.js";
import { TEST_TENANT_ID, applyMigrations, truncateBursarTables } from "./helpers/bootstrap.js";

const DATABASE_URL = inject("DATABASE_URL");
const EXPIRING_USER_ID = "00000000-0000-0000-0000-000000000951";
const RENEWING_USER_ID = "00000000-0000-0000-0000-000000000952";
const MONITORED_USER_ID = "00000000-0000-0000-0000-000000000953";
const FACADE_USER_ID = "00000000-0000-0000-0000-000000000954";

const CONFIG: BursarConfigData = {
  version: 1,
  catalog: { default_plan: "pro" },
  pricing: {
    operations: {
      completion: {
        measures: { tokens: { unit: "token" } },
        dimensions: {},
      },
      background_job: {
        measures: { jobs: { unit: "job" } },
        dimensions: {},
      },
    },
    rate_cards: {
      standard: {
        operations: {
          completion: {
            rules: [],
            unmatched: { action: "charge", charge: { type: "flat", amount: "1" } },
          },
          background_job: {
            rules: [],
            unmatched: { action: "charge", charge: { type: "flat", amount: "2" } },
          },
        },
      },
    },
  },
  credits: {
    buckets: {
      expiring: {
        priority: 10,
        expiry: {
          type: "after_grant",
          interval: { unit: "day", count: 1 },
          timezone: "UTC",
        },
      },
      subscription: { priority: 20, expiry: { type: "never" } },
    },
    default_bucket: "subscription",
  },
  plans: {
    pro: {
      display_name: "Pro",
      rank: 1,
      rate_card: "standard",
      allowed_operations: ["completion", "background_job"],
    },
  },
};

describe.runIf(DATABASE_URL)("CreditsService resilience integration", () => {
  const pool = new pg.Pool({ connectionString: DATABASE_URL, max: 1 });
  const store = new PostgresStore({
    postgres: pool,
    tenantId: TEST_TENANT_ID,
    providerEnvironment: "test",
  });

  beforeAll(() => applyMigrations(pool), 60_000);
  beforeEach(() => truncateBursarTables(pool));
  afterAll(() => pool.end());

  it("expires a stale lot on the next balance read and emits one expiry event", async () => {
    const events: string[] = [];
    const emitter = new CreditEventEmitter();
    emitter.on("credits.expired", () => {
      events.push("expired");
    });
    const credits = new CreditsService(store, null, emitter, { lazyExpiry: true });
    await credits.publishAndActivateCatalog(CONFIG);

    const grant = await credits.addCredits(EXPIRING_USER_ID, new Decimal(7), {
      type: "purchase",
      bucket: "expiring",
      idempotencyKey: "resilience-expiring-grant",
    });
    expect(grant.entryId).toBeTruthy();
    expect((await credits.getBalance(EXPIRING_USER_ID)).balance.toString()).toBe("7");

    const mutationClient = await pool.connect();
    try {
      await mutationClient.query("BEGIN");
      await mutationClient.query("SELECT set_config('bursar.mutation_context', 'internal', true)");
      await mutationClient.query(
        `UPDATE bursar.credit_lots
            SET expires_at = now() - interval '1 second'
          WHERE source_entry_id = $1::uuid`,
        [grant.entryId],
      );
      await mutationClient.query("COMMIT");
    } catch (error) {
      await mutationClient.query("ROLLBACK");
      throw error;
    } finally {
      mutationClient.release();
    }

    await expect(credits.getBalance(EXPIRING_USER_ID)).resolves.toMatchObject({
      balance: new Decimal(0),
    });
    expect(events).toEqual(["expired"]);
    await expect(credits.getBalance(EXPIRING_USER_ID)).resolves.toMatchObject({
      balance: new Decimal(0),
    });
    expect(events).toEqual(["expired"]);
  });

  it("makes renewal webhook replay safe while rejecting conflicting expiry options", async () => {
    const bursar = new Bursar({ creditStore: store });
    await bursar.catalog.publishAndActivate(CONFIG);

    await expect(
      bursar.credits.grantSubscriptionCycle(RENEWING_USER_ID, new Decimal(10), {
        idempotencyKey: "renewal-conflicting-expiry",
        expiresAt: new Date(),
        ttlDays: 30,
      }),
    ).rejects.toThrow(/at most one/);

    const renewalOptions = {
      idempotencyKey: "renewal-replay-safe",
      expiresAt: new Date(Date.now() + 30 * 86_400_000),
      planKey: "pro",
      bucket: "subscription",
      metadata: { providerEvent: "invoice.paid" },
    };
    const first = await bursar.credits.grantSubscriptionCycle(
      RENEWING_USER_ID,
      new Decimal(10),
      renewalOptions,
    );
    const replay = await bursar.credits.grantSubscriptionCycle(
      RENEWING_USER_ID,
      new Decimal(10),
      renewalOptions,
    );

    expect(first.idempotent).toBe(false);
    expect(replay).toMatchObject({ entryId: first.entryId, idempotent: true });
    expect((await bursar.credits.getBalance(RENEWING_USER_ID)).balance.toString()).toBe("10");
    expect((await bursar.credits.getUserPlan(RENEWING_USER_ID)).planKey).toBe("pro");

    const job = await bursar.credits.deductFlatJob(RENEWING_USER_ID, "background_job", {
      idempotencyKey: "renewal-background-job",
    });
    expect(job.amount.toString()).toBe("2");
    expect((await bursar.credits.getBalance(RENEWING_USER_ID)).balance.toString()).toBe("8");
  });

  it("edge-triggers low-balance thresholds, rearms after credit, and isolates hooks", async () => {
    const lowBalanceEvents: Array<{ balance: string; threshold: string }> = [];
    const handler = vi.fn(async () => {
      throw new Error("notification transport unavailable");
    });
    const logger = { error: vi.fn(), warn: vi.fn() };
    const emitter = new CreditEventEmitter();
    emitter.on("credits.low_balance", (event) => {
      lowBalanceEvents.push({
        balance: String(event.data?.balance),
        threshold: String(event.data?.threshold),
      });
    });
    const credits = new CreditsService(store, null, emitter, {
      logger,
      lowBalance: { thresholds: ["5", "2"], onTrigger: handler },
      postDeduction: async () => {
        throw new Error("analytics sink unavailable");
      },
    });
    await credits.publishAndActivateCatalog(CONFIG);
    await credits.addCredits(MONITORED_USER_ID, new Decimal(12), {
      type: "purchase",
      idempotencyKey: "low-balance-seed",
    });

    for (const [index, key] of [
      "low-balance-noop-1",
      "low-balance-noop-2",
      "low-balance-noop-3",
    ].entries()) {
      await credits.deductFlatJob(MONITORED_USER_ID, "background_job", {
        idempotencyKey: key,
        metadata: { attempt: index, nullable: null },
      });
    }
    await credits.deductFlatJob(MONITORED_USER_ID, "background_job", {
      idempotencyKey: "low-balance-cross-5",
    });
    await credits.deductFlatJob(MONITORED_USER_ID, "background_job", {
      idempotencyKey: "low-balance-cross-2",
    });
    const replay = await credits.deductFlatJob(MONITORED_USER_ID, "background_job", {
      idempotencyKey: "low-balance-cross-2",
    });
    expect(replay.idempotent).toBe(true);
    expect(lowBalanceEvents).toEqual([
      { balance: "4", threshold: "5" },
      { balance: "2", threshold: "2" },
    ]);

    await credits.addCredits(MONITORED_USER_ID, new Decimal(5), {
      type: "purchase",
      idempotencyKey: "low-balance-rearm",
    });
    await credits.deductFlatJob(MONITORED_USER_ID, "background_job", {
      idempotencyKey: "low-balance-cross-again",
    });
    expect(lowBalanceEvents).toEqual([
      { balance: "4", threshold: "5" },
      { balance: "2", threshold: "2" },
      { balance: "5", threshold: "5" },
    ]);
    expect(handler).toHaveBeenCalledTimes(3);
    expect(logger.error).toHaveBeenCalledTimes(3);
    expect(logger.warn).toHaveBeenCalledTimes(6);
    expect((await credits.getBalance(MONITORED_USER_ID)).balance.toString()).toBe("5");
  });

  it("drives catalog lifecycle operations through the Bursar facade", async () => {
    const bursar = new Bursar({ creditStore: store });
    expect(bursar.catalog.isLoaded).toBe(false);
    const draftId = await bursar.catalog.publishDraft(CONFIG, "facade draft");
    const draft = (await store.getCatalogHistory()).find((revision) => revision.id === draftId);
    expect(draft?.active).toBe(false);
    expect(draft).toBeDefined();

    await bursar.catalog.activate(draft!.version);
    expect(bursar.catalog.isLoaded).toBe(true);
    await bursar.catalog.refresh();
    bursar.catalog.invalidate();
    await bursar.loadCatalog();

    await bursar.credits.setUserPlan(FACADE_USER_ID, "pro");
    await expect(bursar.catalog.setRevisionPin(FACADE_USER_ID, true)).resolves.toBe(true);
    await expect(bursar.catalog.setRevisionPin(FACADE_USER_ID, false)).resolves.toBe(true);
    await expect(bursar.catalog.applyDueChanges(10)).resolves.toBe(0);
    await expect(bursar.catalog.getConfig()).resolves.toMatchObject({
      catalog: { defaultPlan: "pro" },
    });
  });
});
