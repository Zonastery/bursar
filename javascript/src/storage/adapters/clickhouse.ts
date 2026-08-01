import Decimal from "decimal.js";
import type {
  AggregateStats,
  DailySpendRow,
  SpendByModelRow,
  SpendByUserRow,
  TopUserRow,
  UsageAnalyticsStore,
  UsageCharge,
  UsageChargePage,
  ListUsageChargesOptions,
  UsageChargeStore,
} from "../../credits/types/index.js";
import type { UsageChargeExport, UsageEventSink } from "../ports.js";

export interface ClickHouseQueryResult {
  json<T>(): Promise<T>;
}

/**
 * Structural subset of `@clickhouse/client`. Users without ClickHouse do not
 * need to install that package.
 */
export interface ClickHouseClient {
  command(options: { query: string }): Promise<unknown>;
  insert(options: {
    table: string;
    values: Record<string, unknown>[];
    format: "JSONEachRow";
  }): Promise<unknown>;
  query(options: {
    query: string;
    query_params?: Record<string, unknown>;
    format: "JSONEachRow";
  }): Promise<ClickHouseQueryResult>;
}

export interface ClickHouseUsageStoreOptions {
  client: ClickHouseClient;
  tenantId: string;
  table?: string;
  /** Create the usage projection table on first use. Defaults to true. */
  createTable?: boolean;
  /** Optional ClickHouse-side TTL. Omit to retain the full projection. */
  retentionDays?: number | null;
}

interface SpendRow {
  key: string;
  total_spend: string | number;
  entry_count: string | number;
}

interface TotalRow {
  total_spend: string | number;
  active_users: string | number;
}

function validateRange(start: Date, end: Date): void {
  if (
    !(start instanceof Date) ||
    !(end instanceof Date) ||
    Number.isNaN(start.getTime()) ||
    Number.isNaN(end.getTime()) ||
    end <= start
  ) {
    throw new RangeError("analytics requires end after start");
  }
}

function validateTableName(table: string): string {
  if (!/^[a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)?$/.test(table)) {
    throw new TypeError("ClickHouse table must be an identifier or database.identifier");
  }
  return table;
}

function quoteTable(table: string): string {
  return table
    .split(".")
    .map((part) => `"${part}"`)
    .join(".");
}

function clickHouseDate(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime()) || !/(?:Z|[+-]\d{2}:?\d{2})$/i.test(value)) {
    throw new Error(`Invalid usage timestamp: ${value}`);
  }
  return parsed.toISOString().replace("T", " ").replace("Z", "");
}

/**
 * Idempotent ClickHouse usage projection plus the analytics read port.
 *
 * `ReplacingMergeTree` and `FINAL` make replay after a worker crash safe.
 */
export class ClickHouseUsageStore implements UsageEventSink, UsageAnalyticsStore, UsageChargeStore {
  private readonly client: ClickHouseClient;
  private readonly tenantId: string;
  private readonly table: string;
  private readonly quotedTable: string;
  private readonly createTable: boolean;
  private readonly retentionDays: number | null;
  private initializePromise: Promise<void> | null = null;

  constructor(options: ClickHouseUsageStoreOptions) {
    this.client = options.client;
    this.tenantId = options.tenantId;
    this.table = validateTableName(options.table ?? "bursar_usage_events");
    this.quotedTable = quoteTable(this.table);
    this.createTable = options.createTable ?? true;
    this.retentionDays = options.retentionDays ?? null;
    if (
      this.retentionDays !== null &&
      (!Number.isInteger(this.retentionDays) ||
        this.retentionDays < 1 ||
        this.retentionDays > 36_500)
    ) {
      throw new RangeError("ClickHouse retentionDays must be between 1 and 36500");
    }
  }

  initialize(): Promise<void> {
    if (!this.createTable) return Promise.resolve();
    if (!this.initializePromise) this.initializePromise = this.createProjectionTable();
    return this.initializePromise;
  }

  async writeUsage(event: UsageChargeExport, outboxEventId: string): Promise<void> {
    await this.initialize();
    if (event.tenantId !== this.tenantId) {
      throw new Error("Usage event tenantId does not match ClickHouse store tenantId");
    }
    await this.client.insert({
      table: this.table,
      format: "JSONEachRow",
      values: [
        {
          tenant_id: event.tenantId,
          outbox_event_id: outboxEventId,
          charge_id: event.chargeId,
          account_id: event.accountId,
          subject_id: event.subjectId,
          operation: event.operation,
          feature: event.feature,
          model: event.model,
          region: event.region,
          measures: JSON.stringify(event.measures),
          dimensions: JSON.stringify(event.dimensions),
          metadata: JSON.stringify(event.metadata),
          requested: event.requested,
          charged: event.charged,
          allowance_requested: event.allowanceRequested,
          allowance_covered: event.allowanceCovered,
          catalog_revision_id: event.catalogRevisionId,
          plan_id: event.planId,
          rate_card_key: event.rateCardKey,
          pricing_snapshot: JSON.stringify(event.pricingSnapshot),
          ledger_entry_id: event.ledgerEntryId,
          correction_of_charge_id: event.correctionOfChargeId,
          idempotency_key: event.idempotencyKey,
          request_digest: event.requestDigest,
          event_at: clickHouseDate(event.eventAt),
          created_at: clickHouseDate(event.createdAt),
        },
      ],
    });
  }

  async spendByUser(start: Date, end: Date): Promise<SpendByUserRow[]> {
    const rows = await this.spendRows("subject_id", start, end);
    return rows.map((row) => ({
      userId: row.key,
      totalSpend: new Decimal(row.total_spend),
      entryCount: Number(row.entry_count),
    }));
  }

  async spendByModel(start: Date, end: Date): Promise<SpendByModelRow[]> {
    const rows = await this.spendRows("coalesce(model, 'unknown')", start, end);
    return rows.map((row) => ({
      model: row.key,
      totalSpend: new Decimal(row.total_spend),
      entryCount: Number(row.entry_count),
    }));
  }

  async topUsers(limit: number, start: Date, end: Date): Promise<TopUserRow[]> {
    if (!Number.isInteger(limit) || limit < 1 || limit > 10_000) {
      throw new RangeError("topUsers limit must be between 1 and 10000");
    }
    const rows = await this.spendRows("subject_id", start, end, limit);
    return rows.map((row) => ({
      userId: row.key,
      totalSpend: new Decimal(row.total_spend),
    }));
  }

  async dailySpend(start: Date, end: Date): Promise<DailySpendRow[]> {
    validateRange(start, end);
    const rows = await this.queryRows<SpendRow>(
      `SELECT
         formatDateTime(toStartOfDay(event_at), '%F') AS key,
         toString(sum(charged)) AS total_spend,
         toString(count()) AS entry_count
       FROM ${this.quotedTable} FINAL
       WHERE tenant_id = {tenantId:UUID}
         AND event_at >= parseDateTime64BestEffort({start:String})
         AND event_at < parseDateTime64BestEffort({end:String})
       GROUP BY key
       ORDER BY key`,
      start,
      end,
    );
    return rows.map((row) => ({
      date: row.key,
      totalSpend: new Decimal(row.total_spend),
      entryCount: Number(row.entry_count),
    }));
  }

  async aggregateStats(start: Date, end: Date): Promise<AggregateStats> {
    validateRange(start, end);
    const [totals, models, users] = await Promise.all([
      this.queryRows<TotalRow>(
        `SELECT
           toString(sum(charged)) AS total_spend,
           toString(uniqExact(subject_id)) AS active_users
         FROM ${this.quotedTable} FINAL
         WHERE tenant_id = {tenantId:UUID}
           AND event_at >= parseDateTime64BestEffort({start:String})
           AND event_at < parseDateTime64BestEffort({end:String})`,
        start,
        end,
      ),
      this.spendRows("coalesce(model, 'unknown')", start, end, 1),
      this.spendRows("subject_id", start, end, 1),
    ]);
    const total = new Decimal(totals[0]?.total_spend ?? 0);
    const days = Math.max(Math.ceil((end.getTime() - start.getTime()) / 86_400_000), 1);
    return {
      totalCreditsConsumed: total,
      activeUsers: Number(totals[0]?.active_users ?? 0),
      avgDailySpend: total.div(days),
      topModel: models[0]?.key ?? "",
      topUser: users[0]?.key ?? "",
    };
  }

  async listUsageCharges(
    userId: string,
    options: ListUsageChargesOptions = {},
  ): Promise<UsageChargePage> {
    if (!userId.trim()) throw new TypeError("userId must not be empty");
    const limit = options.limit ?? 50;
    if (!Number.isInteger(limit) || limit < 1 || limit > 200) {
      throw new RangeError("limit must be between 1 and 200");
    }
    const predicates = ["tenant_id = {tenantId:UUID}", "subject_id = {subjectId:UUID}"];
    const params: Record<string, unknown> = { tenantId: this.tenantId, subjectId: userId };
    if (options.fromDate) {
      predicates.push("event_at >= parseDateTime64BestEffort({fromDate:String})");
      params.fromDate = options.fromDate.toISOString();
    }
    if (options.toDate) {
      predicates.push("event_at < parseDateTime64BestEffort({toDate:String})");
      params.toDate = options.toDate.toISOString();
    }
    if (options.cursor) {
      predicates.push(
        "(event_at, charge_id) < " +
          "(parseDateTime64BestEffort({cursorEventAt:String}), {cursorUsageId:UUID})",
      );
      params.cursorEventAt = options.cursor.eventAt;
      params.cursorUsageId = options.cursor.usageId;
    }
    const rows = await this.queryRows<UsageRow>(
      `SELECT
         toString(charge_id) AS usage_id,
         toString(account_id) AS account_id,
         operation,
         toString(requested) AS requested,
         toString(charged) AS charged,
         toString(allowance_requested) AS allowance_requested,
         toString(allowance_covered) AS allowance_covered,
         feature,
         model,
         region,
         toString(event_at) AS event_at,
         idempotency_key,
         metadata,
         toString(created_at) AS created_at
       FROM ${this.quotedTable} FINAL
       WHERE ${predicates.join(" AND ")}
       ORDER BY event_at DESC, charge_id DESC
       LIMIT ${limit + 1}`,
      new Date(0),
      new Date(1),
      params,
    );
    const visible = rows.slice(0, limit);
    const items = visible.map((row) => ({
      usageId: row.usage_id,
      accountId: row.account_id,
      operation: row.operation,
      requested: new Decimal(row.requested),
      charged: new Decimal(row.charged),
      allowanceRequested: new Decimal(row.allowance_requested),
      allowanceCovered: new Decimal(row.allowance_covered),
      feature: row.feature ?? null,
      model: row.model ?? null,
      region: row.region ?? null,
      eventAt: new Date(row.event_at).toISOString(),
      idempotencyKey: row.idempotency_key,
      metadata: parseJsonObject(row.metadata),
      createdAt: new Date(row.created_at).toISOString(),
    })) satisfies UsageCharge[];
    const last = items.at(-1);
    return {
      items,
      nextCursor:
        rows.length > limit && last ? { eventAt: last.eventAt, usageId: last.usageId } : null,
    };
  }

  private async createProjectionTable(): Promise<void> {
    const ttl =
      this.retentionDays === null
        ? ""
        : `\nTTL event_at + toIntervalDay(${this.retentionDays}) DELETE`;
    await this.client.command({
      query: `CREATE TABLE IF NOT EXISTS ${this.quotedTable} (
        tenant_id UUID,
        outbox_event_id UInt64,
        charge_id UUID,
        account_id UUID,
        subject_id UUID,
        operation LowCardinality(String),
        feature Nullable(String),
        model Nullable(String),
        region Nullable(String),
        measures String,
        dimensions String,
        metadata String,
        requested Decimal(20, 6),
        charged Decimal(20, 6),
        allowance_requested Decimal(20, 6),
        allowance_covered Decimal(20, 6),
        catalog_revision_id Nullable(UUID),
        plan_id Nullable(UUID),
        rate_card_key Nullable(String),
        pricing_snapshot String,
        ledger_entry_id Nullable(UUID),
        correction_of_charge_id Nullable(UUID),
        idempotency_key String,
        request_digest String,
        event_at DateTime64(6, 'UTC'),
        created_at DateTime64(6, 'UTC'),
        ingested_at DateTime64(6, 'UTC') DEFAULT now64(6)
      )
      ENGINE = ReplacingMergeTree(outbox_event_id)
      PARTITION BY toYYYYMM(event_at)
      ORDER BY (tenant_id, event_at, charge_id)${ttl}`,
    });
  }

  private async spendRows(
    keyExpression: string,
    start: Date,
    end: Date,
    limit?: number,
  ): Promise<SpendRow[]> {
    validateRange(start, end);
    const limitSql = limit === undefined ? "" : `\nLIMIT ${limit}`;
    return this.queryRows<SpendRow>(
      `SELECT
         toString(${keyExpression}) AS key,
         toString(sum(charged)) AS total_spend,
         toString(count()) AS entry_count
       FROM ${this.quotedTable} FINAL
       WHERE tenant_id = {tenantId:UUID}
         AND event_at >= parseDateTime64BestEffort({start:String})
         AND event_at < parseDateTime64BestEffort({end:String})
       GROUP BY key
       ORDER BY sum(charged) DESC, key${limitSql}`,
      start,
      end,
    );
  }

  private async queryRows<T>(
    query: string,
    start: Date,
    end: Date,
    extraParams: Record<string, unknown> = {},
  ): Promise<T[]> {
    await this.initialize();
    const result = await this.client.query({
      query,
      query_params: {
        tenantId: this.tenantId,
        start: start.toISOString(),
        end: end.toISOString(),
        ...extraParams,
      },
      format: "JSONEachRow",
    });
    return result.json<T[]>();
  }
}

interface UsageRow {
  usage_id: string;
  account_id: string;
  operation: string;
  requested: string | number;
  charged: string | number;
  allowance_requested: string | number;
  allowance_covered: string | number;
  feature: string | null;
  model: string | null;
  region: string | null;
  event_at: string;
  idempotency_key: string;
  metadata: string | Record<string, unknown> | null;
  created_at: string;
}

function parseJsonObject(
  value: string | Record<string, unknown> | null,
): Record<string, unknown> | null {
  if (value === null) return null;
  if (typeof value === "object") return value;
  const parsed: unknown = JSON.parse(value);
  return parsed && typeof parsed === "object" && !Array.isArray(parsed)
    ? (parsed as Record<string, unknown>)
    : null;
}
