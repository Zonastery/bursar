import { describe, expect, it, vi } from "vitest";
import type { PostgresPool } from "../src/shared/postgres-client.js";
import { ClickHouseUsageStore, type ClickHouseClient } from "../src/storage/adapters/clickhouse.js";
import { S3BillingArchive } from "../src/storage/adapters/s3.js";
import { OutboxWorker } from "../src/storage/outbox-worker.js";
import type { OutboxEvent, OutboxStore, UsageChargeExport } from "../src/storage/ports.js";
import { createBursarRuntime } from "../src/storage/runtime.js";

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
      key: "tenant-a/billing-events/2026/07/29/00000000-0000-0000-0000-000000000001.json",
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

    await archive.close();
    expect(s3Mock.destroy).toHaveBeenCalledOnce();
  });
});

describe("ClickHouseUsageStore", () => {
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
    const store = new ClickHouseUsageStore({ client, createTable: false });
    const usage: UsageChargeExport = {
      chargeId: "00000000-0000-0000-0000-000000000042",
      accountId: "00000000-0000-0000-0000-000000000006",
      subjectId: "00000000-0000-0000-0000-000000000007",
      operation: "generate",
      feature: "chat",
      model: "gpt",
      region: null,
      measures: { tokens: 10 },
      dimensions: { workspace: "one" },
      metadata: {},
      requested: "15.000000",
      charged: "12.500000",
      allowanceRequested: "2.500000",
      allowanceCovered: "2.500000",
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
            outbox_event_id: "99",
            charge_id: usage.chargeId,
            charged: "12.500000",
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
  });
});

describe("BursarRuntime", () => {
  it("has no background worker or external dependency in PostgreSQL-only mode", async () => {
    const pool: PostgresPool = {
      query: vi.fn().mockResolvedValue({ rows: [] }),
      end: vi.fn().mockResolvedValue(undefined),
    };
    const runtime = await createBursarRuntime({ postgres: pool });

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
      clickhouse: { client: clickhouseClient, createTable: false },
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
});
