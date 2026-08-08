import { z } from "zod";
import type { CallProc } from "../../../shared/postgres-types.js";
import { optionalRecordRow, requireRow, safeParse } from "../../../shared/postgres-validation.js";

const decimal = z.union([z.string().min(1), z.number().finite()] as const);
const count = z.union([
  z.number().int().nonnegative(),
  z.string().regex(/^\d+$/).transform(Number),
]);
const timestamp = z.union([z.string().min(1), z.date().transform((value) => value.toISOString())]);
const calendarDate = z.union([
  z.string().regex(/^\d{4}-\d{2}-\d{2}$/),
  z.date().transform((value) => value.toISOString().slice(0, 10)),
]);

const SpendByUserRowSchema = z
  .object({
    user_id: z.string().min(1),
    total_spend: decimal,
    entry_count: count,
  })
  .strict();

const SpendByModelRowSchema = z
  .object({
    model: z.string().min(1),
    total_spend: decimal,
    entry_count: count,
  })
  .strict();

const TopUserRowSchema = z.object({ user_id: z.string().min(1), total_spend: decimal }).strict();

const DailySpendRowSchema = z
  .object({
    date: calendarDate,
    total_spend: decimal,
    entry_count: count,
  })
  .strict();

const AggregateStatsRowSchema = z
  .object({
    total_credits_consumed: decimal,
    active_users: count,
    avg_daily_spend: decimal,
    top_model: z.string().min(1).nullable(),
    top_user: z.string().min(1).nullable(),
  })
  .strict();

const LedgerEntryRowSchema = z
  .object({
    entry_id: z.string(),
    account_id: z.string(),
    actor_user_id: z.string().nullable(),
    amount: decimal,
    entry_type: z.string(),
    operation: z.string(),
    reference_entry_id: z.string().nullable(),
    idempotency_key: z.string().nullable(),
    metadata: z.record(z.string(), z.unknown()).nullable(),
    created_at: timestamp,
  })
  .strict();

const UsageChargeRowSchema = z
  .object({
    usage_id: z.string(),
    account_id: z.string(),
    operation: z.string(),
    requested: decimal,
    charged: decimal,
    allowance_requested: decimal,
    allowance_covered: decimal,
    billing_disposition: z.enum(["billable", "record_only"]),
    feature: z.string().nullable(),
    model: z.string().nullable(),
    region: z.string().nullable(),
    event_at: timestamp,
    idempotency_key: z.string(),
    metadata: z.record(z.string(), z.unknown()).nullable(),
    created_at: timestamp,
  })
  .strict();

export type SpendByUserRow = z.infer<typeof SpendByUserRowSchema>;
export type SpendByModelRow = z.infer<typeof SpendByModelRowSchema>;
export type TopUserRow = z.infer<typeof TopUserRowSchema>;
export type DailySpendRow = z.infer<typeof DailySpendRowSchema>;
export type AggregateStatsRow = z.infer<typeof AggregateStatsRowSchema>;
export type LedgerEntryRow = z.infer<typeof LedgerEntryRowSchema>;
export type UsageChargeRow = z.infer<typeof UsageChargeRowSchema>;

export class AnalyticsRepository {
  constructor(private readonly callproc: CallProc) {}

  async spendByUser(start: string, end: string): Promise<SpendByUserRow[]> {
    const rows = await this.callproc("spend_by_user", [start, end]);
    return (rows ?? []).map((raw) => {
      const row = raw as Record<string, unknown>;
      return safeParse(
        SpendByUserRowSchema,
        { user_id: row.subject_id, total_spend: row.total_spend, entry_count: row.charge_count },
        "AnalyticsRepository.spendByUser",
      );
    });
  }

  async spendByModel(start: string, end: string): Promise<SpendByModelRow[]> {
    const rows = await this.callproc("spend_by_model", [start, end]);
    return (rows ?? []).map((raw) => {
      const row = raw as Record<string, unknown>;
      return safeParse(
        SpendByModelRowSchema,
        { model: row.model, total_spend: row.total_spend, entry_count: row.charge_count },
        "AnalyticsRepository.spendByModel",
      );
    });
  }

  async topUsers(limit: number, start: string, end: string): Promise<TopUserRow[]> {
    const rows = await this.callproc("spend_by_user", [start, end]);
    return (rows ?? []).slice(0, limit).map((raw) => {
      const row = raw as Record<string, unknown>;
      return safeParse(
        TopUserRowSchema,
        { user_id: row.subject_id, total_spend: row.total_spend },
        "AnalyticsRepository.topUsers",
      );
    });
  }

  async dailySpend(start: string, end: string): Promise<DailySpendRow[]> {
    const rows = await this.callproc("daily_spend", [start, end]);
    return (rows ?? []).map((raw) => {
      const row = raw as Record<string, unknown>;
      return safeParse(
        DailySpendRowSchema,
        { date: row.day, total_spend: row.total_spend, entry_count: row.charge_count },
        "AnalyticsRepository.dailySpend",
      );
    });
  }

  async aggregateStats(start: string, end: string): Promise<AggregateStatsRow> {
    const rows = await this.callproc("aggregate_usage_stats", [start, end]);
    return safeParse(
      AggregateStatsRowSchema,
      requireRow(rows, "AnalyticsRepository.aggregateStats"),
      "AnalyticsRepository.aggregateStats",
    );
  }

  async listLedgerEntries(
    userId: string,
    entryTypes: string[] | null,
    fromDate: string | null,
    toDate: string | null,
    limit: number,
    cursorCreatedAt: string | null,
    cursorEntryId: string | null,
    usageOnly = false,
  ): Promise<LedgerEntryRow[]> {
    if ((cursorCreatedAt == null) !== (cursorEntryId == null)) {
      throw new Error("ledger cursor requires both createdAt and entryId");
    }
    const rows = await this.callproc("list_ledger", [
      userId,
      cursorCreatedAt,
      cursorEntryId,
      limit,
      entryTypes,
      fromDate,
      toDate,
      usageOnly,
    ]);
    return (rows ?? []).map((row) =>
      safeParse(LedgerEntryRowSchema, row, "AnalyticsRepository.listLedgerEntries"),
    );
  }

  async getLedgerEntry(userId: string, entryId: string): Promise<LedgerEntryRow | null> {
    const rows = await this.callproc("get_ledger_entry", [userId, entryId]);
    const row = optionalRecordRow(rows, "AnalyticsRepository.getLedgerEntry");
    return row === null
      ? null
      : safeParse(LedgerEntryRowSchema, row, "AnalyticsRepository.getLedgerEntry");
  }

  async listUsageCharges(
    userId: string,
    fromDate: string | null,
    toDate: string | null,
    limit: number,
    cursorEventAt: string | null,
    cursorUsageId: string | null,
    includeRecordOnly: boolean,
  ): Promise<UsageChargeRow[]> {
    if ((cursorEventAt == null) !== (cursorUsageId == null)) {
      throw new Error("usage charge cursor requires both eventAt and usageId");
    }
    const rows = await this.callproc("list_usage_charges", [
      userId,
      cursorEventAt,
      cursorUsageId,
      limit,
      fromDate,
      toDate,
      includeRecordOnly,
    ]);
    return (rows ?? []).map((row) =>
      safeParse(UsageChargeRowSchema, row, "AnalyticsRepository.listUsageCharges"),
    );
  }
}
