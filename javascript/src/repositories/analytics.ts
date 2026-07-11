import { z } from "zod";
import type { CallProc } from "./types.js";

export const SpendByUserRowSchema = z
  .object({
    user_id: z.string().optional(),
    total_spend: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
    transaction_count: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
  })
  .passthrough();

export const SpendByModelRowSchema = z
  .object({
    model: z.string().optional(),
    total_spend: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
    transaction_count: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
  })
  .passthrough();

export const TopUserRowSchema = z
  .object({
    user_id: z.string().optional(),
    total_spend: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
  })
  .passthrough();

export const DailySpendRowSchema = z
  .object({
    date: z.string().optional(),
    total_spend: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
    transaction_count: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
  })
  .passthrough();

export const AggregateStatsRowSchema = z
  .object({
    total_credits_consumed: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
    active_users: z.number().optional(),
    avg_daily_spend: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
    top_model: z.string().optional(),
    top_user: z.string().optional(),
  })
  .passthrough();

export const TransactionRowSchema = z
  .object({
    id: z.string().optional(),
    user_id: z.string().optional(),
    amount: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
    type: z.string().optional(),
    reference_type: z.string().nullable().optional(),
    reference_id: z.string().nullable().optional(),
    metadata: z.record(z.string(), z.unknown()).nullable().optional(),
    created_at: z
      .union([z.string(), z.date()] as const)
      .nullable()
      .optional(),
    total_count: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
  })
  .passthrough();

export type SpendByUserRow = z.infer<typeof SpendByUserRowSchema>;
export type SpendByModelRow = z.infer<typeof SpendByModelRowSchema>;
export type TopUserRow = z.infer<typeof TopUserRowSchema>;
export type DailySpendRow = z.infer<typeof DailySpendRowSchema>;
export type AggregateStatsRow = z.infer<typeof AggregateStatsRowSchema>;
export type TransactionRow = z.infer<typeof TransactionRowSchema>;

export class AnalyticsRepository {
  constructor(private callproc: CallProc) {}

  async spendByUser(start: string, end: string): Promise<SpendByUserRow[]> {
    const rows = await this.callproc("spend_by_user", [start, end]);
    return (rows ?? []).map((r) => SpendByUserRowSchema.parse(r));
  }

  async spendByModel(start: string, end: string): Promise<SpendByModelRow[]> {
    const rows = await this.callproc("spend_by_model", [start, end]);
    return (rows ?? []).map((r) => SpendByModelRowSchema.parse(r));
  }

  async topUsers(limit: number, start: string, end: string): Promise<TopUserRow[]> {
    const rows = await this.callproc("top_users", [limit, start, end]);
    return (rows ?? []).map((r) => TopUserRowSchema.parse(r));
  }

  async dailySpend(start: string, end: string): Promise<DailySpendRow[]> {
    const rows = await this.callproc("daily_spend", [start, end]);
    return (rows ?? []).map((r) => DailySpendRowSchema.parse(r));
  }

  async aggregateStats(start: string, end: string): Promise<AggregateStatsRow> {
    const rows = await this.callproc("aggregate_stats", [start, end]);
    return AggregateStatsRowSchema.parse(rows?.[0] ?? {});
  }

  async listUserTransactions(
    userId: string,
    types: string[] | null,
    fromDate: string | null,
    toDate: string | null,
    limit: number,
    offset: number,
  ): Promise<TransactionRow[]> {
    const rows = await this.callproc("list_user_transactions", [
      userId,
      types,
      fromDate,
      toDate,
      limit,
      offset,
    ]);
    return (rows ?? []).map((r) => TransactionRowSchema.parse(r));
  }

  async listUsageEvents(
    userId: string,
    fromDate: string | null,
    toDate: string | null,
    limit: number,
    offset: number,
  ): Promise<TransactionRow[]> {
    const rows = await this.callproc("list_usage_events", [
      userId,
      fromDate,
      toDate,
      limit,
      offset,
    ]);
    return (rows ?? []).map((r) => TransactionRowSchema.parse(r));
  }
}
