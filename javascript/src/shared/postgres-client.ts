import type { QueryFn } from "./postgres-types.js";

export interface PostgresPool {
  query(text: string, params?: unknown[]): Promise<{ rows: unknown[] }>;
  connect(): Promise<PostgresPoolClient>;
  end(): Promise<void>;
}

export interface PostgresPoolClient {
  query(text: string, params?: unknown[]): Promise<{ rows: unknown[] }>;
  release(): void;
}

export interface PostgresPoolConstructor {
  new (config: { connectionString: string }): PostgresPool;
}

export interface PostgresClientOptions {
  poolConstructor?: PostgresPoolConstructor;
  closedError?: () => Error;
  tenantId?: string;
  usageBackend?: "postgres" | "clickhouse";
  billingPayloadBackend?: "postgres" | "s3";
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
  private readonly usageBackend: "postgres" | "clickhouse";
  private readonly billingPayloadBackend: "postgres" | "s3";
  private closed = false;

  constructor(poolOrUrl: PostgresPool | string, options: PostgresClientOptions = {}) {
    this.databaseUrl = typeof poolOrUrl === "string" ? poolOrUrl : null;
    this.pool = typeof poolOrUrl === "string" ? null : poolOrUrl;
    this.poolConstructor = options.poolConstructor ?? null;
    this.ownsPool = typeof poolOrUrl === "string";
    this.closedError =
      options.closedError ?? (() => new Error("PostgreSQL client has been closed"));
    this.tenantId = options.tenantId ? normalizeTenantId(options.tenantId) : null;
    this.usageBackend = options.usageBackend ?? "postgres";
    this.billingPayloadBackend = options.billingPayloadBackend ?? "postgres";
  }

  readonly query: QueryFn = async (text: string, params?: unknown[]) => {
    const pool = await this.getPool();
    if (!this.tenantId) {
      return (await pool.query(text, params)).rows;
    }

    const client = await pool.connect();
    try {
      await client.query("BEGIN");
      await client.query("SELECT set_config('bursar.tenant_id', $1, true)", [this.tenantId]);
      await client.query("SELECT set_config('bursar.usage_backend', $1, true)", [
        this.usageBackend,
      ]);
      await client.query("SELECT set_config('bursar.billing_payload_backend', $1, true)", [
        this.billingPayloadBackend,
      ]);
      const result = await client.query(text, params);
      await client.query("COMMIT");
      return result.rows;
    } catch (error) {
      await client.query("ROLLBACK");
      throw error;
    } finally {
      client.release();
    }
  };

  async close(): Promise<void> {
    if (this.closed) return;
    this.closed = true;

    if (!this.ownsPool) return;
    const pool = this.poolPromise ? await this.poolPromise : this.pool;
    if (pool) await pool.end();
    this.pool = null;
    this.poolPromise = null;
  }

  private async getPool(): Promise<PostgresPool> {
    if (this.closed) throw this.closedError();
    if (this.pool) return this.pool;
    if (!this.databaseUrl) throw new Error("PostgreSQL client has no connection source");

    if (!this.poolPromise) {
      this.poolPromise = this.createPool(this.databaseUrl);
    }
    return this.poolPromise;
  }

  private async createPool(databaseUrl: string): Promise<PostgresPool> {
    const Pool = await this.getPoolConstructor();
    const pool = new Pool({ connectionString: databaseUrl });
    this.pool = pool;
    return pool;
  }

  private async getPoolConstructor(): Promise<PostgresPoolConstructor> {
    if (this.poolConstructor) return this.poolConstructor;
    const pg = await import("pg");
    this.poolConstructor = pg.Pool as unknown as PostgresPoolConstructor;
    return this.poolConstructor;
  }
}

function normalizeTenantId(tenantId: string): string {
  const normalized = tenantId.trim().toLowerCase();
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(normalized)) {
    throw new TypeError("tenantId must be a UUID");
  }
  return normalized;
}
