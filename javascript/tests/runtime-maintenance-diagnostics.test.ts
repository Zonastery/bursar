import { describe, expect, it, vi } from "vitest";

import type { PostgresPool } from "../src/shared/postgres-client.js";
import {
  RuntimeDiagnosticsTracker,
  type OutboxStatusSnapshot,
} from "../src/storage/diagnostics.js";
import { BursarMaintenance, BursarOperatorMaintenance } from "../src/storage/maintenance.js";
import { PostgresStorageRepository } from "../src/storage/postgres-repository.js";
import { createBursarRuntime } from "../src/storage/runtime.js";

const TENANT_ID = "00000000-0000-0000-0000-000000000001";

describe("Bursar maintenance", () => {
  it("runs bounded tenant tasks and reports per-task progress", async () => {
    const now = new Date("2026-08-10T00:00:00.000Z");
    const seenLimits: number[] = [];
    const maintenance = new BursarMaintenance({
      expireLeases: async (limit) => {
        seenLimits.push(limit);
        return limit;
      },
      expireCredits: async (limit) => {
        seenLimits.push(limit);
        return 2;
      },
      applyDuePlanChanges: async (limit) => {
        seenLimits.push(limit);
        return 1;
      },
      expirePastDueGracePeriods: async (effectiveNow) => {
        expect(effectiveNow).toBe(now);
        return 3;
      },
      pastDueGracePeriodLimit: 100,
    });

    const result = await maintenance.runOnce({ limit: 7, now });

    expect(seenLimits).toEqual([7, 7, 7]);
    expect(result).toMatchObject({ status: "completed", count: 13, hasMore: true });
    expect(result.tasks.expiredLeases).toMatchObject({
      status: "completed",
      count: 7,
      limit: 7,
      hasMore: true,
    });
    expect(result.tasks.pastDueGracePeriods.limit).toBe(100);
  });

  it("reports unavailable tasks and persistence-safe failures explicitly", async () => {
    const maintenance = new BursarMaintenance({
      expireLeases: async () => {
        throw new Error("postgresql://user:secret@example.invalid/customer-42");
      },
      applyDuePlanChanges: async () => 0,
      pastDueGracePeriodsUnavailableReason: "billing is not configured",
    });

    const result = await maintenance.runOnce();

    expect(result.status).toBe("partial");
    expect(result.tasks.expiredLeases.error).toBe("maintenance_task_failed:Error");
    expect(result.tasks.expiredLeases.error).not.toContain("secret");
    expect(result.tasks.expiredCredits.status).toBe("unsupported");
    expect(result.tasks.pastDueGracePeriods).toMatchObject({
      status: "skipped",
      reason: "billing is not configured",
    });
  });

  it("ignores unavailable tasks when aggregating applicable task status", async () => {
    const healthy = new BursarMaintenance({
      expireLeases: async () => 0,
      expireCredits: async () => 0,
      applyDuePlanChanges: async () => 0,
      pastDueGracePeriodsUnavailableReason: "billing is not configured",
    });
    await expect(healthy.runOnce()).resolves.toMatchObject({ status: "completed" });

    const failed = new BursarMaintenance({
      expireLeases: async () => Promise.reject(new Error("private one")),
      expireCredits: async () => Promise.reject(new Error("private two")),
      applyDuePlanChanges: async () => Promise.reject(new Error("private three")),
      pastDueGracePeriodsUnavailableReason: "billing is not configured",
    });
    await expect(failed.runOnce()).resolves.toMatchObject({ status: "failed" });
  });

  it("keeps operator storage maintenance separate", async () => {
    const query = vi.fn(async (sql: string) => [
      {
        maintenance_result: sql.includes("run_storage_partition_maintenance")
          ? {
              status: "completed",
              parent_table: "usage_charge_payloads",
              partitions_created: 1,
              partitions_dropped: 2,
              partition_lock_timeouts: 0,
              default_partition_has_rows: false,
              has_more: false,
            }
          : {
              status: "completed",
              batch_size: 100,
              has_more: false,
              usage_payloads_purged: 1,
              record_only_usage_purged: 2,
              billing_payloads_purged: 3,
              quota_usage_events_purged: 4,
              quota_notifications_purged: 5,
              terminal_leases_compacted: 6,
              usage_rollups_purged: 7,
              outbox_events_purged: 8,
            },
      },
    ]);
    const maintenance = new BursarOperatorMaintenance(query);

    await expect(maintenance.runOnce({ mode: "force" })).resolves.toMatchObject({
      status: "completed",
      count: 36,
      batchSize: 100,
    });
    await expect(maintenance.runPartitionOnce("usage_charge_payloads")).resolves.toMatchObject({
      status: "completed",
      count: 3,
    });
    expect(query.mock.calls[0]?.[0]).toContain("run_storage_maintenance");
    expect(query.mock.calls[1]?.[0]).toContain("run_storage_partition_maintenance");
  });
});

describe("Bursar runtime diagnostics", () => {
  it("keeps state local and forwards the active outbox bound", async () => {
    const getOutboxStatus = vi.fn(async (_limit: number): Promise<OutboxStatusSnapshot> => ({
      pendingCount: 2,
      processingCount: 1,
      deliveredCount: 5,
      deadLetterCount: 0,
      oldestPendingAt: "2026-08-10T00:00:00.000Z",
    }));
    const checkPostgres = vi.fn(async () => undefined);
    const getCatalogRevision = vi.fn(async () => ({ id: "revision-id", version: 4 }));
    const tracker = new RuntimeDiagnosticsTracker(
      { checkPostgres, getCatalogRevision, getOutboxStatus },
      true,
    );

    expect(tracker.state({ started: true, closed: false, catalogLoaded: true })).toMatchObject({
      ready: false,
      financialReady: true,
      projectionReady: false,
      degraded: true,
      worker: { lifecycle: "not_started" },
    });
    expect(checkPostgres).not.toHaveBeenCalled();
    expect(getCatalogRevision).not.toHaveBeenCalled();
    expect(getOutboxStatus).not.toHaveBeenCalled();

    tracker.markWorkerStarted();
    const diagnostics = await tracker.checkDependencies(
      { started: true, closed: false, catalogLoaded: true },
      { outboxLimit: 7 },
    );
    expect(diagnostics).toMatchObject({
      ready: true,
      financialReady: true,
      projectionReady: true,
      degraded: false,
      postgres: { status: "ok" },
      catalog: { status: "ok", currentRevision: 4 },
      outbox: { status: "ok", limit: 7, snapshot: { pendingCount: 2 } },
    });
    expect(getOutboxStatus).toHaveBeenCalledWith(7);
  });

  it("reports optional projection degradation without hiding financial readiness", async () => {
    const deadLetters = new RuntimeDiagnosticsTracker(
      {
        checkPostgres: async () => undefined,
        getCatalogRevision: async () => ({ id: "revision-id", version: 4 }),
        getOutboxStatus: async () => ({
          pendingCount: 0,
          processingCount: 0,
          deliveredCount: 5,
          deadLetterCount: 1,
          oldestPendingAt: null,
        }),
      },
      true,
    );
    deadLetters.markWorkerStarted();

    await expect(
      deadLetters.checkDependencies({ started: true, closed: false, catalogLoaded: true }),
    ).resolves.toMatchObject({
      ready: false,
      financialReady: true,
      projectionReady: false,
      degraded: true,
      outbox: { status: "ok", snapshot: { deadLetterCount: 1 } },
    });

    const unavailable = new RuntimeDiagnosticsTracker(
      {
        checkPostgres: async () => undefined,
        getCatalogRevision: async () => ({ id: "revision-id", version: 4 }),
        getOutboxStatus: async () => Promise.reject(new Error("private outbox failure")),
      },
      true,
    );
    unavailable.markWorkerStarted();
    await expect(
      unavailable.checkDependencies({ started: true, closed: false, catalogLoaded: true }),
    ).resolves.toMatchObject({
      ready: false,
      financialReady: true,
      projectionReady: false,
      degraded: true,
      outbox: { status: "error", error: "outbox_check_failed:Error" },
    });
  });

  it("validates a configured projection schema before declaring startup complete", async () => {
    const order: string[] = [];
    const pool: PostgresPool = {
      query: vi.fn().mockResolvedValue({ rows: [] }),
      connect: vi.fn(),
      end: vi.fn().mockResolvedValue(undefined),
    };
    const runtime = await createBursarRuntime({
      postgres: pool,
      operatorPostgres: new Proxy(pool, {}),
      tenantId: TENANT_ID,
      providerEnvironment: "test",
      clickhouse: {
        initialize: async () => {
          order.push("initialize");
        },
        checkSchemaCompatibility: async () => {
          order.push("check");
        },
        writeUsage: async () => undefined,
        spendByUser: async () => [],
        spendByModel: async () => [],
        topUsers: async () => [],
        dailySpend: async () => [],
        aggregateStats: async () => {
          throw new Error("not used");
        },
      } as never,
      outbox: false,
    });

    await runtime.start({ loadCatalog: false });

    expect(order).toEqual(["initialize", "check"]);
    expect(runtime.health()).toMatchObject({
      ready: false,
      financialReady: false,
      projectionReady: true,
      degraded: false,
    });
    await runtime.close();
  });

  it("does not expose active-check exception messages", async () => {
    const tracker = new RuntimeDiagnosticsTracker(
      {
        checkPostgres: async () => {
          throw new Error("postgresql://user:secret@example.invalid/customer-42");
        },
        getCatalogRevision: async () => null,
      },
      false,
    );

    const diagnostics = await tracker.checkDependencies({
      started: true,
      closed: false,
      catalogLoaded: true,
    });

    expect(diagnostics.postgres.error).toBe("postgres_check_failed:Error");
    expect(diagnostics.postgres.error).not.toContain("secret");
  });

  it("owns facades and adapts options-object outbox stats", async () => {
    const stats = vi
      .spyOn(PostgresStorageRepository.prototype, "stats")
      .mockImplementation(async () => ({
        pendingCount: 3,
        processingCount: 0,
        deliveredCount: 9,
        deadLetterCount: 1,
        oldestPendingAt: null,
      }));
    const query = vi.fn(async (sql: string) => ({
      rows: sql.includes("SELECT 1 AS bursar_reachable") ? [{ bursar_reachable: 1 }] : [],
    }));
    const pool: PostgresPool = {
      query: vi.fn().mockResolvedValue({ rows: [] }),
      connect: vi.fn().mockResolvedValue({ query, release: vi.fn() }),
      end: vi.fn().mockResolvedValue(undefined),
    };
    const runtime = await createBursarRuntime({
      postgres: pool,
      operatorPostgres: new Proxy(pool, {}),
      tenantId: TENANT_ID,
      providerEnvironment: "test",
    });
    const getActive = vi
      .spyOn(runtime.bursar.catalog, "getActive")
      .mockResolvedValue({ id: "revision-id", version: 5, config: {} });

    expect(runtime.maintenance).toBeInstanceOf(BursarMaintenance);
    expect(runtime.operatorMaintenance).toBeInstanceOf(BursarOperatorMaintenance);
    runtime.state();
    expect(query).not.toHaveBeenCalled();
    expect(getActive).not.toHaveBeenCalled();

    const diagnostics = await runtime.checkDependencies({ outboxLimit: 11 });
    expect(diagnostics.outbox).toMatchObject({
      status: "ok",
      limit: 11,
      snapshot: { deadLetterCount: 1 },
    });
    expect(stats).toHaveBeenCalledWith({ limit: 11 });
    await runtime.close();
    stats.mockRestore();
  });
});
