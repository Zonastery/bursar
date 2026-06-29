import type { Decimal } from "decimal.js";

/** Flexible metadata attached to credit transactions. */
export interface CreditMetadata {
  inputTokens?: number | null;
  outputTokens?: number | null;
  model?: string | null;
  referenceType?: string | null;
  referenceId?: string | null;
  idempotencyKey?: string | null;
  fixedJob?: string | null;
  [key: string]: unknown;
}

/** Schema for a pricing configuration. */
export interface PricingConfigData {
  models: Record<string, string>;
  tools?: Record<string, string> | null;
  search?: Record<string, string> | null;
  cache?: Record<string, string> | null;
  fixed?: Record<string, number> | null;
  minBalance?: number | null;
  plans?: Record<string, PlanDefinition> | null;
}

/** Current credit balance for a user. */
export interface BalanceResult {
  userId: string;
  balance: Decimal;
  lifetimePurchased: Decimal;
}

/** Result of adding credits to a user's account. */
export interface AddCreditsResult {
  transactionId: string;
  userId: string;
  amount: Decimal;
  newBalance: Decimal;
  lifetimePurchased: Decimal;
}

/** Result of reserving credits for an operation. */
export interface ReserveResult {
  reservationId: string;
  userId: string;
  amount: Decimal;
  balance: Decimal;
  reservedTotal: Decimal;
  error?: string | null;
}

/** Result of deducting credits. */
export interface DeductionResult {
  transactionId: string;
  userId: string;
  amount: Decimal;
  allowanceConsumed: Decimal;
  balanceAfter: Decimal;
  idempotent: boolean;
  capWarning: string | null;
  error?: string | null;
}

/** Options for an atomic allowance-aware deduction. */
export interface DeductWithAllowanceOptions {
  idempotencyKey?: string | null;
  minBalance?: Decimal;
  model?: string | null;
  metadata?: CreditMetadata | null;
}

/** Pricing config fetched from store. */
export interface PricingConfigResult {
  id: string;
  config: PricingConfigData;
  version: number;
}

/** Report of SQL setup results. */
export interface SetupResult {
  tablesCreated: string[];
  rpcsCreated: string[];
  errors: string[];
  readonly success: boolean;
}

/** Definition of a subscription plan with free allowance and rate overrides. */
export interface PlanDefinition {
  id: string;
  name: string;
  freeAllowance: Decimal;
  rateOverrides?: Record<string, string> | null;
  features?: Record<string, unknown> | null;
}

/** Result of checking plan allowance. */
export interface AllowanceResult {
  planId: string;
  allowanceRemaining: Decimal;
  periodStart: string;
  periodEnd: string;
}

/** Result of fetching a user's current plan. */
export interface GetUserPlanResult {
  userId: string;
  planId: string | null;
  planName: string | null;
  freeAllowance: Decimal;
  features: Record<string, unknown>;
}

/** Result of checking a user's feature entitlement. */
export interface CheckFeatureResult {
  userId: string;
  feature: string;
  value: unknown;
  hasFeature: boolean;
}
export interface SetUserPlanResult {
  userId: string;
  planId: string;
}

/** Result of refunding a credit deduction. */
export interface RefundResult {
  refundTransactionId: string;
  originalTransactionId: string;
  userId: string;
  amount: Decimal;
  newBalance: Decimal;
  error?: string | null;
}

/** Result of sweeping expired credits. */
export interface SweepResult {
  expiredCount: number;
  expiredAmount: Decimal;
  dryRun: boolean;
}

// ── Transaction listing ──────────────────────────────────────────────
/** A single credit transaction row. */
export interface UserTransactionRow {
  id: string;
  userId: string;
  amount: Decimal;
  type: string;
  referenceType: string | null;
  referenceId: string | null;
  metadata: Record<string, unknown> | null;
  createdAt: string;
}

/** Options for listing user transactions. */
export interface ListTransactionsOptions {
  types?: string[];
  fromDate?: Date;
  toDate?: Date;
  limit?: number;
  offset?: number;
}

/** Paginated result of listing user transactions. */
export interface PaginatedTransactions {
  items: UserTransactionRow[];
  total: number;
}

/** Options for listing usage events. */
export interface ListUsageEventsOptions {
  fromDate?: Date;
  toDate?: Date;
  limit?: number;
  offset?: number;
}

// ── Usage analytics ─────────────────────────────────────────────────
/** Aggregated spend for a single user in a time window. */
export interface SpendByUserRow {
  userId: string;
  totalSpend: Decimal;
  transactionCount: number;
}

/** Aggregated spend for a single model in a time window. */
export interface SpendByModelRow {
  model: string;
  totalSpend: Decimal;
  transactionCount: number;
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
  transactionCount: number;
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
  action: "deny" | "warn" | "notify";
}

/** Result of checking a spend cap. */
export interface CapCheckResult {
  capped: boolean;
  currentSpend: Decimal;
  limit: Decimal;
  action: "deny" | "warn" | "notify" | null;
  model?: string | null;
}

// ── Team/shared balance pools ─────────────────────────────────────────
/** A team with a shared credit balance pool. */
export interface Team {
  id: string;
  name: string;
  balance: Decimal;
  memberCount: number;
  createdAt: string;
}

/** A member of a team, with optional spend cap. */
export interface TeamMember {
  userId: string;
  role: string;
  spendCap?: Decimal | null;
  totalSpent: Decimal;
}

/** Result of fetching team balance. */
export interface TeamBalanceResult {
  teamId: string;
  name: string;
  balance: Decimal;
  memberCount: number;
}

/** Result of creating a team. */
export interface CreateTeamResult {
  teamId: string;
  name: string;
}

/** Result of adding a team member. */
export interface AddTeamMemberResult {
  teamId: string;
  userId: string;
  role: string;
}

/** Result of deducting credits from a team pool. */
export interface TeamDeductionResult {
  transactionId: string;
  teamId: string;
  userId: string;
  amount: Decimal;
  teamBalanceAfter: Decimal;
  error?: string | null;
}
