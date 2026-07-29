import { Bursar, type BursarOptions } from "../bursar.js";
import { PostgresBillingStore } from "../billing/postgres/store.js";
import { PostgresStore } from "../credits/postgres/store.js";
import type { CreditsServiceOptions } from "../credits/service.js";
import type { UsageAnalyticsStore } from "../credits/types/index.js";
import type { PostgresPool } from "../shared/postgres-client.js";
import type { QueryFn } from "../shared/postgres-types.js";
import { ClickHouseUsageStore, type ClickHouseUsageStoreOptions } from "./adapters/clickhouse.js";
import { S3BillingArchive, type S3BillingArchiveOptions } from "./adapters/s3.js";
import { OutboxWorker, type OutboxRunResult, type OutboxWorkerOptions } from "./outbox-worker.js";
import type { BillingPayloadArchive, OutboxHandler, UsageEventSink } from "./ports.js";
import { PostgresStorageRepository } from "./postgres-repository.js";

type RuntimeBursarOptions = Omit<
  BursarOptions,
  "creditStore" | "billingStore" | "credits" | "creditsOptions"
> & {
  creditsOptions?: Omit<CreditsServiceOptions, "analytics"> | null;
};

export interface BursarRuntimeOptions {
  postgres: string | PostgresPool;
  s3?: BillingPayloadArchive | S3BillingArchiveOptions | null;
  clickhouse?: (UsageEventSink & UsageAnalyticsStore) | ClickHouseUsageStoreOptions | null;
  /**
   * Background delivery configuration. Set false only when another process
   * consumes Bursar's outbox.
   */
  outbox?: OutboxWorkerOptions | false;
  bursar?: RuntimeBursarOptions;
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

    this.creditStore = new PostgresStore("", pool);
    this.billingStore = new PostgresBillingStore(pool as import("pg").Pool);
    const bursarOptions = options.bursar ?? {};
    this.bursar = new Bursar({
      ...bursarOptions,
      creditStore: this.creditStore,
      billingStore: this.billingStore,
      creditsOptions: {
        ...(bursarOptions.creditsOptions ?? {}),
        analytics: this.clickhouse ?? undefined,
      },
    });

    const query: QueryFn = async (text, params) => (await pool.query(text, params)).rows;
    const repository = new PostgresStorageRepository(query);
    const handlers = this.createHandlers(repository);
    this.worker =
      handlers.length > 0 && options.outbox !== false
        ? new OutboxWorker(repository, handlers, options.outbox)
        : null;
  }

  async start(): Promise<void> {
    if (this.started) return;
    if (this.closed) throw new Error("BursarRuntime has been closed");
    await this.clickhouse?.initialize?.();
    await this.worker?.start();
    this.started = true;
  }

  async flush(): Promise<OutboxRunResult> {
    if (this.closed) throw new Error("BursarRuntime has been closed");
    return this.worker?.runOnce() ?? { claimed: 0, delivered: 0, failed: 0 };
  }

  async close(): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    await this.worker?.stop();
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
          const usage = await repository.getUsageCharge(outboxEvent.aggregateId);
          if (!usage) {
            throw new Error(`Usage charge ${outboxEvent.aggregateId} is unavailable for export`);
          }
          await this.clickhouse?.writeUsage(usage, outboxEvent.eventId);
        },
      });
    }
    if (this.s3) {
      handlers.push({
        topics: ["billing.webhook_completed"],
        handle: async (outboxEvent) => {
          if (outboxEvent.payloadVersion !== 1) {
            throw new Error(
              `Unsupported billing outbox payload version ${outboxEvent.payloadVersion}`,
            );
          }
          const event = await repository.getBillingEventPayload(outboxEvent.aggregateId);
          if (!event) {
            throw new Error(`Billing event ${outboxEvent.aggregateId} is unavailable for archive`);
          }
          if (event.objectKey) return;
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
