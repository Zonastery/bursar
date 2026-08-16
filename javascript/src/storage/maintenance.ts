import { z } from "zod";

import { persistedDiagnosticSummary } from "../shared/diagnostics.js";
import { isJsonObject, type JsonObject, type PostgresParams } from "../shared/json.js";
import type { QueryFn } from "../shared/postgres-types.js";

export type MaintenanceTaskStatus = "completed" | "skipped" | "unsupported" | "failed";
export type MaintenanceRunStatus = "completed" | "partial" | "failed";

export interface MaintenanceTaskResult {
  status: MaintenanceTaskStatus;
  count: number;
  limit: number;
  hasMore: boolean;
  reason?: string;
  error?: string;
}

export interface MaintenanceRunResult {
  status: MaintenanceRunStatus;
  count: number;
  hasMore: boolean;
  tasks: {
    expiredLeases: MaintenanceTaskResult;
    expiredCredits: MaintenanceTaskResult;
    duePlanChanges: MaintenanceTaskResult;
    pastDueGracePeriods: MaintenanceTaskResult;
  };
}

export interface MaintenanceRunOptions {
  /** Maximum rows handled by each configurable tenant-scoped task. */
  limit?: number;
  /** Effective time used by time-sensitive tasks. Defaults to now. */
  now?: Date;
}

export interface MaintenanceOperations {
  expireLeases?: (limit: number) => Promise<number>;
  expireCredits?: (limit: number) => Promise<number>;
  applyDuePlanChanges?: (limit: number) => Promise<number>;
  expirePastDueGracePeriods?: (now: Date) => Promise<number>;
  pastDueGracePeriodLimit?: number;
  pastDueGracePeriodsUnavailableReason?: string;
}

const maintenanceRunOptionsSchema = z
  .object({
    limit: z.number().finite().int().min(1).max(1_000).default(100),
    now: z.date().optional(),
  })
  .strict();

/** Host-invoked, tenant-scoped maintenance. This class never schedules itself. */
export class BursarMaintenance {
  constructor(private readonly operations: MaintenanceOperations) {}

  async runOnce(options: MaintenanceRunOptions = {}): Promise<MaintenanceRunResult> {
    const parsed = maintenanceRunOptionsSchema.parse(options);
    const now = parsed.now ?? new Date();
    const graceLimit = this.operations.pastDueGracePeriodLimit ?? 100;
    if (!Number.isInteger(graceLimit) || graceLimit < 1 || graceLimit > 1_000) {
      throw new RangeError("pastDueGracePeriodLimit must be an integer between 1 and 1000");
    }

    const expiredLeases = await runTask(this.operations.expireLeases, parsed.limit, parsed.limit);
    const expiredCredits = await runTask(this.operations.expireCredits, parsed.limit, parsed.limit);
    const duePlanChanges = await runTask(
      this.operations.applyDuePlanChanges,
      parsed.limit,
      parsed.limit,
    );
    const pastDueGracePeriods = this.operations.expirePastDueGracePeriods
      ? await runTask(() => this.operations.expirePastDueGracePeriods!(now), graceLimit, graceLimit)
      : unavailableTask(graceLimit, this.operations.pastDueGracePeriodsUnavailableReason);

    const tasks = {
      expiredLeases,
      expiredCredits,
      duePlanChanges,
      pastDueGracePeriods,
    };
    const results = Object.values(tasks);
    const count = results.reduce((total, task) => total + task.count, 0);
    const applicable = results.filter(
      (task) => task.status === "completed" || task.status === "failed",
    );
    const failed = applicable.filter((task) => task.status === "failed").length;
    const status: MaintenanceRunStatus =
      applicable.length > 0 && failed === applicable.length
        ? "failed"
        : failed > 0
          ? "partial"
          : "completed";

    return {
      status,
      count,
      hasMore: results.some((task) => task.hasMore),
      tasks,
    };
  }
}

export type StorageMaintenanceStatus = "completed" | "busy" | "not_due" | "failed";

export interface StorageMaintenanceCounts {
  usagePayloadsPurged: number;
  recordOnlyUsagePurged: number;
  billingPayloadsPurged: number;
  quotaUsageEventsPurged: number;
  quotaNotificationsPurged: number;
  terminalLeasesCompacted: number;
  usageRollupsPurged: number;
  outboxEventsPurged: number;
}

export interface StorageMaintenanceResult {
  status: StorageMaintenanceStatus;
  count: number;
  hasMore: boolean;
  batchSize: number | null;
  counts: StorageMaintenanceCounts;
  lastMaintenanceAt?: string;
  nextMaintenanceAt?: string;
  error?: string;
}

export interface OperatorMaintenanceRunOptions {
  /** Run only when due by default; force invokes the bounded pass immediately. */
  mode?: "ifDue" | "force";
  now?: Date;
}

export type StoragePartition = "usage_charge_payloads" | "billing_event_payloads";

export interface PartitionMaintenanceResult {
  status: "completed" | "busy" | "failed";
  parentTable: StoragePartition;
  count: number;
  partitionsCreated: number;
  partitionsDropped: number;
  partitionLockTimeouts: number;
  defaultPartitionHasRows: boolean;
  hasMore: boolean;
  error?: string;
}

const operatorMaintenanceRunOptionsSchema = z
  .object({
    mode: z.enum(["ifDue", "force"]).default("ifDue"),
    now: z.date().optional(),
  })
  .strict();

const partitionSchema = z.enum(["usage_charge_payloads", "billing_event_payloads"]);

/** Explicit operator-only storage and partition maintenance entry points. */
export class BursarOperatorMaintenance {
  constructor(private readonly query: QueryFn) {}

  async runOnce(options: OperatorMaintenanceRunOptions = {}): Promise<StorageMaintenanceResult> {
    const parsed = operatorMaintenanceRunOptionsSchema.parse(options);
    const functionName =
      parsed.mode === "force" ? "run_storage_maintenance" : "maybe_run_storage_maintenance";
    try {
      const payload = await callJsonFunction(this.query, functionName, [
        (parsed.now ?? new Date()).toISOString(),
      ]);
      return storageMaintenanceResult(payload);
    } catch (cause) {
      return failedStorageMaintenance(cause);
    }
  }

  async runPartitionOnce(
    parentTable: StoragePartition,
    options: Omit<OperatorMaintenanceRunOptions, "mode"> = {},
  ): Promise<PartitionMaintenanceResult> {
    const parent = partitionSchema.parse(parentTable);
    const parsed = operatorMaintenanceRunOptionsSchema.parse({ ...options, mode: "force" });
    try {
      const payload = await callJsonFunction(this.query, "run_storage_partition_maintenance", [
        parent,
        (parsed.now ?? new Date()).toISOString(),
      ]);
      return partitionMaintenanceResult(parent, payload);
    } catch (cause) {
      return {
        status: "failed",
        parentTable: parent,
        count: 0,
        partitionsCreated: 0,
        partitionsDropped: 0,
        partitionLockTimeouts: 0,
        defaultPartitionHasRows: false,
        hasMore: true,
        error: persistedDiagnosticSummary(cause, "partition_maintenance_failed"),
      };
    }
  }
}

async function runTask(
  operation: ((limit: number) => Promise<number>) | undefined,
  operationLimit: number,
  reportedLimit: number,
): Promise<MaintenanceTaskResult> {
  if (!operation) return unavailableTask(reportedLimit);
  try {
    const count = await operation(operationLimit);
    if (!Number.isSafeInteger(count) || count < 0) {
      throw new TypeError("maintenance task returned an invalid count");
    }
    return {
      status: "completed",
      count,
      limit: reportedLimit,
      hasMore: count === reportedLimit,
    };
  } catch (cause) {
    return {
      status: "failed",
      count: 0,
      limit: reportedLimit,
      hasMore: true,
      error: persistedDiagnosticSummary(cause, "maintenance_task_failed"),
    };
  }
}

function unavailableTask(limit: number, reason?: string): MaintenanceTaskResult {
  const result: MaintenanceTaskResult = {
    status: reason ? "skipped" : "unsupported",
    count: 0,
    limit,
    hasMore: false,
  };
  if (reason) result.reason = reason;
  return result;
}

async function callJsonFunction(
  query: QueryFn,
  functionName:
    | "run_storage_maintenance"
    | "maybe_run_storage_maintenance"
    | "run_storage_partition_maintenance",
  params: PostgresParams,
): Promise<JsonObject> {
  const placeholders = params.map((_, index) => `$${index + 1}`).join(", ");
  const rows = await query(
    `SELECT bursar.${functionName}(${placeholders}) AS maintenance_result`,
    params,
  );
  const row = rows[0];
  if (row === undefined) throw new TypeError("maintenance RPC returned no result");
  const raw = row.maintenance_result;
  if (raw !== undefined && isJsonObject(raw)) return raw;
  const text = z.string().safeParse(raw);
  if (text.success) {
    const parsed = z.json().safeParse(JSON.parse(text.data));
    if (parsed.success && isJsonObject(parsed.data)) return parsed.data;
  }
  throw new TypeError("maintenance RPC returned a malformed result");
}

function storageMaintenanceResult(payload: JsonObject): StorageMaintenanceResult {
  const status = payload.status;
  if (status === "busy") {
    return { ...emptyStorageMaintenanceCounts(), status, hasMore: true };
  }
  if (status === "not_due") {
    return {
      ...emptyStorageMaintenanceCounts(),
      status,
      hasMore: false,
      ...optionalTimestamp(payload, "last_maintenance_at", "lastMaintenanceAt"),
      ...optionalTimestamp(payload, "next_maintenance_at", "nextMaintenanceAt"),
    };
  }
  if (status !== "completed") throw new TypeError("maintenance RPC returned an unknown status");
  const counts: StorageMaintenanceCounts = {
    usagePayloadsPurged: nonNegativeInteger(payload, "usage_payloads_purged"),
    recordOnlyUsagePurged: nonNegativeInteger(payload, "record_only_usage_purged"),
    billingPayloadsPurged: nonNegativeInteger(payload, "billing_payloads_purged"),
    quotaUsageEventsPurged: nonNegativeInteger(payload, "quota_usage_events_purged"),
    quotaNotificationsPurged: nonNegativeInteger(payload, "quota_notifications_purged"),
    terminalLeasesCompacted: nonNegativeInteger(payload, "terminal_leases_compacted"),
    usageRollupsPurged: nonNegativeInteger(payload, "usage_rollups_purged"),
    outboxEventsPurged: nonNegativeInteger(payload, "outbox_events_purged"),
  };
  return {
    status,
    count: Object.values(counts).reduce((total, count) => total + count, 0),
    hasMore: booleanField(payload, "has_more"),
    batchSize: nonNegativeInteger(payload, "batch_size"),
    counts,
  };
}

function partitionMaintenanceResult(
  parentTable: StoragePartition,
  payload: JsonObject,
): PartitionMaintenanceResult {
  if (payload.status === "busy") {
    return {
      status: "busy",
      parentTable,
      count: 0,
      partitionsCreated: 0,
      partitionsDropped: 0,
      partitionLockTimeouts: 0,
      defaultPartitionHasRows: false,
      hasMore: true,
    };
  }
  if (payload.status !== "completed" || payload.parent_table !== parentTable) {
    throw new TypeError("partition maintenance RPC returned a malformed result");
  }
  const partitionsCreated = nonNegativeInteger(payload, "partitions_created");
  const partitionsDropped = nonNegativeInteger(payload, "partitions_dropped");
  const partitionLockTimeouts = nonNegativeInteger(payload, "partition_lock_timeouts");
  return {
    status: "completed",
    parentTable,
    count: partitionsCreated + partitionsDropped,
    partitionsCreated,
    partitionsDropped,
    partitionLockTimeouts,
    defaultPartitionHasRows: booleanField(payload, "default_partition_has_rows"),
    hasMore: booleanField(payload, "has_more"),
  };
}

function failedStorageMaintenance(cause: unknown): StorageMaintenanceResult {
  return {
    ...emptyStorageMaintenanceCounts(),
    status: "failed",
    hasMore: true,
    error: persistedDiagnosticSummary(cause, "storage_maintenance_failed"),
  };
}

function emptyStorageMaintenanceCounts(): Omit<
  StorageMaintenanceResult,
  "status" | "hasMore" | "error"
> {
  return {
    count: 0,
    batchSize: null,
    counts: {
      usagePayloadsPurged: 0,
      recordOnlyUsagePurged: 0,
      billingPayloadsPurged: 0,
      quotaUsageEventsPurged: 0,
      quotaNotificationsPurged: 0,
      terminalLeasesCompacted: 0,
      usageRollupsPurged: 0,
      outboxEventsPurged: 0,
    },
  };
}

function nonNegativeInteger(payload: JsonObject, key: string): number {
  const value = payload[key];
  const parsed = z.number().int().nonnegative().safeParse(value);
  if (!parsed.success) {
    throw new TypeError(`maintenance RPC field ${key} must be a non-negative integer`);
  }
  return parsed.data;
}

function booleanField(payload: JsonObject, key: string): boolean {
  const value = payload[key];
  const parsed = z.boolean().safeParse(value);
  if (!parsed.success) {
    throw new TypeError(`maintenance RPC field ${key} must be a boolean`);
  }
  return parsed.data;
}

interface MaintenanceTimestampPatch {
  lastMaintenanceAt?: string;
  nextMaintenanceAt?: string;
}

function optionalTimestamp(
  payload: JsonObject,
  key: string,
  outputKey: keyof MaintenanceTimestampPatch,
): MaintenanceTimestampPatch {
  const value = payload[key];
  if (value === undefined || value === null) return {};
  const parsed = z.string().safeParse(value);
  if (!parsed.success) {
    throw new TypeError(`maintenance RPC field ${key} must be a timestamp string`);
  }
  if (outputKey === "lastMaintenanceAt") return { lastMaintenanceAt: parsed.data };
  return { nextMaintenanceAt: parsed.data };
}
