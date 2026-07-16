import { z } from "zod";
import type { CallProc } from "./types.js";
import { safeParse } from "./_shared.js";

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

/** Repository for analytics/aggregation queries.
 *
 * All methods call Postgres RPCs via the callproc function.
 * Returns typed Pydantic-model-like rows for each query type.
 */
export class AnalyticsRepository {
  constructor(private callproc: CallProc) {}

  /** Aggregate spend grouped by user within a date range. */
  async spendByUser(start: string, end: string): Promise<SpendByUserRow[]> {
    const rows = await this.callproc("spend_by_user", [start, end]);
    return (rows ?? []).map((r) =>
      safeParse(SpendByUserRowSchema, r, "AnalyticsRepository.spendByUser"),
    );
  }

  /** Aggregate spend grouped by model within a date range. */
  async spendByModel(start: string, end: string): Promise<SpendByModelRow[]> {
    const rows = await this.callproc("spend_by_model", [start, end]);
    return (rows ?? []).map((r) =>
      safeParse(SpendByModelRowSchema, r, "AnalyticsRepository.spendByModel"),
    );
  }

  /** Top users by total spend within a date range. */
  async topUsers(limit: number, start: string, end: string): Promise<TopUserRow[]> {
    const rows = await this.callproc("top_users", [limit, start, end]);
    return (rows ?? []).map((r) => safeParse(TopUserRowSchema, r, "AnalyticsRepository.topUsers"));
  }

  /** Daily spend breakdown within a date range. */
  async dailySpend(start: string, end: string): Promise<DailySpendRow[]> {
    const rows = await this.callproc("daily_spend", [start, end]);
    return (rows ?? []).map((r) =>
      safeParse(DailySpendRowSchema, r, "AnalyticsRepository.dailySpend"),
    );
  }

  /** Aggregate statistics (total credits, active users, avg daily spend, top model/user). */
  async aggregateStats(start: string, end: string): Promise<AggregateStatsRow> {
    const rows = await this.callproc("aggregate_stats", [start, end]);
    return safeParse(
      AggregateStatsRowSchema,
      rows?.[0] ?? {},
      "AnalyticsRepository.aggregateStats",
    );
  }

  /** Paginated list of user transactions filtered by type and date range. */
  async listUserTransactions(
    userId: string,
    types: string[] | null,
    fromDate: string | null,
    toDate: string | null,
    limit: number,
    offset: number,
  ): Promise<TransactionRow[]> {
    return this.listOffsetCompat(userId, types, fromDate, toDate, limit, offset, false);
  }

  /** Stable cursor page for mutable transaction history. */
  async listTransactionsCursor(
    userId: string,
    types: string[] | null,
    fromDate: string | null,
    toDate: string | null,
    limit: number,
    cursorCreatedAt: string | null,
    cursorId: string | null,
  ): Promise<TransactionRow[]> {
    const rows = await this.callproc("list_transactions_cursor", [
      userId,
      types,
      fromDate,
      toDate,
      limit,
      cursorCreatedAt,
      cursorId,
    ]);
    return (rows ?? []).map((r) =>
      safeParse(TransactionRowSchema, r, "AnalyticsRepository.listTransactionsCursor"),
    );
  }

  /** Paginated list of usage events for a user within a date range. */
  async listUsageEvents(
    userId: string,
    fromDate: string | null,
    toDate: string | null,
    limit: number,
    offset: number,
  ): Promise<TransactionRow[]> {
    return this.listOffsetCompat(userId, null, fromDate, toDate, limit, offset, true);
  }

  private async listOffsetCompat(
    userId: string,
    types: string[] | null,
    fromDate: string | null,
    toDate: string | null,
    limit: number,
    offset: number,
    usageOnly: boolean,
  ): Promise<TransactionRow[]> {
    if (limit <= 0) return [];
    let cursorCreatedAt: string | null = null;
    let cursorId: string | null = null;
    let remaining = Math.max(offset, 0);
    let totalCount = 0;
    const result: TransactionRow[] = [];
    while (true) {
      const pageLimit = Math.min(Math.max(limit + remaining, 1), 200);
      const rows = await this.callproc(
        usageOnly ? "list_usage_events_cursor" : "list_transactions_cursor_with_total",
        usageOnly
          ? [userId, fromDate, toDate, pageLimit, cursorCreatedAt, cursorId]
          : [userId, types, fromDate, toDate, pageLimit, cursorCreatedAt, cursorId],
      );
      const parsed = (rows ?? []).map((r) =>
        safeParse(
          TransactionRowSchema,
          r,
          usageOnly
            ? "AnalyticsRepository.listUsageEvents"
            : "AnalyticsRepository.listUserTransactions",
        ),
      );
      if (parsed.length > 0) totalCount = Number(parsed[0].total_count ?? 0);
      if (remaining < parsed.length) {
        result.push(...parsed.slice(remaining, remaining + limit));
        break;
      }
      remaining -= parsed.length;
      const marker = parsed[parsed.length - 1] as
        | (TransactionRow & {
            next_cursor_created_at?: string | Date | null;
            next_cursor_id?: string | null;
          })
        | undefined;
      if (!marker?.next_cursor_created_at || !marker.next_cursor_id) break;
      cursorCreatedAt = String(marker.next_cursor_created_at);
      cursorId = String(marker.next_cursor_id);
    }
    return result.map((row) => ({ ...row, total_count: totalCount }));
  }
}
