import { Bursar } from "../bursar.js";
import { z } from "zod";
import {
  ConfigError,
  ImportError as BursarImportError,
  isRetryableBursarError,
  CatalogNotLoadedError,
  StoreClosedError,
} from "../errors.js";
import { retryBursarOperation } from "../retry.js";
import { PostgresBillingStore } from "../billing/postgres/store.js";
import type { BillingServiceOptions } from "../billing/billing-service.js";
import { PostgresStore } from "../credits/postgres/store.js";
import type { CreditsServiceOptions } from "../credits/service.js";
import type { CreditEventEmitter } from "../credits/events.js";
import type { CommerceOptions } from "../commerce/types.js";
import {
  normalizeProviderEnvironment,
  type ProviderEnvironment,
} from "../providers/environment.js";
import type { UsageAnalyticsStore, UsageChargeStore } from "../credits/types/index.js";
import {
  normalizeTenantId,
  PostgresClient,
  postgresPoolConfig,
  type PostgresConnectionOptions,
  type PostgresPool,
} from "../shared/postgres-client.js";
import type { QueryFn } from "../shared/postgres-types.js";
import { ClickHouseUsageStore, type ClickHouseUsageStoreOptions } from "./adapters/clickhouse.js";
import { S3BillingArchive, type S3BillingArchiveOptions } from "./adapters/s3.js";
import {
  RuntimeDiagnosticsTracker,
  type BursarRuntimeDiagnostics,
  type BursarRuntimeState,
  type CheckDependenciesOptions,
  type OutboxStatusSnapshot,
} from "./diagnostics.js";
import { BursarMaintenance, BursarOperatorMaintenance } from "./maintenance.js";
import { OutboxWorker, type OutboxRunResult, type OutboxWorkerOptions } from "./outbox-worker.js";
import type {
  BillingEventPayloadExport,
  BillingPayloadArchive,
  OutboxHandler,
  OutboxRecoveryStore,
  UsageChargeExport,
  UsageEventSink,
} from "./ports.js";
import { PostgresStorageRepository } from "./postgres-repository.js";

/** Facade configuration accepted by the composed Node.js runtime. */
export interface BursarRuntimeBursarOptions {
  creditsOptions?: Omit<CreditsServiceOptions, "analytics" | "usageStore">;
  billingOptions?: Omit<BillingServiceOptions, "provisioning">;
  commerceOptions?: Omit<CommerceOptions, "tenantId" | "providerEnvironment">;
  emitter?: CreditEventEmitter;
}

export interface BursarRuntimeOptions {
  /** Tenant-scoped caller with SET-only membership in bursar_client. */
  postgres: string | PostgresPool;
  /** Cross-tenant caller with SET-only membership in bursar_operator. */
  operatorPostgres: string | PostgresPool;
  /** Explicit financial namespace shared by persistence and provider factories. */
  providerEnvironment: ProviderEnvironment;
  /** Applied to SDK-owned pools and per-transaction statement deadlines. */
  postgresOptions?: Omit<PostgresConnectionOptions, "providerEnvironment">;
  tenantId: string;
  /**
   * Optional provisioned tenant slug. When supplied, startup verifies that it
   * resolves to `tenantId` before any worker or catalog lifecycle begins.
   */
  tenantSlug?: string;
  s3?: BillingPayloadArchive | S3BillingArchiveOptions;
  clickhouse?: (UsageEventSink & UsageAnalyticsStore) | ClickHouseUsageStoreOptions;
  /**
   * Background delivery configuration. Set false only when another process
   * consumes Bursar's outbox.
   */
  outbox?: OutboxWorkerOptions | false;
  bursar?: BursarRuntimeBursarOptions;
}

export interface BursarRuntimeStartOptions {
  /** Load the active catalog before starting background workers. */
  loadCatalog?: boolean;
  /** Total catalog-load attempts, including the first. Defaults to 1. */
  maxAttempts?: number;
  /** Initial retry delay. Each retry doubles up to 5 seconds. */
  retryDelayMs?: number;
  shouldRetry?: (error: unknown) => boolean;
  /** Maximum elapsed catalog-load retry budget. Defaults to 30 seconds. */
  maxElapsedMs?: number;
  /** Abort startup retries and pending backoff. */
  signal?: AbortSignal;
}

const runtimeStartOptionsSchema = z
  .object({
    loadCatalog: z.boolean().default(true),
    maxAttempts: z.number().finite().int().min(1).max(Number.MAX_SAFE_INTEGER).default(1),
    retryDelayMs: z.number().finite().min(0).max(5_000).default(250),
    shouldRetry: z
      .custom<(error: unknown) => boolean>(
        (value) => typeof value === "function",
        "shouldRetry must be a function",
      )
      .optional(),
    maxElapsedMs: z.number().finite().min(0).max(2_147_483_647).optional(),
    signal: z
      .custom<AbortSignal>(
        (value) =>
          typeof value === "object" &&
          value !== null &&
          typeof (value as { throwIfAborted?: unknown }).throwIfAborted === "function",
        "signal must be an AbortSignal",
      )
      .optional(),
  })
  .strict();

export interface BursarRuntimeHealth {
  ready: boolean;
  financialReady: boolean;
  projectionReady: boolean;
  degraded: boolean;
  started: boolean;
  closed: boolean;
  catalogLoaded: boolean;
}

interface PendingUsageWrite {
  event: UsageChargeExport;
  outboxEventId: string;
  resolve: () => void;
  reject: (error: unknown) => void;
}

/** Coalesce concurrently dispatched usage events into one optional sink write. */
class UsageWriteBatcher {
  private pending: PendingUsageWrite[] = [];
  private scheduled = false;

  constructor(private readonly sink: UsageEventSink) {}

  write(event: UsageChargeExport, outboxEventId: string): Promise<void> {
    if (!this.sink.writeUsageBatch) return this.sink.writeUsage(event, outboxEventId);
    return new Promise<void>((resolve, reject) => {
      this.pending.push({ event, outboxEventId, resolve, reject });
      if (this.scheduled) return;
      this.scheduled = true;
      queueMicrotask(() => {
        void this.flush();
      });
    });
  }

  private async flush(): Promise<void> {
    const pending = this.pending.splice(0);
    this.scheduled = false;
    try {
      await this.sink.writeUsageBatch!(
        pending.map(({ event, outboxEventId }) => [event, outboxEventId] as const),
      );
      for (const write of pending) write.resolve();
    } catch (error) {
      for (const write of pending) write.reject(error);
    }
  }
}

/**
 * Node composition root for Bursar's stores and optional data infrastructure.
 *
 * Without S3 or ClickHouse it constructs the same PostgreSQL-only Bursar and
 * no outbox polling loop. Tenant and operator work always use distinct pools.
 */
export class BursarRuntime {
  readonly bursar: Bursar;
  readonly creditStore: PostgresStore;
  readonly billingStore: PostgresBillingStore;
  readonly maintenance: BursarMaintenance;
  readonly operatorMaintenance: BursarOperatorMaintenance;
  readonly worker: OutboxWorker | null;
  readonly outboxRecovery: OutboxRecoveryStore;
  readonly clickhouse: (UsageEventSink & UsageAnalyticsStore) | null;
  readonly s3: BillingPayloadArchive | null;

  private readonly pool: PostgresPool;
  private readonly operatorPool: PostgresPool;
  private readonly ownsPool: boolean;
  private readonly ownsOperatorPool: boolean;
  private readonly postgres: PostgresClient;
  private readonly query: QueryFn;
  private readonly tenantId: string;
  private readonly tenantSlug: string | null;
  private readonly diagnosticsTracker: RuntimeDiagnosticsTracker;
  private readonly usageBatcher: UsageWriteBatcher | null;
  readonly providerEnvironment: ProviderEnvironment;
  private startPromise: Promise<void> | null = null;
  private closePromise: Promise<void> | null = null;
  private started = false;
  private closed = false;

  /** Construct a runtime while keeping pool ownership inside the SDK. */
  static async create(options: BursarRuntimeOptions): Promise<BursarRuntime> {
    if (options.operatorPostgres === undefined || options.operatorPostgres === null) {
      throw new TypeError("operatorPostgres is required");
    }
    if (typeof options.postgres === "string" && !options.postgres.trim()) {
      throw new TypeError("postgres connection string must not be empty");
    }
    if (typeof options.operatorPostgres === "string" && !options.operatorPostgres.trim()) {
      throw new TypeError("operatorPostgres connection string must not be empty");
    }
    if (options.postgres === options.operatorPostgres) {
      throw new TypeError("postgres and operatorPostgres must use distinct connections");
    }

    let pg: typeof import("pg") | undefined;
    if (typeof options.postgres === "string" || typeof options.operatorPostgres === "string") {
      try {
        pg = await import("pg");
      } catch (cause) {
        throw new BursarImportError("pg is required for the Bursar runtime: npm install pg", {
          cause,
        });
      }
    }
    const ownsPool = typeof options.postgres === "string";
    const ownsOperatorPool = typeof options.operatorPostgres === "string";
    let pool: PostgresPool | undefined;
    let operatorPool: PostgresPool | undefined;
    try {
      pool =
        typeof options.postgres === "string"
          ? (new pg!.Pool(
              postgresPoolConfig(options.postgres, options.postgresOptions),
            ) as PostgresPool)
          : options.postgres;
      operatorPool =
        typeof options.operatorPostgres === "string"
          ? (new pg!.Pool(
              postgresPoolConfig(options.operatorPostgres, options.postgresOptions),
            ) as PostgresPool)
          : options.operatorPostgres;
      return new BursarRuntime(pool, ownsPool, operatorPool, ownsOperatorPool, options);
    } catch (error) {
      await Promise.allSettled([
        ...(ownsPool && pool ? [pool.end()] : []),
        ...(ownsOperatorPool && operatorPool ? [operatorPool.end()] : []),
      ]);
      throw error;
    }
  }

  private constructor(
    pool: PostgresPool,
    ownsPool: boolean,
    operatorPool: PostgresPool,
    ownsOperatorPool: boolean,
    options: BursarRuntimeOptions,
  ) {
    this.pool = pool;
    this.operatorPool = operatorPool;
    this.ownsPool = ownsPool;
    this.ownsOperatorPool = ownsOperatorPool;
    this.tenantId = normalizeTenantId(options.tenantId);
    this.providerEnvironment = normalizeProviderEnvironment(options.providerEnvironment);
    this.tenantSlug =
      options.tenantSlug === undefined ? null : normalizeTenantSlug(options.tenantSlug);
    for (const optionName of ["bursar", "s3", "clickhouse"] as const) {
      if (options[optionName] === null) {
        throw new TypeError(`${optionName} must not be null`);
      }
    }
    const bursarOptions = options.bursar ?? {};
    if (bursarOptions.creditsOptions === null) {
      throw new TypeError("bursar.creditsOptions must not be null");
    }
    if (
      bursarOptions.creditsOptions &&
      ("analytics" in bursarOptions.creditsOptions || "usageStore" in bursarOptions.creditsOptions)
    ) {
      throw new TypeError("BursarRuntime owns creditsOptions.analytics and usageStore");
    }
    if (bursarOptions.commerceOptions === null) {
      throw new TypeError("bursar.commerceOptions must not be null");
    }
    if (
      bursarOptions.commerceOptions &&
      ("tenantId" in bursarOptions.commerceOptions ||
        "providerEnvironment" in bursarOptions.commerceOptions)
    ) {
      throw new TypeError("BursarRuntime owns commerceOptions.tenantId and providerEnvironment");
    }
    if (
      options.clickhouse &&
      !("writeUsage" in options.clickhouse) &&
      normalizeTenantId(options.clickhouse.tenantId) !== this.tenantId
    ) {
      throw new TypeError("ClickHouse tenantId must match runtime tenantId");
    }
    this.clickhouse = options.clickhouse
      ? "writeUsage" in options.clickhouse
        ? options.clickhouse
        : new ClickHouseUsageStore({ ...options.clickhouse, tenantId: this.tenantId })
      : null;
    this.usageBatcher = this.clickhouse ? new UsageWriteBatcher(this.clickhouse) : null;
    this.s3 = options.s3
      ? "archive" in options.s3
        ? options.s3
        : new S3BillingArchive(options.s3)
      : null;

    this.creditStore = new PostgresStore({
      postgres: pool,
      tenantId: this.tenantId,
      ...(options.postgresOptions ?? {}),
      providerEnvironment: this.providerEnvironment,
      usageBackend: this.clickhouse ? "clickhouse" : "postgres",
    });
    this.billingStore = new PostgresBillingStore({
      postgres: pool,
      tenantId: this.tenantId,
      ...(options.postgresOptions ?? {}),
      providerEnvironment: this.providerEnvironment,
      billingPayloadBackend: this.s3 ? "s3" : "postgres",
    });
    const commerceOptions = bursarOptions.commerceOptions
      ? {
          ...bursarOptions.commerceOptions,
          tenantId: this.tenantId,
          providerEnvironment: this.providerEnvironment,
        }
      : undefined;
    this.bursar = new Bursar({
      ...bursarOptions,
      commerceOptions,
      creditStore: this.creditStore,
      billingStore: this.billingStore,
      creditsOptions: {
        ...(bursarOptions.creditsOptions ?? {}),
        analytics: this.clickhouse ?? undefined,
        usageStore:
          this.clickhouse && "listUsageCharges" in this.clickhouse
            ? (this.clickhouse as UsageChargeStore)
            : undefined,
      },
    });

    this.postgres = new PostgresClient(operatorPool, {
      ...(options.postgresOptions ?? {}),
      tenantId: this.tenantId,
      providerEnvironment: this.providerEnvironment,
      accessRole: "bursar_operator",
      usageBackend: this.clickhouse ? "clickhouse" : "postgres",
      billingPayloadBackend: this.s3 ? "s3" : "postgres",
    });
    this.query = this.postgres.query;
    this.maintenance = new BursarMaintenance({
      expireLeases: (limit) => this.creditStore.expireLeases(limit),
      expireCredits: async (limit) =>
        (await this.creditStore.sweepExpiredCredits(false, undefined, limit)).expiredCount,
      applyDuePlanChanges: (limit) => this.creditStore.applyDuePlanChanges(limit),
      expirePastDueGracePeriods: this.bursar.billing
        ? (now) => this.bursar.billing!.expirePastDueGracePeriods(now)
        : undefined,
      pastDueGracePeriodLimit: 100,
      pastDueGracePeriodsUnavailableReason: this.bursar.billing
        ? undefined
        : "billing is not configured",
    });
    this.operatorMaintenance = new BursarOperatorMaintenance(this.query);
    const repository = new PostgresStorageRepository(this.query, this.tenantId);
    this.outboxRecovery = repository;
    const handlers = this.createHandlers(repository);
    const configuredOutboxOptions =
      options.outbox === false || options.outbox === undefined ? {} : options.outbox;
    const originalOnError = configuredOutboxOptions.onError;
    let diagnosticsTracker: RuntimeDiagnosticsTracker | null = null;
    const workerOptions: OutboxWorkerOptions = {
      ...configuredOutboxOptions,
      onError: async (error) => {
        diagnosticsTracker?.recordWorkerError(error);
        await originalOnError?.(error);
      },
    };
    this.worker =
      handlers.length > 0 && options.outbox !== false
        ? new OutboxWorker(repository, handlers, workerOptions)
        : null;
    this.diagnosticsTracker = new RuntimeDiagnosticsTracker(
      {
        checkPostgres: async () => {
          const rows = await this.query("SELECT 1 AS bursar_reachable");
          const row = rows[0];
          if (
            !row ||
            typeof row !== "object" ||
            !("bursar_reachable" in row) ||
            (row as { bursar_reachable?: unknown }).bursar_reachable !== 1
          ) {
            throw new Error("PostgreSQL reachability check returned an invalid result");
          }
        },
        getCatalogRevision: async () => {
          const revision = await this.bursar.catalog.getActive();
          return revision ? { id: revision.id, version: revision.version } : null;
        },
        getOutboxStatus: outboxStatusProvider(repository),
      },
      this.worker !== null,
    );
    diagnosticsTracker = this.diagnosticsTracker;
  }

  start(options: BursarRuntimeStartOptions = {}): Promise<void> {
    if (this.closed) {
      return Promise.reject(new StoreClosedError("BursarRuntime has been closed"));
    }
    if (this.started) return Promise.resolve();
    if (!this.startPromise) {
      this.startPromise = this.startRuntime(options).catch((error: unknown) => {
        this.startPromise = null;
        throw error;
      });
    }
    return this.startPromise;
  }

  private async startRuntime(options: BursarRuntimeStartOptions): Promise<void> {
    const startOptions = runtimeStartOptionsSchema.parse(options);
    if (this.tenantSlug) await this.verifyTenantIdentity();
    if (startOptions.loadCatalog) {
      const shouldRetry =
        startOptions.shouldRetry ??
        ((error: unknown) =>
          error instanceof CatalogNotLoadedError || isRetryableBursarError(error));
      await retryBursarOperation(() => this.bursar.loadCatalog(), {
        maxAttempts: startOptions.maxAttempts,
        baseDelayMs: startOptions.retryDelayMs,
        maxDelayMs: 5_000,
        maxElapsedMs: startOptions.maxElapsedMs,
        signal: startOptions.signal,
        shouldRetry,
      });
    }
    await this.clickhouse?.initialize?.();
    await this.clickhouse?.checkSchemaCompatibility?.();
    await this.worker?.start();
    if (this.worker) this.diagnosticsTracker.markWorkerStarted();
    this.started = true;
  }

  state(): BursarRuntimeState {
    return this.diagnosticsTracker.state({
      started: this.started,
      closed: this.closed,
      catalogLoaded: this.bursar.catalog.isLoaded,
    });
  }

  checkDependencies(options: CheckDependenciesOptions = {}): Promise<BursarRuntimeDiagnostics> {
    return this.diagnosticsTracker.checkDependencies(
      {
        started: this.started,
        closed: this.closed,
        catalogLoaded: this.bursar.catalog.isLoaded,
      },
      options,
    );
  }

  health(): BursarRuntimeHealth {
    const state = this.state();
    return {
      ready: state.ready,
      financialReady: state.financialReady,
      projectionReady: state.projectionReady,
      degraded: state.degraded,
      started: state.started,
      closed: state.closed,
      catalogLoaded: state.catalogLoaded,
    };
  }

  async flush(): Promise<OutboxRunResult> {
    if (this.closed) throw new StoreClosedError("BursarRuntime has been closed");
    if (!this.worker) return { claimed: 0, delivered: 0, failed: 0, claimLost: 0 };
    return this.diagnosticsTracker.observeManualRun(() => this.worker!.runOnce());
  }

  close(): Promise<void> {
    if (this.closePromise) return this.closePromise;
    this.closed = true;
    this.closePromise = this.closeRuntime();
    return this.closePromise;
  }

  private async closeRuntime(): Promise<void> {
    const failures: unknown[] = [];
    const pendingStart = this.startPromise;
    if (pendingStart && !this.started) {
      try {
        await pendingStart;
      } catch {
        // Startup callers receive their own failure; shutdown must still clean up.
      }
    }

    try {
      await this.worker?.stop();
    } catch (error) {
      failures.push(error);
    } finally {
      if (this.worker) this.diagnosticsTracker.markWorkerStopped();
    }

    const resources = await Promise.allSettled([
      Promise.resolve().then(() => this.s3?.close?.()),
      Promise.resolve().then(() => this.creditStore.close()),
      Promise.resolve().then(() => this.billingStore.close()),
      Promise.resolve().then(() => this.postgres.close()),
    ]);
    for (const result of resources) {
      if (result.status === "rejected") failures.push(result.reason);
    }

    if (this.ownsPool) {
      try {
        await this.pool.end();
      } catch (error) {
        failures.push(error);
      }
    }
    if (this.ownsOperatorPool) {
      try {
        await this.operatorPool.end();
      } catch (error) {
        failures.push(error);
      }
    }

    if (failures.length === 1) throw failures[0];
    if (failures.length > 1) {
      throw new AggregateError(failures, "BursarRuntime failed to close all resources");
    }
  }

  private createHandlers(repository: PostgresStorageRepository): OutboxHandler[] {
    const handlers: OutboxHandler[] = [];
    if (this.clickhouse) {
      handlers.push({
        topics: ["usage.charge_recorded"],
        handle: async (outboxEvent) => {
          if (outboxEvent.payloadVersion !== 1) {
            throw new Error(
              `Unsupported usage outbox payload version ${outboxEvent.payloadVersion}`,
            );
          }
          let usage = usageExportFromOutbox(outboxEvent.payload);
          usage ??= await repository.getUsageCharge(outboxEvent.aggregateId);
          if (!usage) {
            throw new Error(`Usage charge ${outboxEvent.aggregateId} is unavailable for export`);
          }
          if (usage.tenantId !== outboxEvent.tenantId) {
            throw new Error("Usage export tenant does not match its outbox event");
          }
          if (usage.chargeId !== outboxEvent.aggregateId) {
            throw new Error("Usage export charge does not match its outbox event");
          }
          if (!this.usageBatcher) throw new Error("ClickHouse usage sink is not configured");
          await this.usageBatcher.write(usage, outboxEvent.eventId);
        },
      });
    }
    if (this.s3) {
      handlers.push({
        topics: ["billing.webhook_received", "billing.webhook_completed"],
        handle: async (outboxEvent) => {
          if (outboxEvent.payloadVersion !== 1) {
            throw new Error(
              `Unsupported billing outbox payload version ${outboxEvent.payloadVersion}`,
            );
          }
          const stored = await repository.getBillingEventPayload(outboxEvent.aggregateId);
          if (stored?.objectKey) return;
          const event =
            outboxEvent.topic === "billing.webhook_received"
              ? billingExportFromOutbox(outboxEvent.payload)
              : stored;
          if (!event) {
            throw new Error(`Billing event ${outboxEvent.aggregateId} is unavailable for archive`);
          }
          if (event.tenantId !== outboxEvent.tenantId) {
            throw new Error("Billing export tenant does not match its outbox event");
          }
          if (event.eventId !== outboxEvent.aggregateId) {
            throw new Error("Billing export event does not match its outbox event");
          }
          const archived = await this.s3?.archive(event);
          if (!archived) throw new Error("S3 archive is not configured");
          const recorded = await repository.archiveBillingEventPayload(
            event.eventId,
            archived.key,
            archived.versionId,
          );
          if (!recorded) {
            throw new Error(`Could not record archive pointer for billing event ${event.eventId}`);
          }
        },
      });
    }
    return handlers;
  }

  private async verifyTenantIdentity(): Promise<void> {
    if (!this.tenantSlug) return;
    const rows = await this.query(
      "SELECT bursar.resolve_active_tenant_for_trigger($1)::text AS tenant_id",
      [this.tenantSlug],
    );
    const resolved = rows[0];
    const tenantId =
      resolved && typeof resolved === "object" && "tenant_id" in resolved
        ? (resolved as { tenant_id?: unknown }).tenant_id
        : undefined;
    if (tenantId !== this.tenantId) {
      throw new ConfigError(
        `Bursar tenant slug '${this.tenantSlug}' resolves to a different tenant ID`,
      );
    }
  }
}

export async function createBursarRuntime(options: BursarRuntimeOptions): Promise<BursarRuntime> {
  return BursarRuntime.create(options);
}

function outboxStatusProvider(
  repository: PostgresStorageRepository,
): (limit: number) => Promise<OutboxStatusSnapshot> {
  const stats = (
    repository as unknown as {
      stats: (...args: unknown[]) => unknown;
    }
  ).stats;
  return async (limit) => {
    let raw: unknown;
    try {
      raw = await stats.call(repository, { limit });
    } catch (error) {
      if (!isInputShapeError(error)) throw error;
      raw = await stats.call(repository, limit);
    }
    if (!isJsonObject(raw)) throw new TypeError("outbox stats returned a malformed result");
    return {
      pendingCount: statsCount(raw, "pendingCount", "pending_count", "pending"),
      processingCount: statsCount(raw, "processingCount", "processing_count", "processing"),
      deliveredCount: statsCount(raw, "deliveredCount", "delivered_count", "delivered"),
      deadLetterCount: statsCount(raw, "deadLetterCount", "dead_letter_count", "dead_letter"),
      oldestPendingAt: statsTimestamp(raw, "oldestPendingAt", "oldest_pending_at"),
    };
  };
}

function statsCount(raw: Record<string, unknown>, ...keys: string[]): number {
  const value = keys.map((key) => raw[key]).find((candidate) => candidate !== undefined);
  if (!Number.isSafeInteger(value) || (value as number) < 0) {
    throw new TypeError(`outbox stats field ${keys[0]} must be a non-negative integer`);
  }
  return value as number;
}

function statsTimestamp(raw: Record<string, unknown>, ...keys: string[]): string | null {
  const value = keys.map((key) => raw[key]).find((candidate) => candidate !== undefined);
  if (value === null || value === undefined) return null;
  if (value instanceof Date && !Number.isNaN(value.getTime())) return value.toISOString();
  if (typeof value === "string") return value;
  throw new TypeError(`outbox stats field ${keys[0]} must be a timestamp string or null`);
}

function isInputShapeError(error: unknown): boolean {
  return (
    error instanceof TypeError ||
    error instanceof RangeError ||
    (error instanceof Error && error.name === "ZodError")
  );
}

function normalizeTenantSlug(value: string): string {
  if (typeof value !== "string") throw new TypeError("tenantSlug must be a string");
  const normalized = value.trim().toLowerCase();
  if (
    normalized.length < 1 ||
    normalized.length > 100 ||
    !/^[a-z0-9]+(?:[a-z0-9-]*[a-z0-9])?$/.test(normalized)
  ) {
    throw new TypeError("tenantSlug must be a valid Bursar tenant slug");
  }
  return normalized;
}

function usageExportFromOutbox(payload: Record<string, unknown>): UsageChargeExport | null {
  const requiredStrings = [
    "tenant_id",
    "charge_id",
    "account_id",
    "subject_id",
    "operation",
    "requested",
    "charged",
    "allowance_requested",
    "allowance_covered",
    "idempotency_key",
    "request_digest",
    "event_at",
    "created_at",
  ] as const;
  if (requiredStrings.some((key) => !isNonEmptyString(payload[key]))) return null;
  if (
    !isJsonObject(payload.measures) ||
    !isJsonObject(payload.dimensions) ||
    !isJsonObject(payload.metadata) ||
    !isJsonObject(payload.pricing_snapshot)
  ) {
    return null;
  }
  const optionalStrings = [
    "feature",
    "model",
    "region",
    "catalog_revision_id",
    "plan_id",
    "rate_card_key",
    "ledger_entry_id",
    "correction_of_charge_id",
  ] as const;
  if (optionalStrings.some((key) => !isOptionalString(payload[key]))) return null;
  if (
    payload.billing_disposition !== undefined &&
    payload.billing_disposition !== "billable" &&
    payload.billing_disposition !== "record_only"
  ) {
    return null;
  }
  return {
    tenantId: payload.tenant_id as string,
    chargeId: payload.charge_id as string,
    accountId: payload.account_id as string,
    subjectId: payload.subject_id as string,
    operation: payload.operation as string,
    feature: (payload.feature as string | null) ?? null,
    model: (payload.model as string | null) ?? null,
    region: (payload.region as string | null) ?? null,
    measures: payload.measures,
    dimensions: payload.dimensions,
    metadata: payload.metadata,
    requested: payload.requested as string,
    charged: payload.charged as string,
    allowanceRequested: payload.allowance_requested as string,
    allowanceCovered: payload.allowance_covered as string,
    billingDisposition: payload.billing_disposition === "record_only" ? "record_only" : "billable",
    catalogRevisionId: (payload.catalog_revision_id as string | null) ?? null,
    planId: (payload.plan_id as string | null) ?? null,
    rateCardKey: (payload.rate_card_key as string | null) ?? null,
    pricingSnapshot: payload.pricing_snapshot,
    ledgerEntryId: (payload.ledger_entry_id as string | null) ?? null,
    correctionOfChargeId: (payload.correction_of_charge_id as string | null) ?? null,
    idempotencyKey: payload.idempotency_key as string,
    requestDigest: payload.request_digest as string,
    eventAt: payload.event_at as string,
    createdAt: payload.created_at as string,
  };
}

function billingExportFromOutbox(
  payload: Record<string, unknown>,
): BillingEventPayloadExport | null {
  const requiredStrings = [
    "tenant_id",
    "event_id",
    "provider",
    "provider_environment",
    "provider_event_id",
    "event_type",
    "status",
    "received_at",
  ] as const;
  if (requiredStrings.some((key) => !isNonEmptyString(payload[key]))) return null;
  if (!isJsonObject(payload.envelope)) return null;
  if (!isOptionalString(payload.completed_at)) return null;
  return {
    tenantId: payload.tenant_id as string,
    eventId: payload.event_id as string,
    provider: payload.provider as string,
    providerEnvironment: payload.provider_environment as string,
    providerEventId: payload.provider_event_id as string,
    eventType: payload.event_type as string,
    status: payload.status as string,
    receivedAt: payload.received_at as string,
    completedAt: payload.completed_at ? String(payload.completed_at) : null,
    envelope: payload.envelope,
    objectKey: (payload.object_key as string | null) ?? null,
    objectVersion: (payload.object_version as string | null) ?? null,
    archivedAt: (payload.archived_at as string | null) ?? null,
  };
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.length > 0;
}

function isOptionalString(value: unknown): value is string | null | undefined {
  return value === null || value === undefined || typeof value === "string";
}

function isJsonObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
