import { beforeEach, describe, expect, it, type Mock, vi } from "vitest";
import { ClickHouseUsageStore, type ClickHouseClient } from "../src/storage/adapters/clickhouse.js";
import {
  S3BillingArchive,
  type S3ArchiveClient,
  type S3ClientOptions,
} from "../src/storage/adapters/s3.js";
import type { BillingEventPayloadExport, UsageChargeExport } from "../src/storage/ports.js";

const TENANT_ID = "00000000-0000-0000-0000-000000000001";

const s3Mock = {
  configurations: new Array<S3ClientOptions>(),
  send: vi.fn<S3ArchiveClient["send"]>(),
  destroy: vi.fn(),
};

const testClientFactory = (options: S3ClientOptions): S3ArchiveClient => {
  s3Mock.configurations.push(options);
  return { send: s3Mock.send, destroy: s3Mock.destroy };
};

function billingEvent(): BillingEventPayloadExport {
  return {
    tenantId: TENANT_ID,
    eventId: "00000000-0000-0000-0000-000000000011",
    provider: "stripe",
    providerEnvironment: "live",
    providerEventId: "evt_11",
    eventType: "invoice.paid",
    status: "completed",
    receivedAt: "2026-07-29T12:30:00.000Z",
    completedAt: "2026-07-29T12:30:01.000Z",
    envelope: { id: "evt_11" },
    objectKey: null,
    objectVersion: null,
    archivedAt: null,
  };
}

function usageEvent(chargeId = "00000000-0000-0000-0000-000000000042"): UsageChargeExport {
  return {
    tenantId: TENANT_ID,
    chargeId,
    accountId: "00000000-0000-0000-0000-000000000006",
    subjectId: "00000000-0000-0000-0000-000000000007",
    operation: "generate",
    feature: "chat",
    model: "gpt",
    region: null,
    measures: { tokens: 10 },
    dimensions: { workspace: "one" },
    metadata: { source: "test" },
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
    idempotencyKey: `usage:${chargeId}`,
    requestDigest: "\\x1234",
    eventAt: "2026-07-29T12:00:00.000Z",
    createdAt: "2026-07-29T12:00:00.000Z",
  };
}

interface ClickHouseSchemaTestRow {
  name: string;
  type: string;
  engine: string;
  engine_full: string;
  sorting_key: string;
}

interface ClickHouseTestOptions {
  schemaRows?: ClickHouseSchemaTestRow[];
}

interface ClickHouseTestClient extends ClickHouseClient {
  command: Mock<ClickHouseClient["command"]>;
  insert: Mock<ClickHouseClient["insert"]>;
  query: Mock<ClickHouseClient["query"]>;
}

function clickHouseClient(options: ClickHouseTestOptions = {}): ClickHouseTestClient {
  return {
    command: vi.fn<ClickHouseClient["command"]>().mockResolvedValue(undefined),
    insert: vi.fn<ClickHouseClient["insert"]>().mockResolvedValue(undefined),
    query: vi.fn<ClickHouseClient["query"]>().mockResolvedValue({
      // SAFETY: The adapter requests the schema row type from this faithful test client.
      json: async <T>() => (options.schemaRows ?? []) as T,
    }),
  };
}

function compatibleSchemaRows(): ClickHouseSchemaTestRow[] {
  const columns = {
    tenant_id: "UUID",
    outbox_event_id: "UInt64",
    charge_id: "UUID",
    account_id: "UUID",
    subject_id: "UUID",
    operation: "LowCardinality(String)",
    feature: "Nullable(String)",
    model: "Nullable(String)",
    region: "Nullable(String)",
    measures: "String",
    dimensions: "String",
    metadata: "String",
    requested: "Decimal(20, 6)",
    charged: "Decimal(20, 6)",
    allowance_requested: "Decimal(20, 6)",
    allowance_covered: "Decimal(20, 6)",
    billing_disposition: "LowCardinality(String)",
    catalog_revision_id: "Nullable(UUID)",
    plan_id: "Nullable(UUID)",
    rate_card_key: "Nullable(String)",
    pricing_snapshot: "String",
    ledger_entry_id: "Nullable(UUID)",
    correction_of_charge_id: "Nullable(UUID)",
    idempotency_key: "String",
    request_digest: "String",
    event_at: "DateTime64(6, 'UTC')",
    created_at: "DateTime64(6, 'UTC')",
  };
  return Object.entries(columns).map(([name, type]) => ({
    name,
    type,
    engine: "ReplicatedReplacingMergeTree",
    engine_full: "ReplicatedReplacingMergeTree('/tables/x', 'replica', outbox_event_id)",
    sorting_key: "(tenant_id, event_at, charge_id)",
  }));
}

beforeEach(() => {
  s3Mock.configurations.length = 0;
  s3Mock.send.mockReset().mockResolvedValue({ VersionId: "v1" });
  s3Mock.destroy.mockReset();
});

describe("S3BillingArchive hardening", () => {
  it("uses the default credential and region chains and applies safe request options", async () => {
    const archive = new S3BillingArchive({
      bucket: "billing-archive",
      clientFactory: testClientFactory,
      putObject: {
        ServerSideEncryption: "aws:kms",
        SSEKMSKeyId: "alias/bursar",
        BucketKeyEnabled: true,
        ChecksumAlgorithm: "SHA256",
      },
    });

    await archive.archive(billingEvent());

    expect(s3Mock.configurations).toEqual([{ forcePathStyle: false }]);
    expect(s3Mock.configurations[0]).not.toHaveProperty("credentials");
    expect(s3Mock.configurations[0]).not.toHaveProperty("region");
    const command = s3Mock.send.mock.calls[0]?.[0];
    if (!command) throw new Error("expected an S3 put command");
    expect(command.input).toMatchObject({
      ServerSideEncryption: "aws:kms",
      SSEKMSKeyId: "alias/bursar",
      BucketKeyEnabled: true,
      ChecksumAlgorithm: "SHA256",
    });

    await archive.close();
    expect(s3Mock.destroy).toHaveBeenCalledOnce();
  });

  it("normalizes long slash-wrapped prefixes without pathological backtracking", async () => {
    const archive = new S3BillingArchive({
      bucket: "billing-archive",
      clientFactory: testClientFactory,
      prefix: `${"/".repeat(10_000)}tenant-a${"/".repeat(10_000)}`,
    });

    await archive.archive(billingEvent());

    const command = s3Mock.send.mock.calls[0]?.[0];
    if (!command) throw new Error("expected an S3 put command");
    expect(command.input.Key).toBe(
      "tenant-a/tenants/00000000-0000-0000-0000-000000000001/" +
        "billing-events/2026/07/29/00000000-0000-0000-0000-000000000011.json",
    );
  });

  it("does not destroy an injected client unless ownership is explicit", async () => {
    const client = {
      send: vi.fn().mockResolvedValue({ VersionId: "injected" }),
      destroy: vi.fn(),
    } satisfies S3ArchiveClient;
    const archive = new S3BillingArchive({
      bucket: "billing-archive",
      client,
    });

    await archive.archive(billingEvent());
    await archive.close();

    expect(client.send).toHaveBeenCalledOnce();
    expect(client.destroy).not.toHaveBeenCalled();
  });

  it("lazily creates and closes a factory-owned client", async () => {
    const client = {
      send: vi.fn().mockResolvedValue({}),
      destroy: vi.fn(),
    } satisfies S3ArchiveClient;
    const clientFactory = vi.fn(() => client);
    const archive = new S3BillingArchive({
      bucket: "billing-archive",
      clientFactory: (options) => {
        s3Mock.configurations.push(options);
        return clientFactory();
      },
    });

    expect(clientFactory).not.toHaveBeenCalled();
    await archive.archive(billingEvent());
    await archive.close();

    expect(clientFactory).toHaveBeenCalledOnce();
    expect(client.destroy).toHaveBeenCalledOnce();
  });

  it("recovers after a transient factory failure and rejects malformed clients", async () => {
    const client = {
      send: vi.fn().mockResolvedValue({}),
      destroy: vi.fn(),
    } satisfies S3ArchiveClient;
    const factory = vi
      .fn<() => S3ArchiveClient | Promise<S3ArchiveClient>>()
      .mockRejectedValueOnce(new Error("temporary credential provider failure"))
      .mockResolvedValueOnce(client);
    const archive = new S3BillingArchive({
      bucket: "billing-archive",
      clientFactory: factory,
    });

    await expect(archive.archive(billingEvent())).rejects.toThrow("temporary credential");
    await expect(archive.archive(billingEvent())).resolves.toBeDefined();
    expect(factory).toHaveBeenCalledTimes(2);

    const malformed = new S3BillingArchive({
      bucket: "billing-archive",
      clientFactory: () => {
        // SAFETY: This deliberately malformed client exercises runtime validation.
        return {} as S3ArchiveClient;
      },
    });
    await expect(malformed.archive(billingEvent())).rejects.toThrow(/must provide send/);
  });
});

describe("ClickHouseUsageStore hardening", () => {
  it("batches rows in one insert and preserves every outbox event ID", async () => {
    const client = clickHouseClient();
    const store = new ClickHouseUsageStore({ client, tenantId: TENANT_ID });
    const first = usageEvent();
    const second = usageEvent("00000000-0000-0000-0000-000000000043");

    await store.writeUsageBatch([
      [first, "99"],
      [second, "100"],
    ]);

    expect(client.command).not.toHaveBeenCalled();
    expect(client.insert).toHaveBeenCalledOnce();
    const request = client.insert.mock.calls[0]?.[0];
    if (!request) throw new Error("Expected writeUsageBatch to issue an insert");
    expect(request.values).toHaveLength(2);
    expect(request.values.map((row) => row.outbox_event_id)).toEqual(["99", "100"]);
    expect(request.values.map((row) => row.charge_id)).toEqual([first.chargeId, second.chargeId]);
  });

  it("has a no-DDL default and an explicit, coalesced schema initializer", async () => {
    const client = clickHouseClient();
    const store = new ClickHouseUsageStore({ client, tenantId: TENANT_ID });

    await store.initialize();
    expect(client.command).not.toHaveBeenCalled();

    const first = store.initializeSchema();
    const second = store.initializeSchema();
    expect(second).toBe(first);
    await first;

    expect(client.command).toHaveBeenCalledOnce();
    const commandCall = client.command.mock.calls[0];
    if (!commandCall) throw new Error("Expected initializeSchema to issue a command");
    const ddl = commandCall[0].query;
    expect(ddl).toContain("ENGINE = ReplacingMergeTree(outbox_event_id)");
    expect(ddl).not.toContain("ReplicatedReplacingMergeTree");
  });

  it("checks schema compatibility without prescribing replica topology", async () => {
    const rows = compatibleSchemaRows();
    const client = clickHouseClient({ schemaRows: rows });
    const store = new ClickHouseUsageStore({
      client,
      tenantId: TENANT_ID,
      table: "analytics.bursar_usage_events",
    });

    await expect(store.checkSchemaCompatibility()).resolves.toBeUndefined();
    expect(client.query).toHaveBeenCalledWith(
      expect.objectContaining({
        query_params: { database: "analytics", tableName: "bursar_usage_events" },
      }),
    );

    rows.find((row) => row.name === "outbox_event_id")!.type = "String";
    await expect(store.checkSchemaCompatibility()).rejects.toThrow(/outbox_event_id is String/);
  });

  it("delegates single-row writes to the batch API", async () => {
    const store = new ClickHouseUsageStore({ client: clickHouseClient(), tenantId: TENANT_ID });
    const batch = vi.spyOn(store, "writeUsageBatch");
    const event = usageEvent();

    await store.writeUsage(event, "101");

    expect(batch).toHaveBeenCalledWith([[event, "101"]]);
  });
});
