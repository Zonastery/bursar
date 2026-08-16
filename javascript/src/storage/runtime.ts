import { z } from "zod";

import { Bursar } from "../bursar.js";
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
import type { JsonObject } from "../shared/json.js";
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
  shouldRetry?: (cause: unknown) => boolean;
  /** Maximum elapsed catalog-load retry budget. Defaults to 30 seconds. */
  maxElapsedMs?: number;
  /** Abort startup retries and pending backoff. */
  signal?: AbortSignal;
}

const abortSignalSchema = z.custom<AbortSignal>(
  (value) =>
    z
      .object({
        aborted: z.boolean(),
        addEventListener: z.function(),
        removeEventListener: z.function(),
        throwIfAborted: z.function(),
      })
      .safeParse(value).success,
  "signal must be an AbortSignal",
);

const shouldRetrySchema = z.custom<(cause: unknown) => boolean>(
  (value) => z.function().safeParse(value).success,
  "shouldRetry must be a function",
);

const runtimeStartOptionsSchema = z
  .object({
    loadCatalog: z.boolean().default(true),
    maxAttempts: z.number().finite().int().min(1).max(Number.MAX_SAFE_INTEGER).default(1),
    retryDelayMs: z.number().finite().min(0).max(5_000).default(250),
    shouldRetry: shouldRetrySchema.optional(),
    maxElapsedMs: z.number().finite().min(0).max(2_147_483_647).optional(),
    signal: abortSignalSchema.optional(),
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
  reject: (error: Error) => void;
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
      const failure =
        error instanceof Error ? error : new Error("Usage batch write failed", { cause: error });
      for (const write of pending) write.reject(failure);
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
    const postgresConnection = connectionStringValue(options.postgres);
    const operatorPostgresConnection = connectionStringValue(options.operatorPostgres);
    const ownsPool = postgresConnection !== null;
    const ownsOperatorPool = operatorPostgresConnection !== null;
    if (postgresConnection !== null && !postgresConnection.trim()) {
      throw new TypeError("postgres connection string must not be empty");
    }
    if (operatorPostgresConnection !== null && !operatorPostgresConnection.trim()) {
      throw new TypeError("operatorPostgres connection string must not be empty");
    }
    if (options.postgres === options.operatorPostgres) {
      throw new TypeError("postgres and operatorPostgres must use distinct connections");
    }

    let pg: typeof import("pg") | undefined;
    if (ownsPool || ownsOperatorPool) {
      try {
        pg = await import("pg");
      } catch (cause) {
        throw new BursarImportError("pg is required for the Bursar runtime: npm install pg", {
          cause,
        });
      }
    }
    let pool: PostgresPool | undefined;
    let operatorPool: PostgresPool | undefined;
    try {
      if ((ownsPool || ownsOperatorPool) && pg === undefined) {
        throw new BursarImportError("pg is required for the Bursar runtime: npm install pg");
      }
      pool = ownsPool
        ? createOwnedPool(
            pg,
            postgresConnection ?? requiredConnectionString(options.postgres),
            options.postgresOptions,
          )
        : providedPool(options.postgres);
      operatorPool = ownsOperatorPool
        ? createOwnedPool(
            pg,
            operatorPostgresConnection ?? requiredConnectionString(options.operatorPostgres),
            options.postgresOptions,
          )
        : providedPool(options.operatorPostgres);
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
          this.clickhouse && supportsUsageHistory(this.clickhouse) ? this.clickhouse : undefined,
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
            !z.number().int().safeParse(row.bursar_reachable).success ||
            row.bursar_reachable !== 1
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
      this.startPromise = this.startRuntime(options).catch((error) => {
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
        ((cause: unknown) =>
          cause instanceof CatalogNotLoadedError || isRetryableBursarError(cause));
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
    const tenantId = z.string().safeParse(resolved?.tenant_id);
    if (!tenantId.success || tenantId.data !== this.tenantId) {
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
  return async (limit) => repository.stats({ limit });
}

type PostgresConnectionInput = string | PostgresPool;

function isPostgresConnectionString(value: PostgresConnectionInput): value is string {
  return z.string().safeParse(value).success;
}

function connectionStringValue(value: PostgresConnectionInput): string | null {
  const parsed = z.string().safeParse(value);
  return parsed.success ? parsed.data : null;
}

function requiredConnectionString(value: PostgresConnectionInput): string {
  const parsed = z.string().min(1).safeParse(value);
  if (!parsed.success) {
    throw new TypeError("an owned PostgreSQL pool requires a connection string");
  }
  return parsed.data;
}

function createOwnedPool(
  pg: typeof import("pg") | undefined,
  connectionString: string,
  options: Omit<PostgresConnectionOptions, "providerEnvironment"> | undefined,
): PostgresPool {
  if (pg === undefined) {
    throw new BursarImportError("pg is required for the Bursar runtime: npm install pg");
  }
  // SAFETY: the runtime's PostgresPool contract is the subset used by Bursar,
  // and the node-postgres Pool is created from the validated connection options above.
  return new pg.Pool(postgresPoolConfig(connectionString, options)) as PostgresPool;
}

function providedPool(value: PostgresConnectionInput): PostgresPool {
  if (isPostgresConnectionString(value)) {
    throw new TypeError("a connection string cannot be used as an external PostgreSQL pool");
  }
  return value;
}

type RuntimeClickhouseStore = UsageEventSink & UsageAnalyticsStore;
type RuntimeClickhouseHistoryStore = RuntimeClickhouseStore & UsageChargeStore;

function supportsUsageHistory(
  value: RuntimeClickhouseStore,
): value is RuntimeClickhouseHistoryStore {
  return z.object({ listUsageCharges: z.function() }).safeParse(value).success;
}

function normalizeTenantSlug(value: string): string {
  const trimmed = z.string().trim().min(1).max(100).safeParse(value);
  if (!trimmed.success) throw new TypeError("tenantSlug must be a valid Bursar tenant slug");
  const normalized = z
    .string()
    .regex(/^[a-z0-9]+(?:[a-z0-9-]*[a-z0-9])?$/u)
    .safeParse(trimmed.data.toLowerCase());
  if (!normalized.success) throw new TypeError("tenantSlug must be a valid Bursar tenant slug");
  return normalized.data;
}

const outboxJsonObjectSchema = z.record(z.string(), z.json());

const usageOutboxPayloadSchema = z
  .object({
    tenant_id: z.string().min(1),
    charge_id: z.string().min(1),
    account_id: z.string().min(1),
    subject_id: z.string().min(1),
    operation: z.string().min(1),
    feature: z.string().nullable().optional(),
    model: z.string().nullable().optional(),
    region: z.string().nullable().optional(),
    measures: outboxJsonObjectSchema,
    dimensions: outboxJsonObjectSchema,
    metadata: outboxJsonObjectSchema,
    requested: z.string().min(1),
    charged: z.string().min(1),
    allowance_requested: z.string().min(1),
    allowance_covered: z.string().min(1),
    billing_disposition: z.enum(["billable", "record_only"]).optional(),
    catalog_revision_id: z.string().nullable().optional(),
    plan_id: z.string().nullable().optional(),
    rate_card_key: z.string().nullable().optional(),
    pricing_snapshot: outboxJsonObjectSchema,
    ledger_entry_id: z.string().nullable().optional(),
    correction_of_charge_id: z.string().nullable().optional(),
    idempotency_key: z.string().min(1),
    request_digest: z.string().min(1),
    event_at: z.string().min(1),
    created_at: z.string().min(1),
  })
  .passthrough();

const billingOutboxPayloadSchema = z
  .object({
    tenant_id: z.string().min(1),
    event_id: z.string().min(1),
    provider: z.string().min(1),
    provider_environment: z.string().min(1),
    provider_event_id: z.string().min(1),
    event_type: z.string().min(1),
    status: z.string().min(1),
    received_at: z.string().min(1),
    completed_at: z.string().nullable().optional(),
    envelope: outboxJsonObjectSchema,
    object_key: z.string().nullable().optional(),
    object_version: z.string().nullable().optional(),
    archived_at: z.string().nullable().optional(),
  })
  .passthrough();

function usageExportFromOutbox(payload: JsonObject): UsageChargeExport | null {
  const parsed = usageOutboxPayloadSchema.safeParse(payload);
  if (!parsed.success) return null;
  const value = parsed.data;
  return {
    tenantId: value.tenant_id,
    chargeId: value.charge_id,
    accountId: value.account_id,
    subjectId: value.subject_id,
    operation: value.operation,
    feature: value.feature ?? null,
    model: value.model ?? null,
    region: value.region ?? null,
    measures: value.measures,
    dimensions: value.dimensions,
    metadata: value.metadata,
    requested: value.requested,
    charged: value.charged,
    allowanceRequested: value.allowance_requested,
    allowanceCovered: value.allowance_covered,
    billingDisposition: value.billing_disposition ?? "billable",
    catalogRevisionId: value.catalog_revision_id ?? null,
    planId: value.plan_id ?? null,
    rateCardKey: value.rate_card_key ?? null,
    pricingSnapshot: value.pricing_snapshot,
    ledgerEntryId: value.ledger_entry_id ?? null,
    correctionOfChargeId: value.correction_of_charge_id ?? null,
    idempotencyKey: value.idempotency_key,
    requestDigest: value.request_digest,
    eventAt: value.event_at,
    createdAt: value.created_at,
  };
}

function billingExportFromOutbox(payload: JsonObject): BillingEventPayloadExport | null {
  const parsed = billingOutboxPayloadSchema.safeParse(payload);
  if (!parsed.success) return null;
  const value = parsed.data;
  return {
    tenantId: value.tenant_id,
    eventId: value.event_id,
    provider: value.provider,
    providerEnvironment: value.provider_environment,
    providerEventId: value.provider_event_id,
    eventType: value.event_type,
    status: value.status,
    receivedAt: value.received_at,
    completedAt: value.completed_at ?? null,
    envelope: value.envelope,
    objectKey: value.object_key ?? null,
    objectVersion: value.object_version ?? null,
    archivedAt: value.archived_at ?? null,
  };
}
