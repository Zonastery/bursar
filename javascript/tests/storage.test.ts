import { describe, expect, it, vi } from "vitest";
import type { PostgresPool } from "../src/shared/postgres-client.js";
import { ClickHouseUsageStore, type ClickHouseClient } from "../src/storage/adapters/clickhouse.js";
import { S3BillingArchive } from "../src/storage/adapters/s3.js";
import { OutboxWorker } from "../src/storage/outbox-worker.js";
import type { OutboxEvent, OutboxStore, UsageChargeExport } from "../src/storage/ports.js";
import { createBursarRuntime } from "../src/storage/runtime.js";
import { PricingNotLoadedError, StoreClosedError } from "../src/errors.js";

const TEST_TENANT_ID = "00000000-0000-0000-0000-000000000001";

const s3Mock = vi.hoisted(() => ({
  send: vi.fn(),
  destroy: vi.fn(),
}));

vi.mock("@aws-sdk/client-s3", () => ({
  S3Client: class {
    readonly send = s3Mock.send;
    readonly destroy = s3Mock.destroy;
  },
  PutObjectCommand: class {
    constructor(readonly input: Record<string, unknown>) {}
  },
}));

const outboxEvent: OutboxEvent = {
  eventId: "42",
  tenantId: TEST_TENANT_ID,
  topic: "usage.charge_recorded",
  aggregateType: "credit_usage_charge",
  aggregateId: "00000000-0000-0000-0000-000000000042",
  payloadVersion: 1,
  payload: {},
  claimToken: "00000000-0000-0000-0000-000000000099",
  attemptCount: 2,
  createdAt: "2026-07-29T00:00:00.000Z",
};

function outboxStore(events: OutboxEvent[]): OutboxStore & {
  claim: ReturnType<typeof vi.fn>;
  complete: ReturnType<typeof vi.fn>;
  fail: ReturnType<typeof vi.fn>;
} {
  return {
    claim: vi.fn().mockResolvedValue(events),
    complete: vi.fn().mockResolvedValue(true),
    fail: vi.fn().mockResolvedValue(true),
  };
}

describe("OutboxWorker", () => {
  it("claims only registered topics and acknowledges after delivery", async () => {
    const store = outboxStore([outboxEvent]);
    const handle = vi.fn().mockResolvedValue(undefined);
    const worker = new OutboxWorker(store, [{ topics: ["usage.charge_recorded"], handle }]);

    await expect(worker.runOnce()).resolves.toEqual({ claimed: 1, delivered: 1, failed: 0 });
    expect(store.claim).toHaveBeenCalledWith(["usage.charge_recorded"], 100, 60);
    expect(handle).toHaveBeenCalledWith(outboxEvent);
    expect(store.complete).toHaveBeenCalledWith(outboxEvent);
    expect(store.fail).not.toHaveBeenCalled();
  });

  it("releases failed events with bounded exponential backoff", async () => {
    const store = outboxStore([outboxEvent]);
    const worker = new OutboxWorker(
      store,
      [
        {
          topics: ["usage.charge_recorded"],
          handle: vi.fn().mockRejectedValue(new Error("ClickHouse unavailable")),
        },
      ],
      { retryDelaySeconds: 5, maxRetryDelaySeconds: 20, attemptLimit: 7 },
    );

    await expect(worker.runOnce()).resolves.toEqual({ claimed: 1, delivered: 0, failed: 1 });
    expect(store.complete).not.toHaveBeenCalled();
    expect(store.fail).toHaveBeenCalledWith(outboxEvent, "Error: ClickHouse unavailable", 10, 7);
  });

  it("normalizes failure diagnostics before persistence", async () => {
    const store = outboxStore([outboxEvent]);
    const worker = new OutboxWorker(store, [
      {
        topics: ["usage.charge_recorded"],
        handle: vi.fn().mockRejectedValue(new Error("  failed\0delivery  ")),
      },
    ]);

    await expect(worker.runOnce()).resolves.toEqual({ claimed: 1, delivered: 0, failed: 1 });
    expect(store.fail).toHaveBeenCalledWith(outboxEvent, "Error:   failed\uFFFDdelivery", 60, 10);
  });

  it("isolates scheduler error observers and keeps polling", async () => {
    vi.useFakeTimers();
    try {
      const store = outboxStore([]);
      store.claim.mockRejectedValue(new Error("PostgreSQL unavailable"));
      const onError = vi.fn().mockRejectedValue(new Error("observer failed"));
      const worker = new OutboxWorker(
        store,
        [{ topics: ["usage.charge_recorded"], handle: vi.fn() }],
        { pollIntervalMs: 10, onError },
      );

      await worker.start();
      await vi.runOnlyPendingTimersAsync();

      expect(onError).toHaveBeenCalledWith(
        expect.objectContaining({ message: "PostgreSQL unavailable" }),
      );
      await worker.stop();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("S3BillingArchive", () => {
  it("uses a deterministic object key and preserves the raw envelope", async () => {
    s3Mock.send.mockResolvedValue({ VersionId: "v1" });
    const archive = new S3BillingArchive({
      bucket: "billing-archive",
      region: "us-east-1",
      credentials: {
        accessKeyId: "access-key",
        secretAccessKey: "secret-key",
      },
      prefix: "/tenant-a/",
    });

    await expect(
      archive.archive({
        tenantId: TEST_TENANT_ID,
        eventId: "00000000-0000-0000-0000-000000000001",
        provider: "stripe",
        providerEnvironment: "live",
        providerEventId: "evt_1",
        eventType: "invoice.paid",
        status: "completed",
        receivedAt: "2026-07-29T12:30:00.000Z",
        completedAt: "2026-07-29T12:30:01.000Z",
        envelope: { id: "evt_1", data: { amount: 1200 } },
        objectKey: null,
        objectVersion: null,
        archivedAt: null,
      }),
    ).resolves.toEqual({
      key:
        "tenant-a/tenants/00000000-0000-0000-0000-000000000001/" +
        "billing-events/2026/07/29/00000000-0000-0000-0000-000000000001.json",
      versionId: "v1",
    });

    const command = s3Mock.send.mock.calls[0]?.[0] as {
      input: Record<string, unknown>;
    };
    expect(command.input.Bucket).toBe("billing-archive");
    expect(command.input.ContentType).toBe("application/json");
    const saved = JSON.parse(new TextDecoder().decode(command.input.Body as Uint8Array)) as Record<
      string,
      unknown
    >;
    expect(saved.envelope).toEqual({ id: "evt_1", data: { amount: 1200 } });

    await expect(
      archive.archive({
        tenantId: TEST_TENANT_ID,
        eventId: "00000000-0000-0000-0000-000000000002",
        provider: "stripe",
        providerEnvironment: "live",
        providerEventId: "evt_2",
        eventType: "invoice.paid",
        status: "completed",
        receivedAt: "2026-07-29T12:30:00",
        completedAt: null,
        envelope: { id: "evt_2" },
        objectKey: null,
        objectVersion: null,
        archivedAt: null,
      }),
    ).rejects.toThrow("invalid receivedAt timestamp");

    await archive.close();
    expect(s3Mock.destroy).toHaveBeenCalledOnce();
  });
});

describe("ClickHouseUsageStore", () => {
  it("coalesces initialization and allows recovery after a transient failure", async () => {
    const failure = new Error("ClickHouse unavailable");
    const command = vi.fn().mockRejectedValueOnce(failure).mockResolvedValue(undefined);
    const store = new ClickHouseUsageStore({
      client: {
        command,
        insert: vi.fn().mockResolvedValue(undefined),
        query: vi.fn(),
      },
      tenantId: TEST_TENANT_ID,
    });

    const first = store.initialize();
    const second = store.initialize();
    expect(second).toBe(first);
    await expect(first).rejects.toBe(failure);
    await expect(store.initialize()).resolves.toBeUndefined();
    expect(command).toHaveBeenCalledTimes(2);
  });

  it("writes a replay-safe usage projection and serves analytics", async () => {
    const insert = vi.fn().mockResolvedValue(undefined);
    const query = vi.fn().mockResolvedValue({
      json: async <T>() =>
        [
          { key: "00000000-0000-0000-0000-000000000007", total_spend: "12.5", entry_count: "2" },
        ] as T,
    });
    const client: ClickHouseClient = {
      command: vi.fn().mockResolvedValue(undefined),
      insert,
      query,
    };
    const store = new ClickHouseUsageStore({
      client,
      tenantId: TEST_TENANT_ID,
      createTable: false,
    });
    const usage: UsageChargeExport = {
      tenantId: TEST_TENANT_ID,
      chargeId: "00000000-0000-0000-0000-000000000042",
      accountId: "00000000-0000-0000-0000-000000000006",
      subjectId: "00000000-0000-0000-0000-000000000007",
      operation: "generate",
      feature: "chat",
      model: "gpt",
      region: null,
      measures: { tokens: 10 },
      dimensions: { workspace: "one" },
      metadata: { accountingContext: { source: "openrouter" } },
      requested: "15.000000",
      charged: "12.500000",
      allowanceRequested: "2.500000",
      allowanceCovered: "2.500000",
      billingDisposition: "billable",
      catalogRevisionId: null,
      planId: null,
      rateCardKey: "standard",
      pricingSnapshot: {},
      ledgerEntryId: null,
      correctionOfChargeId: null,
      idempotencyKey: "job:42",
      requestDigest: "\\x1234",
      eventAt: "2026-07-29T12:00:00.000Z",
      createdAt: "2026-07-29T12:00:00.000Z",
    };

    await store.writeUsage(usage, "99");
    expect(insert).toHaveBeenCalledWith(
      expect.objectContaining({
        table: "bursar_usage_events",
        values: [
          expect.objectContaining({
            tenant_id: TEST_TENANT_ID,
            outbox_event_id: "99",
            charge_id: usage.chargeId,
            charged: "12.500000",
            metadata: JSON.stringify(usage.metadata),
          }),
        ],
      }),
    );

    const rows = await store.spendByUser(
      new Date("2026-07-01T00:00:00.000Z"),
      new Date("2026-08-01T00:00:00.000Z"),
    );
    expect(rows[0]?.userId).toBe(usage.subjectId);
    expect(rows[0]?.totalSpend.toString()).toBe("12.5");
    expect(rows[0]?.entryCount).toBe(2);
    expect(query).toHaveBeenCalledWith(
      expect.objectContaining({
        query: expect.stringContaining("billing_disposition = 'billable'"),
      }),
    );

    await expect(
      store.writeUsage({ ...usage, eventAt: "2026-07-29T12:00:00" }, "100"),
    ).rejects.toThrow("Invalid usage timestamp");
  });

  it("preserves UTC and microsecond precision in usage-history cursors", async () => {
    const query = vi.fn().mockResolvedValue({
      json: async <T>() =>
        [
          {
            usage_id: "00000000-0000-0000-0000-000000000042",
            account_id: "00000000-0000-0000-0000-000000000006",
            operation: "generate",
            requested: "15.000000",
            charged: "12.500000",
            allowance_requested: "2.500000",
            allowance_covered: "2.500000",
            feature: "chat",
            model: "gpt",
            region: null,
            event_at: "2026-07-29 12:00:00.123456",
            idempotency_key: "job:42",
            metadata: '{"trace_id":"trace-42"}',
            created_at: "2026-07-29 12:00:00.654321",
          },
          {
            usage_id: "00000000-0000-0000-0000-000000000041",
            account_id: "00000000-0000-0000-0000-000000000006",
            operation: "generate",
            requested: "1.000000",
            charged: "1.000000",
            allowance_requested: "0.000000",
            allowance_covered: "0.000000",
            feature: null,
            model: null,
            region: null,
            event_at: "2026-07-29 12:00:00.123455",
            idempotency_key: "job:41",
            metadata: "{}",
            created_at: "2026-07-29 12:00:00.654320",
          },
        ] as T,
    });
    const store = new ClickHouseUsageStore({
      client: {
        command: vi.fn().mockResolvedValue(undefined),
        insert: vi.fn().mockResolvedValue(undefined),
        query,
      },
      tenantId: TEST_TENANT_ID,
      createTable: false,
    });

    const page = await store.listUsageCharges("00000000-0000-0000-0000-000000000007", {
      limit: 1,
      includeRecordOnly: false,
    });

    expect(page.items[0]).toMatchObject({
      eventAt: "2026-07-29T12:00:00.123456Z",
      createdAt: "2026-07-29T12:00:00.654321Z",
      metadata: { trace_id: "trace-42" },
    });
    expect(page.nextCursor).toEqual({
      eventAt: "2026-07-29T12:00:00.123456Z",
      usageId: "00000000-0000-0000-0000-000000000042",
    });
    expect(query).toHaveBeenCalledWith(
      expect.objectContaining({
        query: expect.stringContaining("billing_disposition = 'billable'"),
        query_params: {
          tenantId: TEST_TENANT_ID,
          subjectId: "00000000-0000-0000-0000-000000000007",
        },
      }),
    );
  });

  it("rejects malformed ClickHouse history timestamps", async () => {
    const query = vi.fn().mockResolvedValue({
      json: async <T>() =>
        [
          {
            usage_id: "00000000-0000-0000-0000-000000000042",
            account_id: "00000000-0000-0000-0000-000000000006",
            operation: "generate",
            requested: "1.000000",
            charged: "1.000000",
            allowance_requested: "0.000000",
            allowance_covered: "0.000000",
            feature: null,
            model: null,
            region: null,
            event_at: "not-a-clickhouse-timestamp",
            idempotency_key: "job:42",
            metadata: "[]",
            created_at: "2026-07-29 12:00:00.000000",
          },
        ] as T,
    });
    const store = new ClickHouseUsageStore({
      client: {
        command: vi.fn().mockResolvedValue(undefined),
        insert: vi.fn().mockResolvedValue(undefined),
        query,
      },
      tenantId: TEST_TENANT_ID,
      createTable: false,
    });

    await expect(store.listUsageCharges("00000000-0000-0000-0000-000000000007")).rejects.toThrow(
      "Invalid ClickHouse timestamp",
    );
  });

  it("rejects syntactically valid but impossible ClickHouse timestamps", async () => {
    const query = vi.fn().mockResolvedValue({
      json: async <T>() =>
        [
          {
            usage_id: "00000000-0000-0000-0000-000000000042",
            account_id: "00000000-0000-0000-0000-000000000006",
            operation: "generate",
            requested: "1.000000",
            charged: "1.000000",
            allowance_requested: "0.000000",
            allowance_covered: "0.000000",
            feature: null,
            model: null,
            region: null,
            event_at: "2026-99-99 12:00:00.000000",
            idempotency_key: "job:42",
            metadata: "{}",
            created_at: "2026-07-29 12:00:00.000000",
          },
        ] as T,
    });
    const store = new ClickHouseUsageStore({
      client: {
        command: vi.fn().mockResolvedValue(undefined),
        insert: vi.fn().mockResolvedValue(undefined),
        query,
      },
      tenantId: TEST_TENANT_ID,
      createTable: false,
    });

    await expect(store.listUsageCharges("00000000-0000-0000-0000-000000000007")).rejects.toThrow(
      "Invalid ClickHouse timestamp",
    );
  });
});

describe("BursarRuntime", () => {
  it("retries a catalog that has not been published yet", async () => {
    const pool: PostgresPool = {
      query: vi.fn().mockResolvedValue({ rows: [] }),
      connect: vi.fn(),
      end: vi.fn().mockResolvedValue(undefined),
    };
    const runtime = await createBursarRuntime({
      postgres: pool,
      tenantId: TEST_TENANT_ID,
    });
    const loadCatalog = vi
      .spyOn(runtime.bursar, "loadCatalog")
      .mockRejectedValueOnce(new PricingNotLoadedError("catalog pending"))
      .mockResolvedValue(undefined);

    await runtime.start({ loadCatalog: true, maxAttempts: 2, retryDelayMs: 0 });

    expect(loadCatalog).toHaveBeenCalledTimes(2);
    expect(runtime.health()).toMatchObject({ started: true, closed: false });
    await runtime.close();
  });

  it("has no background worker or external dependency in PostgreSQL-only mode", async () => {
    const pool: PostgresPool = {
      query: vi.fn().mockResolvedValue({ rows: [] }),
      connect: vi.fn(),
      end: vi.fn().mockResolvedValue(undefined),
    };
    const runtime = await createBursarRuntime({
      postgres: pool,
      tenantId: TEST_TENANT_ID,
    });

    expect(runtime.worker).toBeNull();
    expect(runtime.clickhouse).toBeNull();
    expect(runtime.s3).toBeNull();
    await runtime.start();
    await expect(runtime.flush()).resolves.toEqual({ claimed: 0, delivered: 0, failed: 0 });
    await runtime.close();
    expect(pool.end).not.toHaveBeenCalled();
  });

  it("routes analytics through ClickHouse without changing PostgresStore", async () => {
    const pool: PostgresPool = {
      query: vi.fn().mockResolvedValue({ rows: [] }),
      connect: vi.fn(),
      end: vi.fn().mockResolvedValue(undefined),
    };
    const clickhouseClient: ClickHouseClient = {
      command: vi.fn().mockResolvedValue(undefined),
      insert: vi.fn().mockResolvedValue(undefined),
      query: vi.fn().mockResolvedValue({
        json: async <T>() =>
          [
            { key: "00000000-0000-0000-0000-000000000009", total_spend: "4", entry_count: "1" },
          ] as T,
      }),
    };
    const runtime = await createBursarRuntime({
      postgres: pool,
      tenantId: TEST_TENANT_ID,
      clickhouse: {
        client: clickhouseClient,
        tenantId: TEST_TENANT_ID,
        createTable: false,
      },
      outbox: false,
    });

    const rows = await runtime.bursar.credits.spendByUser(
      new Date("2026-07-01T00:00:00.000Z"),
      new Date("2026-08-01T00:00:00.000Z"),
    );
    expect(rows[0]?.totalSpend.toString()).toBe("4");
    expect(pool.query).not.toHaveBeenCalled();
    await runtime.close();
  });

  it("rejects a ClickHouse tenant mismatch at construction", async () => {
    const pool: PostgresPool = {
      query: vi.fn().mockResolvedValue({ rows: [] }),
      connect: vi.fn(),
      end: vi.fn().mockResolvedValue(undefined),
    };
    const clickhouseClient: ClickHouseClient = {
      command: vi.fn().mockResolvedValue(undefined),
      insert: vi.fn().mockResolvedValue(undefined),
      query: vi.fn().mockResolvedValue({ json: async () => [] }),
    };
    await expect(
      createBursarRuntime({
        postgres: pool,
        tenantId: TEST_TENANT_ID,
        clickhouse: {
          client: clickhouseClient,
          tenantId: "00000000-0000-0000-0000-000000000002",
          createTable: false,
        },
      }),
    ).rejects.toThrow("ClickHouse tenantId must match runtime tenantId");
  });

  it("rejects an empty postgres connection string", async () => {
    await expect(
      createBursarRuntime({ postgres: "   ", tenantId: TEST_TENANT_ID }),
    ).rejects.toThrow(TypeError);
  });

  it("guards against reuse after close and double start", async () => {
    const pool: PostgresPool = {
      query: vi.fn().mockResolvedValue({ rows: [] }),
      connect: vi.fn(),
      end: vi.fn().mockResolvedValue(undefined),
    };
    const runtime = await createBursarRuntime({
      postgres: pool,
      tenantId: TEST_TENANT_ID,
    });
    await runtime.start();
    await runtime.start();
    expect(runtime.health()).toMatchObject({ started: true });

    await runtime.close();
    await runtime.close();
    expect(runtime.health()).toMatchObject({ closed: true, ready: false });
    await expect(runtime.flush()).rejects.toThrow("BursarRuntime has been closed");
    await expect(runtime.start()).resolves.toBeUndefined();
  });

  it("coalesces concurrent lifecycle calls and completes cleanup after a close failure", async () => {
    const initializing = Promise.withResolvers<void>();
    const closing = Promise.withResolvers<void>();
    const pool: PostgresPool = {
      query: vi.fn().mockResolvedValue({ rows: [] }),
      connect: vi.fn(),
      end: vi.fn().mockResolvedValue(undefined),
    };
    const clickhouse = {
      initialize: vi.fn(() => initializing.promise),
      writeUsage: vi.fn().mockResolvedValue(undefined),
      spendByUser: vi.fn().mockResolvedValue([]),
    };
    const s3 = {
      archive: vi.fn().mockResolvedValue({ key: "k", versionId: "v1" }),
      close: vi.fn(() => closing.promise),
    };
    const runtime = await createBursarRuntime({
      postgres: pool,
      tenantId: TEST_TENANT_ID,
      clickhouse,
      s3,
      outbox: false,
    });

    const firstStart = runtime.start();
    const secondStart = runtime.start();
    expect(secondStart).toBe(firstStart);
    expect(clickhouse.initialize).toHaveBeenCalledOnce();
    initializing.resolve();
    await firstStart;

    const firstClose = runtime.close();
    const secondClose = runtime.close();
    expect(secondClose).toBe(firstClose);
    closing.resolve();
    await firstClose;
    expect(s3.close).toHaveBeenCalledOnce();
  });

  it("attempts all runtime cleanup stages when one resource fails to close", async () => {
    const pool: PostgresPool = {
      query: vi.fn().mockResolvedValue({ rows: [] }),
      connect: vi.fn(),
      end: vi.fn().mockResolvedValue(undefined),
    };
    const closeFailure = new Error("S3 close failed");
    const runtime = await createBursarRuntime({
      postgres: pool,
      tenantId: TEST_TENANT_ID,
      s3: {
        archive: vi.fn().mockResolvedValue({ key: "k", versionId: "v1" }),
        close: vi.fn(() => {
          throw closeFailure;
        }),
      },
      outbox: false,
    });

    await expect(runtime.close()).rejects.toBe(closeFailure);
    await expect(runtime.creditStore.getBalance("user-1")).rejects.toBeInstanceOf(StoreClosedError);
  });

  it("rejects start after close before a first start", async () => {
    const pool: PostgresPool = {
      query: vi.fn().mockResolvedValue({ rows: [] }),
      connect: vi.fn(),
      end: vi.fn().mockResolvedValue(undefined),
    };
    const runtime = await createBursarRuntime({
      postgres: pool,
      tenantId: TEST_TENANT_ID,
    });
    await runtime.close();
    await expect(runtime.start()).rejects.toThrow("BursarRuntime has been closed");
  });

  it("validates catalog retry attempts", async () => {
    const pool: PostgresPool = {
      query: vi.fn().mockResolvedValue({ rows: [] }),
      connect: vi.fn(),
      end: vi.fn().mockResolvedValue(undefined),
    };
    const runtime = await createBursarRuntime({
      postgres: pool,
      tenantId: TEST_TENANT_ID,
    });
    await expect(runtime.start({ loadCatalog: true, maxAttempts: 0 })).rejects.toThrow(RangeError);
    await runtime.close();
  });

  it("surfaces a permanent catalog failure without retrying", async () => {
    const pool: PostgresPool = {
      query: vi.fn().mockResolvedValue({ rows: [] }),
      connect: vi.fn(),
      end: vi.fn().mockResolvedValue(undefined),
    };
    const runtime = await createBursarRuntime({
      postgres: pool,
      tenantId: TEST_TENANT_ID,
    });
    const loadCatalog = vi
      .spyOn(runtime.bursar, "loadCatalog")
      .mockRejectedValue(new Error("catalog corrupt"));
    await expect(runtime.start({ loadCatalog: true, maxAttempts: 3 })).rejects.toThrow(
      "catalog corrupt",
    );
    expect(loadCatalog).toHaveBeenCalledOnce();
    await runtime.close();
  });

  it("accepts in-memory sink objects and starts the outbox worker", async () => {
    const pool: PostgresPool = {
      query: vi.fn().mockResolvedValue({ rows: [] }),
      connect: vi.fn(),
      end: vi.fn().mockResolvedValue(undefined),
    };
    const clickhouse = {
      initialize: vi.fn().mockResolvedValue(undefined),
      writeUsage: vi.fn().mockResolvedValue(undefined),
      spendByUser: vi.fn().mockResolvedValue([]),
    };
    const s3 = {
      archive: vi.fn().mockResolvedValue({ key: "k", versionId: "v1" }),
      close: vi.fn().mockResolvedValue(undefined),
    };
    const runtime = await createBursarRuntime({
      postgres: pool,
      tenantId: TEST_TENANT_ID,
      clickhouse,
      s3,
      outbox: {},
      bursar: { commerceOptions: { providers: {} } },
    });

    expect(runtime.clickhouse).toBe(clickhouse);
    expect(runtime.s3).toBe(s3);
    expect(runtime.worker).not.toBeNull();
    await runtime.start();
    expect(runtime.health()).toMatchObject({ started: true });
    await runtime.close();
    expect(clickhouse.initialize).toHaveBeenCalled();
  });
});
