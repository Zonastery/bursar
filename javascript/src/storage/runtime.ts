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
import { OutboxWorker, type OutboxRunResult, type OutboxWorkerOptions } from "./outbox-worker.js";
import type {
  BillingEventPayloadExport,
  BillingPayloadArchive,
  OutboxHandler,
  UsageChargeExport,
  UsageEventSink,
} from "./ports.js";
import { PostgresStorageRepository } from "./postgres-repository.js";

/** Facade configuration accepted by the composed Node.js runtime. */
export interface BursarRuntimeBursarOptions {
  creditsOptions?: Omit<CreditsServiceOptions, "analytics"> | null;
  billingOptions?: BillingServiceOptions | null;
  commerceOptions?: CommerceOptions | null;
  emitter?: CreditEventEmitter | null;
}

export interface BursarRuntimeOptions {
  postgres: string | PostgresPool;
  /** Applied to SDK-owned pools and per-transaction statement deadlines. */
  postgresOptions?: PostgresConnectionOptions;
  tenantId: string;
  /**
   * Optional provisioned tenant slug. When supplied, startup verifies that it
   * resolves to `tenantId` before any worker or catalog lifecycle begins.
   */
  tenantSlug?: string;
  s3?: BillingPayloadArchive | S3BillingArchiveOptions | null;
  clickhouse?: (UsageEventSink & UsageAnalyticsStore) | ClickHouseUsageStoreOptions | null;
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
      .custom<
        (error: unknown) => boolean
      >((value) => typeof value === "function", "shouldRetry must be a function")
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
  started: boolean;
  closed: boolean;
  catalogLoaded: boolean;
}

/**
 * Node composition root for Bursar's stores and optional data infrastructure.
 *
 * Without S3 or ClickHouse it constructs the same PostgreSQL-only Bursar and
 * no outbox polling loop. All components share one PostgreSQL pool.
 */
export class BursarRuntime {
  readonly bursar: Bursar;
  readonly creditStore: PostgresStore;
  readonly billingStore: PostgresBillingStore;
  readonly worker: OutboxWorker | null;
  readonly clickhouse: (UsageEventSink & UsageAnalyticsStore) | null;
  readonly s3: BillingPayloadArchive | null;

  private readonly pool: PostgresPool;
  private readonly ownsPool: boolean;
  private readonly postgres: PostgresClient;
  private readonly query: QueryFn;
  private readonly tenantId: string;
  private readonly tenantSlug: string | null;
  private startPromise: Promise<void> | null = null;
  private closePromise: Promise<void> | null = null;
  private started = false;
  private closed = false;

  /** Construct a runtime while keeping pool ownership inside the SDK. */
  static async create(options: BursarRuntimeOptions): Promise<BursarRuntime> {
    if (typeof options.postgres !== "string") {
      return new BursarRuntime(options.postgres, false, options);
    }
    if (!options.postgres.trim()) {
      throw new TypeError("postgres connection string must not be empty");
    }
    let pg: typeof import("pg");
    try {
      pg = await import("pg");
    } catch (cause) {
      throw new BursarImportError("pg is required for the Bursar runtime: npm install pg", {
        cause,
      });
    }
    const pool = new pg.Pool(
      postgresPoolConfig(options.postgres, options.postgresOptions),
    ) as PostgresPool;
    try {
      return new BursarRuntime(pool, true, options);
    } catch (error) {
      try {
        await pool.end();
      } catch {
        // Preserve the composition error; no runtime exists to report cleanup.
      }
      throw error;
    }
  }

  private constructor(
    pool: PostgresPool,
    ownsPool: boolean,
    options: Omit<BursarRuntimeOptions, "postgres">,
  ) {
    this.pool = pool;
    this.ownsPool = ownsPool;
    this.tenantId = normalizeTenantId(options.tenantId);
    this.tenantSlug =
      options.tenantSlug === undefined ? null : normalizeTenantSlug(options.tenantSlug);
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
    this.s3 = options.s3
      ? "archive" in options.s3
        ? options.s3
        : new S3BillingArchive(options.s3)
      : null;

    this.creditStore = new PostgresStore({
      postgres: pool,
      tenantId: this.tenantId,
      ...(options.postgresOptions ?? {}),
      usageBackend: this.clickhouse ? "clickhouse" : "postgres",
    });
    this.billingStore = new PostgresBillingStore({
      postgres: pool,
      tenantId: this.tenantId,
      ...(options.postgresOptions ?? {}),
      billingPayloadBackend: this.s3 ? "s3" : "postgres",
    });
    const bursarOptions = options.bursar ?? {};
    const commerceOptions = bursarOptions.commerceOptions
      ? {
          ...bursarOptions.commerceOptions,
          tenantId: this.tenantId,
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

    this.postgres = new PostgresClient(pool, {
      ...(options.postgresOptions ?? {}),
      tenantId: this.tenantId,
      accessRole: "bursar_operator",
      usageBackend: this.clickhouse ? "clickhouse" : "postgres",
      billingPayloadBackend: this.s3 ? "s3" : "postgres",
    });
    this.query = this.postgres.query;
    const repository = new PostgresStorageRepository(this.query, this.tenantId);
    const handlers = this.createHandlers(repository);
    this.worker =
      handlers.length > 0 && options.outbox !== false
        ? new OutboxWorker(repository, handlers, options.outbox)
        : null;
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
    await this.worker?.start();
    this.started = true;
  }

  health(): BursarRuntimeHealth {
    const catalogLoaded = this.bursar.catalog.isLoaded;
    return {
      ready: this.started && !this.closed && catalogLoaded,
      started: this.started,
      closed: this.closed,
      catalogLoaded,
    };
  }

  async flush(): Promise<OutboxRunResult> {
    if (this.closed) throw new StoreClosedError("BursarRuntime has been closed");
    return this.worker?.runOnce() ?? { claimed: 0, delivered: 0, failed: 0 };
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
          await this.clickhouse?.writeUsage(usage, outboxEvent.eventId);
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
