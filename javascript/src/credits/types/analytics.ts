import type { Decimal } from "decimal.js";

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
  topModel: string;
  topUser: string;
}

// ── Spend caps and rate limiting ───────────────────────────────────────
/** Configuration for a per-user spend cap. */
export interface SpendCap {
  userId: string;
  type: "daily" | "monthly";
  model?: string | null;
  limit: Decimal;
  onExceed: "deny" | "warn" | "notify";
}

/** Result of checking a spend cap. */
export interface CapCheckResult {
  capped: boolean;
  currentSpend: Decimal;
  limit: Decimal;
  action: "deny" | "warn" | "notify" | null;
  model?: string | null;
}
