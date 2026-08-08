import type { Decimal } from "decimal.js";
import type { ListUsageChargesOptions, UsageChargePage } from "./ledger.js";

// ── Usage analytics ─────────────────────────────────────────────────
/** Aggregated spend for a single user in a time window. */
export interface SpendByUserRow {
  userId: string;
  totalSpend: Decimal;
  entryCount: number;
}

/** Aggregated spend for a single model in a time window. */
export interface SpendByModelRow {
  model: string;
  totalSpend: Decimal;
  entryCount: number;
}

/** Top-spending user in a time window. */
export interface TopUserRow {
  userId: string;
  totalSpend: Decimal;
}

/** Daily spend aggregation in a time window. */
export interface DailySpendRow {
  date: string;
  totalSpend: Decimal;
  entryCount: number;
}

/** Aggregate statistics across all users in a time window. */
export interface AggregateStats {
  totalCreditsConsumed: Decimal;
  activeUsers: number;
  avgDailySpend: Decimal;
  topModel: string | null;
  topUser: string | null;
}

/**
 * Read-only usage analytics backend.
 *
 * PostgreSQL implements this contract by default. High-volume deployments can
 * provide a ClickHouse implementation without moving balances or compact
 * accounting receipts out of PostgreSQL.
 */
export interface UsageAnalyticsStore {
  spendByUser(start: Date, end: Date): Promise<SpendByUserRow[]>;
  spendByModel(start: Date, end: Date): Promise<SpendByModelRow[]>;
  topUsers(limit: number, start: Date, end: Date): Promise<TopUserRow[]>;
  dailySpend(start: Date, end: Date): Promise<DailySpendRow[]>;
  aggregateStats(start: Date, end: Date): Promise<AggregateStats>;
}

/** Read-only usage history backend selected with the analytics backend. */
export interface UsageChargeStore {
  listUsageCharges(userId: string, options?: ListUsageChargesOptions): Promise<UsageChargePage>;
}
