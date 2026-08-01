import { Bursar, type BursarOptions } from "../bursar.js";
import { isRetryableBursarError, PricingNotLoadedError } from "../errors.js";
import { PostgresBillingStore } from "../billing/postgres/store.js";
import { PostgresStore } from "../credits/postgres/store.js";
import type { CreditsServiceOptions } from "../credits/service.js";
import type { UsageAnalyticsStore, UsageChargeStore } from "../credits/types/index.js";
import { PostgresClient, type PostgresPool } from "../shared/postgres-client.js";
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

type RuntimeBursarOptions = Omit<
  BursarOptions,
  "creditStore" | "billingStore" | "credits" | "creditsOptions"
> & {
  creditsOptions?: Omit<CreditsServiceOptions, "analytics"> | null;
};

export interface BursarRuntimeOptions {
  postgres: string | PostgresPool;
  tenantId: string;
  s3?: BillingPayloadArchive | S3BillingArchiveOptions | null;
  clickhouse?: (UsageEventSink & UsageAnalyticsStore) | ClickHouseUsageStoreOptions | null;
  /**
   * Background delivery configuration. Set false only when another process
   * consumes Bursar's outbox.
   */
  outbox?: OutboxWorkerOptions | false;
  bursar?: RuntimeBursarOptions;
}

export interface BursarRuntimeStartOptions {
  /** Load the active catalog before starting background workers. */
  loadCatalog?: boolean;
  /** Total catalog-load attempts, including the first. Defaults to 1. */
  maxAttempts?: number;
  /** Initial retry delay. Each retry doubles up to 5 seconds. */
  retryDelayMs?: number;
  shouldRetry?: (error: unknown) => boolean;
}

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
  private started = false;
  private closed = false;

  constructor(
    pool: PostgresPool,
    ownsPool: boolean,
    options: Omit<BursarRuntimeOptions, "postgres">,
  ) {
    this.pool = pool;
    this.ownsPool = ownsPool;
    if (
      options.clickhouse &&
      !("writeUsage" in options.clickhouse) &&
      options.clickhouse.tenantId !== options.tenantId
    ) {
      throw new Error("ClickHouse tenantId must match runtime tenantId");
    }
    this.clickhouse = options.clickhouse
      ? "writeUsage" in options.clickhouse
        ? options.clickhouse
        : new ClickHouseUsageStore(options.clickhouse)
      : null;
    this.s3 = options.s3
      ? "archive" in options.s3
        ? options.s3
        : new S3BillingArchive(options.s3)
      : null;

    this.creditStore = new PostgresStore("", options.tenantId, pool, {
      usageBackend: this.clickhouse ? "clickhouse" : "postgres",
    });
    this.billingStore = new PostgresBillingStore(pool as import("pg").Pool, options.tenantId, {
      billingPayloadBackend: this.s3 ? "s3" : "postgres",
    });
    const bursarOptions = options.bursar ?? {};
    const commerceOptions = bursarOptions.commerceOptions
      ? {
          ...bursarOptions.commerceOptions,
          tenantId: options.tenantId,
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

    const query: QueryFn = new PostgresClient(pool, {
      tenantId: options.tenantId,
      usageBackend: this.clickhouse ? "clickhouse" : "postgres",
      billingPayloadBackend: this.s3 ? "s3" : "postgres",
    }).query;
    const repository = new PostgresStorageRepository(query, options.tenantId);
    const handlers = this.createHandlers(repository);
    this.worker =
      handlers.length > 0 && options.outbox !== false
        ? new OutboxWorker(repository, handlers, options.outbox)
        : null;
  }

  async start(options: BursarRuntimeStartOptions = {}): Promise<void> {
    if (this.started) return;
    if (this.closed) throw new Error("BursarRuntime has been closed");
    if (options.loadCatalog) {
      const maxAttempts = options.maxAttempts ?? 1;
      if (!Number.isInteger(maxAttempts) || maxAttempts < 1) {
        throw new RangeError("maxAttempts must be a positive integer");
      }
      const shouldRetry =
        options.shouldRetry ??
        ((error: unknown) =>
          error instanceof PricingNotLoadedError || isRetryableBursarError(error));
      let attempt = 0;
      for (;;) {
        attempt += 1;
        try {
          await this.bursar.loadCatalog();
          break;
        } catch (error) {
          if (attempt >= maxAttempts || !shouldRetry(error)) throw error;
          const delay = Math.min((options.retryDelayMs ?? 250) * 2 ** (attempt - 1), 5_000);
          await new Promise((resolve) => setTimeout(resolve, delay));
        }
      }
    }
    await this.clickhouse?.initialize?.();
    await this.worker?.start();
    this.started = true;
  }

  health(): BursarRuntimeHealth {
    const catalogLoaded = this.bursar.credits.pricingEngine != null;
    return {
      ready: this.started && !this.closed && catalogLoaded,
      started: this.started,
      closed: this.closed,
      catalogLoaded,
    };
  }

  async flush(): Promise<OutboxRunResult> {
    if (this.closed) throw new Error("BursarRuntime has been closed");
    return this.worker?.runOnce() ?? { claimed: 0, delivered: 0, failed: 0 };
  }

  async close(): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    await this.worker?.stop();
    await this.s3?.close?.();
    await Promise.all([this.creditStore.close(), this.billingStore.close()]);
    if (this.ownsPool) await this.pool.end();
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
            this.s3?.purgePostgresPayload ?? true,
          );
          if (!recorded) {
            throw new Error(`Could not record archive pointer for billing event ${event.eventId}`);
          }
        },
      });
    }
    return handlers;
  }
}

export async function createBursarRuntime(options: BursarRuntimeOptions): Promise<BursarRuntime> {
  if (typeof options.postgres !== "string") {
    return new BursarRuntime(options.postgres, false, options);
  }
  if (!options.postgres.trim()) throw new TypeError("postgres connection string must not be empty");
  const pg = await import("pg");
  const pool = new pg.Pool({ connectionString: options.postgres }) as unknown as PostgresPool;
  return new BursarRuntime(pool, true, options);
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
