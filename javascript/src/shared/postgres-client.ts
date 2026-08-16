import { z } from "zod";

import { type BursarError, ImportError as BursarImportError, StoreClosedError } from "../errors.js";
import { getDefaultInstrumentation, type Instrumentation } from "../telemetry/index.js";
import {
  normalizeProviderEnvironment,
  type ProviderEnvironment,
} from "../providers/environment.js";
import { normalizePostgresError, type PostgresOperationPhase } from "./postgres-errors.js";
import type { QueryFn } from "./postgres-types.js";
import type { PostgresParams, PostgresRow } from "./json.js";

const DEFAULT_CONNECTION_TIMEOUT_MS = 10_000;
const DEFAULT_STATEMENT_TIMEOUT_MS = 30_000;
const DEFAULT_IDLE_TRANSACTION_TIMEOUT_MS = 30_000;
const CLIENT_TIMEOUT_GRACE_MS = 1_000;
const MAX_POSTGRES_TIMEOUT_MS = 2_147_483_647;

export interface PostgresPoolConfig {
  connectionString: string;
  connectionTimeoutMillis?: number;
  statement_timeout?: number;
  query_timeout?: number;
  idle_in_transaction_session_timeout?: number;
  idleTimeoutMillis?: number;
  max?: number;
  application_name?: string;
}

export interface PostgresPool {
  query(text: string, params?: PostgresParams): Promise<{ rows: PostgresRow[] }>;
  connect(): Promise<PostgresPoolClient>;
  end(): Promise<void>;
  on?(event: "error", listener: (error: Error) => void): void;
}

export interface PostgresPoolClient {
  query(text: string, params?: PostgresParams): Promise<{ rows: PostgresRow[] }>;
  release(error?: Error | boolean): void;
}

export interface PostgresPoolConstructor {
  new (config: PostgresPoolConfig): PostgresPool;
}

const postgresPoolSchema = z.object({
  query: z.function(),
  connect: z.function(),
  end: z.function(),
  on: z.function().optional(),
});

export function isPostgresPool<T>(value: T): value is T & PostgresPool {
  try {
    return postgresPoolSchema.safeParse(value).success;
  } catch {
    return false;
  }
}

/** PostgreSQL deadline, pool, and observability controls. */
export interface PostgresConnectionOptions {
  /** Financial provider namespace. Low-level credit-only clients default to `live`. */
  providerEnvironment?: ProviderEnvironment;
  /** Time allowed to establish or acquire a connection. Defaults to 10 seconds. */
  connectionTimeoutMs?: number;
  /** Server-side statement deadline. Defaults to 30 seconds; set 0 to disable. */
  statementTimeoutMs?: number;
  /** Server-side idle transaction deadline. Defaults to 30 seconds; set 0 to disable. */
  idleTransactionTimeoutMs?: number;
  /** Idle pooled connection lifetime. Uses pg's default when omitted. */
  idleTimeoutMs?: number;
  /** Maximum connections in an SDK-owned pool. Uses pg's default when omitted. */
  maxConnections?: number;
  /** PostgreSQL application_name. Defaults to `bursar-js`. */
  applicationName?: string;
  /** Receives typed errors emitted by idle pool clients. Must not throw. */
  onPoolError?: (error: BursarError) => void;
  /** Optional vendor-neutral instrumentation. Defaults to Bursar's no-op registry. */
  instrumentation?: Instrumentation;
}

export interface PostgresClientOptions extends PostgresConnectionOptions {
  poolConstructor?: PostgresPoolConstructor;
  closedError?: () => Error;
  tenantId?: string;
  accessRole?: PostgresAccessRole;
  usageBackend?: "postgres" | "clickhouse";
  billingPayloadBackend?: "postgres" | "s3";
}

export type PostgresAccessRole = "bursar_client" | "bursar_operator";

interface NormalizedPostgresConnectionOptions {
  providerEnvironment: ProviderEnvironment;
  connectionTimeoutMs: number;
  statementTimeoutMs: number;
  idleTransactionTimeoutMs: number;
  idleTimeoutMs?: number;
  maxConnections?: number;
  applicationName: string;
  onPoolError?: (error: BursarError) => void;
  instrumentation: Instrumentation;
}

function integerOption(
  value: number | undefined,
  fallback: number | undefined,
  name: string,
  minimum: number,
  maximum = MAX_POSTGRES_TIMEOUT_MS,
): number | undefined {
  const normalized = value ?? fallback;
  if (normalized === undefined) return undefined;
  if (!Number.isSafeInteger(normalized) || normalized < minimum || normalized > maximum) {
    throw new RangeError(`${name} must be a safe integer between ${minimum} and ${maximum}`);
  }
  return normalized;
}

function normalizeConnectionOptions(
  options: PostgresConnectionOptions,
): NormalizedPostgresConnectionOptions {
  if (
    options.applicationName !== undefined &&
    !z.string().safeParse(options.applicationName).success
  ) {
    throw new TypeError("applicationName must be a string");
  }
  if (options.onPoolError !== undefined && !z.function().safeParse(options.onPoolError).success) {
    throw new TypeError("onPoolError must be a function");
  }
  const instrumentation = options.instrumentation ?? getDefaultInstrumentation();
  if (!z.object({ run: z.function() }).safeParse(instrumentation).success) {
    throw new TypeError("instrumentation must provide run()");
  }
  const applicationName = options.applicationName?.trim() || "bursar-js";
  if (applicationName.includes("\0")) {
    throw new TypeError("applicationName must not contain null bytes");
  }
  return {
    providerEnvironment: normalizeProviderEnvironment(options.providerEnvironment ?? "live"),
    connectionTimeoutMs: integerOption(
      options.connectionTimeoutMs,
      DEFAULT_CONNECTION_TIMEOUT_MS,
      "connectionTimeoutMs",
      0,
    )!,
    statementTimeoutMs: integerOption(
      options.statementTimeoutMs,
      DEFAULT_STATEMENT_TIMEOUT_MS,
      "statementTimeoutMs",
      0,
    )!,
    idleTransactionTimeoutMs: integerOption(
      options.idleTransactionTimeoutMs,
      DEFAULT_IDLE_TRANSACTION_TIMEOUT_MS,
      "idleTransactionTimeoutMs",
      0,
    )!,
    idleTimeoutMs: integerOption(options.idleTimeoutMs, undefined, "idleTimeoutMs", 0),
    maxConnections: integerOption(options.maxConnections, undefined, "maxConnections", 1),
    applicationName,
    onPoolError: options.onPoolError,
    instrumentation,
  };
}

function postgresTelemetryOperation(text: string): "postgres.query" | "postgres.rpc" {
  return /^\s*select\s+\*\s+from\s+bursar\.[a-z_][a-z0-9_]*\s*\(/i.test(text)
    ? "postgres.rpc"
    : "postgres.query";
}

/** Build a pg Pool config with finite connection and statement deadlines. */
export function postgresPoolConfig(
  connectionString: string,
  options: PostgresConnectionOptions = {},
): PostgresPoolConfig {
  const parsedConnectionString = z.string().safeParse(connectionString);
  if (!parsedConnectionString.success) {
    throw new TypeError("postgres connection string must be a string");
  }
  if (!parsedConnectionString.data.trim()) {
    throw new TypeError("postgres connection string must not be empty");
  }
  const normalized = normalizeConnectionOptions(options);
  const queryTimeout =
    normalized.statementTimeoutMs === 0
      ? 0
      : Math.min(normalized.statementTimeoutMs + CLIENT_TIMEOUT_GRACE_MS, MAX_POSTGRES_TIMEOUT_MS);
  const config: PostgresPoolConfig = {
    connectionString: parsedConnectionString.data,
    connectionTimeoutMillis: normalized.connectionTimeoutMs,
    statement_timeout: normalized.statementTimeoutMs,
    query_timeout: queryTimeout,
    idle_in_transaction_session_timeout: normalized.idleTransactionTimeoutMs,
    application_name: normalized.applicationName,
  };
  if (normalized.idleTimeoutMs !== undefined) config.idleTimeoutMillis = normalized.idleTimeoutMs;
  if (normalized.maxConnections !== undefined) config.max = normalized.maxConnections;
  return config;
}

type PoolErrorObserver = (error: BursarError) => void;

const poolObservers = new WeakMap<object, Map<PoolErrorObserver, number>>();

function notifyPoolError(
  onPoolError: ((error: BursarError) => void) | undefined,
  error: BursarError,
): void {
  try {
    onPoolError?.(error);
  } catch {
    // Observability callbacks must never destabilize SDK control flow.
  }
}

function observePool(pool: PostgresPool, onPoolError: PoolErrorObserver | undefined): () => void {
  if (!pool.on) return () => {};
  const existing = poolObservers.get(pool);
  if (existing) {
    if (onPoolError) existing.set(onPoolError, (existing.get(onPoolError) ?? 0) + 1);
    return () => {
      if (!onPoolError) return;
      const references = existing.get(onPoolError) ?? 0;
      if (references <= 1) existing.delete(onPoolError);
      else existing.set(onPoolError, references - 1);
    };
  }

  const observers = new Map<PoolErrorObserver, number>();
  if (onPoolError) observers.set(onPoolError, 1);
  poolObservers.set(pool, observers);
  pool.on("error", (cause) => {
    const normalized = normalizePostgresError(cause, { operation: "pool", phase: "pool" });
    for (const observer of observers.keys()) notifyPoolError(observer, normalized);
  });
  return () => {
    if (!onPoolError) return;
    const references = observers.get(onPoolError) ?? 0;
    if (references <= 1) observers.delete(onPoolError);
    else observers.set(onPoolError, references - 1);
  };
}

/**
 * Owns the lifecycle and lazy initialization of a PostgreSQL pool.
 *
 * A supplied pool is borrowed and therefore never ended. A connection string
 * creates an owned pool on first use and closes it exactly once.
 */
export class PostgresClient {
  private pool: PostgresPool | null;
  private poolPromise: Promise<PostgresPool> | null = null;
  private poolConstructor: PostgresPoolConstructor | null;
  private readonly databaseUrl: string | null;
  private readonly ownsPool: boolean;
  private readonly closedError: () => Error;
  private readonly tenantId: string | null;
  private readonly accessRole: PostgresAccessRole | null;
  private readonly usageBackend: "postgres" | "clickhouse";
  private readonly billingPayloadBackend: "postgres" | "s3";
  private readonly providerEnvironment: ProviderEnvironment;
  private readonly connectionOptions: NormalizedPostgresConnectionOptions;
  private removePoolObserver: (() => void) | null = null;
  private closePromise: Promise<void> | null = null;
  private closed = false;

  constructor(poolOrUrl: PostgresPool | string, options: PostgresClientOptions = {}) {
    const suppliedPool = isPostgresPool(poolOrUrl);
    const parsedDatabaseUrl = z.string().safeParse(poolOrUrl);
    if (!suppliedPool && !parsedDatabaseUrl.success) {
      throw new TypeError("postgres pool must provide query(), connect(), and end() methods");
    }
    if (
      options.poolConstructor !== undefined &&
      !z.function().safeParse(options.poolConstructor).success
    ) {
      throw new TypeError("poolConstructor must be a constructor");
    }
    if (options.closedError !== undefined && !z.function().safeParse(options.closedError).success) {
      throw new TypeError("closedError must be a function");
    }
    if (
      options.accessRole !== undefined &&
      options.accessRole !== "bursar_client" &&
      options.accessRole !== "bursar_operator"
    ) {
      throw new TypeError("accessRole must be 'bursar_client' or 'bursar_operator'");
    }
    if (
      options.usageBackend !== undefined &&
      options.usageBackend !== "postgres" &&
      options.usageBackend !== "clickhouse"
    ) {
      throw new TypeError("usageBackend must be 'postgres' or 'clickhouse'");
    }
    if (
      options.billingPayloadBackend !== undefined &&
      options.billingPayloadBackend !== "postgres" &&
      options.billingPayloadBackend !== "s3"
    ) {
      throw new TypeError("billingPayloadBackend must be 'postgres' or 's3'");
    }
    this.connectionOptions = normalizeConnectionOptions(options);
    const databaseUrl = parsedDatabaseUrl.success ? parsedDatabaseUrl.data : null;
    this.databaseUrl = suppliedPool ? null : databaseUrl;
    if (this.databaseUrl !== null && !this.databaseUrl.trim()) {
      throw new TypeError("postgres connection string must not be empty");
    }
    this.pool = suppliedPool ? poolOrUrl : null;
    this.poolConstructor = options.poolConstructor ?? null;
    this.ownsPool = !suppliedPool;
    this.closedError =
      options.closedError ?? (() => new StoreClosedError("PostgreSQL client has been closed"));
    this.tenantId = options.tenantId === undefined ? null : normalizeTenantId(options.tenantId);
    this.accessRole = options.accessRole ?? (this.tenantId ? "bursar_client" : null);
    this.usageBackend = options.usageBackend ?? "postgres";
    this.billingPayloadBackend = options.billingPayloadBackend ?? "postgres";
    this.providerEnvironment = this.connectionOptions.providerEnvironment;
    if (this.pool) {
      this.removePoolObserver = observePool(this.pool, this.connectionOptions.onPoolError);
    }
  }

  readonly query: QueryFn = (text: string, params?: PostgresParams) =>
    this.connectionOptions.instrumentation.run(
      postgresTelemetryOperation(text),
      { "bursar.backend": "postgres" },
      async () => {
        let pool: PostgresPool;
        try {
          pool = await this.getPool();
        } catch (cause) {
          if (this.closed) throw cause;
          throw normalizePostgresError(cause, {
            operation: "query",
            phase: "connect",
          });
        }

        if (!this.tenantId && !this.accessRole) {
          try {
            return (await pool.query(text, params)).rows;
          } catch (cause) {
            throw normalizePostgresError(cause, {
              operation: "query",
              phase: "query",
              indeterminate: true,
            });
          }
        }

        let client: PostgresPoolClient;
        try {
          client = await pool.connect();
        } catch (cause) {
          throw normalizePostgresError(cause, {
            operation: "query",
            phase: "connect",
          });
        }

        let phase: PostgresOperationPhase = "begin";
        let transactionStarted = false;
        let discardConnection: Error | undefined;
        try {
          await client.query("BEGIN");
          transactionStarted = true;
          phase = "configure";
          if (this.accessRole) await client.query(`SET LOCAL ROLE ${this.accessRole}`);
          const settings = [
            `set_config('statement_timeout', $1, true)`,
            `set_config('idle_in_transaction_session_timeout', $2, true)`,
          ];
          const values: PostgresParams = [
            String(this.connectionOptions.statementTimeoutMs),
            String(this.connectionOptions.idleTransactionTimeoutMs),
          ];
          if (this.tenantId) {
            settings.push(
              `set_config('bursar.tenant_id', $3, true)`,
              `set_config('bursar.usage_backend', $4, true)`,
              `set_config('bursar.billing_payload_backend', $5, true)`,
              `set_config('bursar.provider_environment', $6, true)`,
            );
            values.push(
              this.tenantId,
              this.usageBackend,
              this.billingPayloadBackend,
              this.providerEnvironment,
            );
          }
          await client.query(`SELECT ${settings.join(", ")}`, values);
          phase = "query";
          const result = await client.query(text, params);
          phase = "commit";
          await client.query("COMMIT");
          transactionStarted = false;
          return result.rows;
        } catch (cause) {
          const failedPhase = phase;
          let rollbackFailed = false;
          if (transactionStarted) {
            try {
              phase = "rollback";
              await client.query("ROLLBACK");
              transactionStarted = false;
            } catch (rollbackError) {
              rollbackFailed = true;
              discardConnection =
                rollbackError instanceof Error
                  ? rollbackError
                  : new Error("PostgreSQL rollback failed", { cause: rollbackError });
            }
          }
          const normalized = normalizePostgresError(cause, {
            operation: "query",
            phase: failedPhase,
            indeterminate: failedPhase === "query" || failedPhase === "commit",
            rollbackFailed,
          });
          throw normalized;
        } finally {
          try {
            client.release(discardConnection);
          } catch (cause) {
            const normalized = normalizePostgresError(cause, {
              operation: "release connection",
              phase: "pool",
            });
            notifyPoolError(this.connectionOptions.onPoolError, normalized);
          }
        }
      },
    );

  close(): Promise<void> {
    if (this.closePromise) return this.closePromise;
    this.closed = true;
    this.closePromise = this.closePool();
    return this.closePromise;
  }

  private async closePool(): Promise<void> {
    if (!this.ownsPool) {
      this.removePoolObserver?.();
      this.removePoolObserver = null;
      return;
    }
    try {
      const pool = this.poolPromise ? await this.poolPromise : this.pool;
      if (pool) await pool.end();
    } catch (cause) {
      throw normalizePostgresError(cause, {
        operation: "close",
        phase: "close",
      });
    } finally {
      this.pool = null;
      this.poolPromise = null;
      this.removePoolObserver?.();
      this.removePoolObserver = null;
    }
  }

  private async getPool(): Promise<PostgresPool> {
    if (this.closed) throw this.closedError();
    if (this.pool) return this.pool;
    if (!this.databaseUrl) throw new StoreClosedError("PostgreSQL client has no connection source");

    if (!this.poolPromise) {
      this.poolPromise = this.createPool(this.databaseUrl).catch((error) => {
        this.poolPromise = null;
        throw error;
      });
    }
    return this.poolPromise;
  }

  private async createPool(databaseUrl: string): Promise<PostgresPool> {
    const Pool = await this.getPoolConstructor();
    const pool = new Pool(postgresPoolConfig(databaseUrl, this.connectionOptions));
    this.removePoolObserver = observePool(pool, this.connectionOptions.onPoolError);
    this.pool = pool;
    return pool;
  }

  private async getPoolConstructor(): Promise<PostgresPoolConstructor> {
    if (this.poolConstructor) return this.poolConstructor;
    let pg: typeof import("pg");
    try {
      pg = await import("pg");
    } catch (cause) {
      throw new BursarImportError("pg is required for PostgreSQL stores: npm install pg", {
        cause,
      });
    }
    // SAFETY: The `pg` package exports Pool with the constructor shape required by this adapter.
    this.poolConstructor = pg.Pool as PostgresPoolConstructor;
    return this.poolConstructor;
  }
}

/** Normalize and validate a tenant UUID at SDK composition boundaries. */
export function normalizeTenantId(tenantId: string): string {
  const parsed = z.string().safeParse(tenantId);
  if (!parsed.success) throw new TypeError("tenantId must be a UUID");
  const normalized = parsed.data.trim().toLowerCase();
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(normalized)) {
    throw new TypeError("tenantId must be a UUID");
  }
  return normalized;
}
