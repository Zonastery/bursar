import { describe, expect, it, vi } from "vitest";

import {
  StoreClosedError,
  StoreError,
  StoreTimeoutError,
  StoreUnavailableError,
} from "../src/errors.js";
import { retryBursarOperation } from "../src/retry.js";
import {
  PostgresClient,
  postgresPoolConfig,
  type PostgresPool,
} from "../src/shared/postgres-client.js";

const TENANT_ID = "00000000-0000-0000-0000-000000000001";

function transactionPool(queryError?: Error, rollbackError?: Error) {
  const release = vi.fn();
  const query = vi.fn(async (text: string) => {
    if (
      text === "BEGIN" ||
      text === "COMMIT" ||
      text.startsWith("SET LOCAL ROLE") ||
      text.startsWith("SELECT set_config(")
    ) {
      return { rows: [] };
    }
    if (text === "ROLLBACK") {
      if (rollbackError) throw rollbackError;
      return { rows: [] };
    }
    if (queryError) throw queryError;
    return { rows: [{ ok: true }] };
  });
  const pool: PostgresPool = {
    query,
    connect: vi.fn().mockResolvedValue({ query, release }),
    end: vi.fn().mockResolvedValue(undefined),
  };
  return { pool, query, release };
}

async function rejectedValue(promise: Promise<unknown>): Promise<unknown> {
  try {
    await promise;
  } catch (error) {
    return error;
  }
  throw new Error("Expected promise to reject");
}

describe("retryBursarOperation", () => {
  it("uses p-retry backoff and reports the next attempt", async () => {
    const operation = vi
      .fn<() => Promise<string>>()
      .mockRejectedValueOnce(new StoreUnavailableError("temporary"))
      .mockResolvedValue("ok");
    const onRetry = vi.fn();

    await expect(
      retryBursarOperation(operation, {
        maxAttempts: 2,
        baseDelayMs: 0,
        jitter: false,
        onRetry,
      }),
    ).resolves.toBe("ok");

    expect(operation).toHaveBeenCalledTimes(2);
    expect(onRetry).toHaveBeenCalledWith(expect.any(StoreUnavailableError), 2, 0);
  });

  it("aborts pending retries with the caller's reason", async () => {
    const controller = new AbortController();
    const reason = new Error("request cancelled");
    const operation = vi.fn().mockRejectedValue(new StoreUnavailableError("temporary"));

    const pending = retryBursarOperation(operation, {
      maxAttempts: 3,
      baseDelayMs: 1_000,
      jitter: false,
      signal: controller.signal,
      onRetry: () => controller.abort(reason),
    });

    await expect(pending).rejects.toBe(reason);
    expect(operation).toHaveBeenCalledOnce();
  });

  it("does not retry unclassified failures and validates retry budgets", async () => {
    const operation = vi.fn().mockRejectedValue(new StoreError("permanent"));
    await expect(
      retryBursarOperation(operation, { maxAttempts: 3, baseDelayMs: 0 }),
    ).rejects.toThrow(StoreError);
    expect(operation).toHaveBeenCalledOnce();

    await expect(retryBursarOperation(async () => "ok", { maxAttempts: 0 })).rejects.toThrow(
      "maxAttempts must be a positive safe integer",
    );
    await expect(
      retryBursarOperation(async () => "ok", { baseDelayMs: 10, maxDelayMs: 5 }),
    ).rejects.toThrow("maxDelayMs must be greater than or equal to baseDelayMs");
    await expect(
      retryBursarOperation(async () => "ok", { jitter: "yes" as never }),
    ).rejects.toThrow("jitter must be a boolean");
  });
});

describe("Postgres reliability boundary", () => {
  it("builds finite, overridable pg pool deadlines", () => {
    expect(postgresPoolConfig("postgresql://localhost/bursar")).toMatchObject({
      connectionString: "postgresql://localhost/bursar",
      connectionTimeoutMillis: 10_000,
      statement_timeout: 30_000,
      query_timeout: 31_000,
      idle_in_transaction_session_timeout: 30_000,
      application_name: "bursar-js",
    });
    expect(
      postgresPoolConfig("postgresql://localhost/bursar", {
        connectionTimeoutMs: 2_000,
        statementTimeoutMs: 5_000,
        idleTransactionTimeoutMs: 7_000,
        idleTimeoutMs: 60_000,
        maxConnections: 20,
        applicationName: "billing-worker",
      }),
    ).toMatchObject({
      connectionTimeoutMillis: 2_000,
      statement_timeout: 5_000,
      query_timeout: 6_000,
      idle_in_transaction_session_timeout: 7_000,
      idleTimeoutMillis: 60_000,
      max: 20,
      application_name: "billing-worker",
    });
    expect(() => postgresPoolConfig(" ")).toThrow(TypeError);
    expect(() =>
      postgresPoolConfig("postgresql://localhost/bursar", { statementTimeoutMs: -1 }),
    ).toThrow(RangeError);
    expect(() =>
      postgresPoolConfig("postgresql://localhost/bursar", { onPoolError: "log" as never }),
    ).toThrow("onPoolError must be a function");
  });

  it("rejects invalid runtime pool and tenancy configuration", () => {
    const { pool } = transactionPool();

    expect(() => new PostgresClient(null as never)).toThrow(
      "postgres pool must provide query(), connect(), and end() methods",
    );
    expect(() => new PostgresClient(pool, { tenantId: "" })).toThrow("tenantId must be a UUID");
    expect(() => new PostgresClient(pool, { usageBackend: "redis" as never })).toThrow(
      "usageBackend must be 'postgres' or 'clickhouse'",
    );
    expect(() => new PostgresClient(pool, { accessRole: "owner" as never })).toThrow(
      "accessRole must be 'bursar_client' or 'bursar_operator'",
    );
  });

  it.each([
    {
      driverError: Object.assign(new Error("socket reset"), { code: "ECONNRESET" }),
      expectedType: StoreUnavailableError,
      retryable: true,
      indeterminate: true,
      detailCode: { networkCode: "ECONNRESET" },
    },
    {
      driverError: Object.assign(new Error("broken pipe"), { code: "EPIPE" }),
      expectedType: StoreUnavailableError,
      retryable: true,
      indeterminate: true,
      detailCode: { networkCode: "EPIPE" },
    },
    {
      driverError: Object.assign(new Error("serialization failure"), { code: "40001" }),
      expectedType: StoreUnavailableError,
      retryable: true,
      indeterminate: false,
      detailCode: { sqlState: "40001" },
    },
    {
      driverError: Object.assign(new Error("canceling statement due to statement timeout"), {
        code: "57014",
      }),
      expectedType: StoreTimeoutError,
      retryable: true,
      indeterminate: false,
      detailCode: { sqlState: "57014" },
    },
    {
      driverError: Object.assign(new Error("duplicate key"), { code: "23505" }),
      expectedType: StoreError,
      retryable: false,
      indeterminate: false,
      detailCode: { sqlState: "23505" },
    },
  ])(
    "classifies pg error $driverError.code without parsing localized messages",
    async ({ driverError, expectedType, retryable, indeterminate, detailCode }) => {
      const { pool, query, release } = transactionPool(driverError);
      const postgres = new PostgresClient(pool, {
        tenantId: TENANT_ID,
        statementTimeoutMs: 1_234,
      });

      const failure = await rejectedValue(postgres.query("SELECT bursar.operation()"));

      expect(failure).toBeInstanceOf(expectedType);
      expect(failure).toMatchObject({
        retryable,
        indeterminate,
        cause: driverError,
        details: {
          datastore: "postgresql",
          operation: "query",
          phase: "query",
          ...detailCode,
        },
      });
      expect(query).toHaveBeenCalledWith(expect.stringContaining("statement_timeout"), [
        "1234",
        "30000",
        TENANT_ID,
        "postgres",
        "postgres",
      ]);
      expect(query).toHaveBeenCalledWith("SET LOCAL ROLE bursar_client");
      expect(query).toHaveBeenCalledWith("ROLLBACK");
      expect(release).toHaveBeenCalledWith(undefined);
    },
  );

  it("classifies pg-pool acquisition timeouts without message leakage", async () => {
    const driverError = new Error("timeout exceeded when trying to connect");
    const { pool } = transactionPool();
    pool.connect = vi.fn().mockRejectedValue(driverError);
    const postgres = new PostgresClient(pool, { tenantId: TENANT_ID });

    const failure = await rejectedValue(postgres.query("SELECT 1"));

    expect(failure).toBeInstanceOf(StoreTimeoutError);
    expect(failure).toMatchObject({
      message: "PostgreSQL query timed out",
      cause: driverError,
      retryable: true,
      indeterminate: false,
      details: { phase: "connect" },
    });
  });

  it("preserves the primary failure when rollback also fails", async () => {
    const primary = Object.assign(new Error("duplicate key"), { code: "23505" });
    const rollback = Object.assign(new Error("connection lost"), { code: "ECONNRESET" });
    const { pool, release } = transactionPool(primary, rollback);
    const postgres = new PostgresClient(pool, { tenantId: TENANT_ID });

    const failure = await rejectedValue(postgres.query("SELECT bursar.operation()"));

    expect(failure).toBeInstanceOf(StoreError);
    expect(failure).toMatchObject({
      cause: primary,
      retryable: false,
      details: { rollbackFailed: true },
    });
    expect(release).toHaveBeenCalledWith(rollback);
  });

  it("surfaces idle pool failures without allowing observers to crash the process", () => {
    let listener: ((error: Error) => void) | undefined;
    const onPoolError = vi.fn(() => {
      throw new Error("observer failed");
    });
    const { pool } = transactionPool();
    pool.on = vi.fn((_event, registered) => {
      listener = registered;
    });

    new PostgresClient(pool, { tenantId: TENANT_ID, onPoolError });
    const driverError = Object.assign(new Error("socket reset"), { code: "ECONNRESET" });

    expect(() => listener?.(driverError)).not.toThrow();
    expect(onPoolError).toHaveBeenCalledWith(expect.any(StoreUnavailableError));
  });

  it("fans idle errors out to active observers sharing a borrowed pool", async () => {
    let listener: ((error: Error) => void) | undefined;
    const { pool } = transactionPool();
    pool.on = vi.fn((_event, registered) => {
      listener = registered;
    });
    const firstObserver = vi.fn();
    const secondObserver = vi.fn();
    const first = new PostgresClient(pool, { tenantId: TENANT_ID, onPoolError: firstObserver });
    const second = new PostgresClient(pool, { tenantId: TENANT_ID, onPoolError: secondObserver });
    const driverError = Object.assign(new Error("socket reset"), { code: "ECONNRESET" });

    listener?.(driverError);
    await first.close();
    listener?.(driverError);

    expect(pool.on).toHaveBeenCalledOnce();
    expect(firstObserver).toHaveBeenCalledOnce();
    expect(secondObserver).toHaveBeenCalledTimes(2);
    await second.close();
  });

  it("reference-counts the same observer across clients sharing a pool", async () => {
    let listener: ((error: Error) => void) | undefined;
    const { pool } = transactionPool();
    pool.on = vi.fn((_event, registered) => {
      listener = registered;
    });
    const sharedObserver = vi.fn();
    const first = new PostgresClient(pool, { tenantId: TENANT_ID, onPoolError: sharedObserver });
    const second = new PostgresClient(pool, { tenantId: TENANT_ID, onPoolError: sharedObserver });
    const driverError = Object.assign(new Error("socket reset"), { code: "ECONNRESET" });

    listener?.(driverError);
    await first.close();
    listener?.(driverError);
    await second.close();
    listener?.(driverError);

    expect(sharedObserver).toHaveBeenCalledTimes(2);
  });

  it("does not turn a post-commit connection-release failure into a duplicate-prone retry", async () => {
    const { pool, release } = transactionPool();
    const releaseError = Object.assign(new Error("release failed"), { code: "ECONNRESET" });
    release.mockImplementation(() => {
      throw releaseError;
    });
    const onPoolError = vi.fn();
    const postgres = new PostgresClient(pool, { tenantId: TENANT_ID, onPoolError });

    await expect(postgres.query("SELECT bursar.operation()")).resolves.toEqual([{ ok: true }]);
    expect(onPoolError).toHaveBeenCalledWith(
      expect.objectContaining({ cause: releaseError, retryable: true }),
    );
  });

  it("rejects calls after close with a typed, non-retryable error", async () => {
    const { pool } = transactionPool();
    const postgres = new PostgresClient(pool, { tenantId: TENANT_ID });
    await postgres.close();

    await expect(postgres.query("SELECT 1")).rejects.toBeInstanceOf(StoreClosedError);
    expect(pool.end).not.toHaveBeenCalled();
  });

  it("coalesces concurrent close calls until an owned pool is drained", async () => {
    const draining = Promise.withResolvers<void>();
    const ownedPools: PostgresPool[] = [];
    class DeferredPool implements PostgresPool {
      readonly query = vi.fn().mockResolvedValue({ rows: [] });
      readonly connect = vi.fn();
      readonly end = vi.fn(() => draining.promise);

      constructor() {
        ownedPools.push(this);
      }
    }
    const postgres = new PostgresClient("postgresql://localhost/bursar", {
      poolConstructor: DeferredPool,
    });
    await postgres.query("SELECT 1");

    const first = postgres.close();
    const second = postgres.close();

    expect(second).toBe(first);
    await Promise.resolve();
    expect(ownedPools[0]?.end).toHaveBeenCalledOnce();
    draining.resolve();
    await first;
  });
});
