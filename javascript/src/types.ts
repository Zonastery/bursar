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
  balance: number;
  lifetimePurchased: number;
}

/** Result of adding credits to a user's account. */
export interface AddCreditsResult {
  transactionId: string;
  userId: string;
  amount: number;
  newBalance: number;
  lifetimePurchased: number;
}

/** Result of reserving credits for an operation. */
export interface ReserveResult {
  reservationId: string;
  userId: string;
  amount: number;
  balance: number;
  reservedTotal: number;
  error?: string | null;
}

/** Result of deducting credits. */
export interface DeductionResult {
  transactionId: string;
  userId: string;
  amount: number;
  balanceAfter: number;
  idempotent: boolean;
  error?: string | null;
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
  freeAllowance: number;
  rateOverrides?: Record<string, string> | null;
  features?: Record<string, boolean> | null;
}

/** Result of checking plan allowance. */
export interface AllowanceResult {
  planId: string;
  allowanceRemaining: number;
  periodStart: string;
  periodEnd: string;
}

/** Result of fetching a user's current plan. */
export interface GetUserPlanResult {
  userId: string;
  planId: string | null;
  planName: string | null;
  freeAllowance: number;
}

/** Result of assigning a plan to a user. */
export interface SetUserPlanResult {
  userId: string;
  planId: string;
}

/** Result of refunding a credit deduction. */
export interface RefundResult {
  refundTransactionId: string;
  originalTransactionId: string;
  userId: string;
  amount: number;
  newBalance: number;
  error?: string | null;
}

/** Result of sweeping expired credits. */
export interface SweepResult {
  expiredCount: number;
  expiredAmount: number;
  dryRun: boolean;
}

// ── Usage analytics ─────────────────────────────────────────────────
/** Aggregated spend for a single user in a time window. */
export interface SpendByUserRow {
  userId: string;
  totalSpend: number;
  transactionCount: number;
}

/** Aggregated spend for a single model in a time window. */
export interface SpendByModelRow {
  model: string;
  totalSpend: number;
  transactionCount: number;
}

/** Top-spending user in a time window. */
export interface TopUserRow {
  userId: string;
  totalSpend: number;
}

/** Daily spend aggregation in a time window. */
export interface DailySpendRow {
  date: string;
  totalSpend: number;
  transactionCount: number;
}

/** Aggregate statistics across all users in a time window. */
export interface AggregateStats {
  totalCreditsConsumed: number;
  activeUsers: number;
  avgDailySpend: number;
  topModel: string;
  topUser: string;
}

// ── Spend caps and rate limiting ───────────────────────────────────────
/** Configuration for a per-user spend cap. */
export interface SpendCap {
  userId: string;
  type: "daily" | "monthly";
  model?: string | null;
  limit: number;
  action: "deny" | "warn" | "notify";
}

/** Result of checking a spend cap. */
export interface CapCheckResult {
  capped: boolean;
  currentSpend: number;
  limit: number;
  action: "deny" | "warn" | "notify" | null;
  model?: string | null;
}

// ── Team/shared balance pools ─────────────────────────────────────────
/** A team with a shared credit balance pool. */
export interface Team {
  id: string;
  name: string;
  balance: number;
  memberCount: number;
  createdAt: string;
}

/** A member of a team, with optional spend cap. */
export interface TeamMember {
  userId: string;
  role: string;
  spendCap?: number | null;
  totalSpent: number;
}

/** Result of fetching team balance. */
export interface TeamBalanceResult {
  teamId: string;
  name: string;
  balance: number;
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
  amount: number;
  teamBalanceAfter: number;
  error?: string | null;
}
