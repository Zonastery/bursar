import type { Decimal } from "decimal.js";
import type { AllowancePeriod, FeatureLimitPeriod } from "./allowance.js";

/**
 * Billing mode for an operation. ``strict`` never lets the balance fall below
 * the floor at admission (lease worst-case ⇒ zero debt); ``overdraft`` permits
 * the balance to go negative down to a configured floor and always bills the
 * full actual cost at settle (interface plan §1/D3/D5).
 */
export type BillingMode = "strict" | "overdraft";

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
  search?: string | null;
  cache?: string | null;
  // Money fields: Decimal, never a binary `number` (contract §1).
  fixed?: Record<string, Decimal> | null;
  minBalance?: Decimal | null;
  signupBonus?: number | null;
  plans?: Record<string, PlanDefinition> | null;
  tiers?: Record<string, TierDefinition> | null;
}

/**
 * Definition of a credit tier (credit tiers feature).
 *
 * ``priority`` controls deduction order (ascending = drained first, ties
 * broken by config key ascending — not an error). ``expires`` gates whether
 * ``addCredits`` into this tier requires/accepts an ``expiresAt``.
 * ``defaultTtlDays`` is used to compute an expiry when ``addCredits`` omits
 * ``expiresAt`` for an expiring tier. At most one tier may set
 * ``allowOverdraft: true`` (the tier that absorbs overdraft debt) and at most
 * one may set ``isDefault: true`` (the tier untagged ``addCredits`` calls
 * land in) — both enforced at config-validation time.
 */
export interface TierDefinition {
  name: string;
  priority: number;
  expires: boolean;
  defaultTtlDays?: number | null;
  allowOverdraft?: boolean;
  isDefault?: boolean;
}

/** A single tier's current balance, as returned by `getCreditTiers`. */
export interface TierBalance {
  tierKey: string;
  name: string;
  priority: number;
  expires: boolean;
  balance: Decimal;
}

/** Result of querying a user's per-tier credit balances. */
export interface TierBalancesResult {
  userId: string;
  /** Sorted by `priority` ascending. */
  tiers: TierBalance[];
  /** Equal to `BalanceResult.balance` — the aggregate across all tiers. */
  totalBalance: Decimal;
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
  /** The tier this grant landed in (`"default"` when no tiers are configured). */
  tier: string;
  /** `true` when this result is a replay of a prior grant with the same `idempotencyKey`. */
  idempotent?: boolean;
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
  /** Exact per-tier split of `amount` (credit tiers); `null` when not computed. */
  tierBreakdown?: Record<string, Decimal> | null;
  /**
   * Non-blocking `warn`/`notify` signal from a breached `FeatureLimit` (per-feature
   * invocation-count limits). `null` when no limit was breached (or none configured).
   * Mirrors `capWarning`.
   */
  featureLimitWarning?: string | null;
}

/** Options for an atomic allowance-aware deduction. */
export interface DeductWithAllowanceOptions {
  idempotencyKey?: string | null;
  minBalance?: Decimal;
  model?: string | null;
  metadata?: CreditMetadata | null;
  /** Free-allowance window start override (WS9); defaults to calendar-month when omitted. */
  periodStart?: Date | null;
  /** Feature name tagged onto the transaction and checked against `featureLimit`. */
  feature?: string | null;
  /** The resolved `FeatureLimit` for `feature` (looked up from the user's plan). `null`/omitted skips enforcement. */
  featureLimit?: FeatureLimit | null;
  /**
   * Calendar-aligned window start for `featureLimit.period` (resolved via
   * `resolveCalendarWindow`). Mirrors Python `base.py`: only the start is
   * threaded down — the store derives the window end itself from
   * `featureLimit.period`.
   */
  featurePeriodStart?: Date | null;
}

/** Pricing config fetched from store. */
export interface PricingConfigResult {
  id: string;
  config: PricingConfigData;
  version: number;
}

/** Lightweight summary for pricing version listing (H8 parity with Python base.py). */
export interface PricingConfigHistoryItem {
  id: string;
  version: number;
  label: string | null;
  active: boolean;
  createdAt: string;
}

/** Report of SQL setup results. */
export interface SetupResult {
  tablesCreated: string[];
  rpcsCreated: string[];
  errors: string[];
  readonly success: boolean;
}

/**
 * Per-operation financial-safety policy (interface plan §1).
 *
 * Resolved per call as: explicit arg → ``PlanDefinition.perOperation[type]`` →
 * plan default → the manager's constructor preset. ``maxConcurrent`` bounds the
 * number of simultaneously-active leases for an operation type; ``overdraftFloor``
 * (only meaningful when ``billingMode === "overdraft"``) is the negative balance
 * floor admission is allowed down to.
 */
export interface OperationPolicy {
  billingMode: BillingMode;
  maxConcurrent?: number | null;
  overdraftFloor?: Decimal | null;
}

/**
 * Per-feature invocation-count limit (cadence-based rate limiting).
 *
 * `maxCalls` bounds how many times a feature may be invoked within one
 * `period` window. Windows are calendar-aligned (see
 * {@link resolveCalendarWindow} in `allowance.ts`): `"daily"` resets at UTC
 * midnight, `"weekly"` on Monday (ISO week), `"monthly"` on the 1st,
 * `"yearly"` on Jan 1 — every user resets together, no per-user anchor.
 *
 * `action` mirrors `SpendCap.action`: `"deny"` blocks the call (checked at
 * admission/deduction); `"warn"`/`"notify"` are non-blocking signals, checked
 * only on the immediate-charge and settle paths — admission
 * (`reserve`/`createLease`) only ever enforces `"deny"`, exactly like spend
 * caps.
 */
export interface FeatureLimit {
  maxCalls: number;
  period: FeatureLimitPeriod;
  action: "deny" | "warn" | "notify";
}

/**
 * Definition of a subscription plan with free allowance and rate overrides.
 *
 * Beyond allowance/rates/features, a plan carries the financial-safety policy
 * (interface plan §1): a ``defaultBillingMode`` for the whole plan, optional
 * ``perOperation`` overrides keyed by operation type, and plan-wide
 * ``maxConcurrent`` / ``overdraftFloor`` defaults.
 */
export interface PlanDefinition {
  id: string;
  name: string;
  freeAllowance: Decimal;
  rateOverrides?: Record<string, string> | null;
  features?: Record<string, unknown> | null;
  /**
   * Per-feature invocation-count limits, keyed by feature name. Independent of
   * `features` (boolean/value entitlement) — a feature can be entitled and
   * rate-limited at the same time.
   */
  featureLimits?: Record<string, FeatureLimit> | null;
  defaultBillingMode?: BillingMode;
  perOperation?: Record<string, OperationPolicy>;
  maxConcurrent?: number | null;
  overdraftFloor?: Decimal | null;
  /** Free-allowance reset window mode (WS9). Defaults to `"calendar_month"` when absent. */
  allowancePeriod?: AllowancePeriod;
}

/** Result of checking plan allowance. */
export interface AllowanceResult {
  planId: string;
  allowanceRemaining: Decimal;
  periodStart: string;
  periodEnd: string;
}

/**
 * Result of fetching a user's current plan.
 *
 * Carries the plan's financial-safety policy (``defaultBillingMode``,
 * ``perOperation``, ``maxConcurrent``, ``overdraftFloor``) so the manager can
 * resolve admission policy without a second round-trip (interface plan §1).
 */
export interface GetUserPlanResult {
  userId: string;
  planId: string | null;
  planName: string | null;
  freeAllowance: Decimal;
  features: Record<string, unknown>;
  /** Per-feature invocation-count limits, keyed by feature name. Empty when none configured. */
  featureLimits?: Record<string, FeatureLimit>;
  defaultBillingMode?: BillingMode;
  perOperation?: Record<string, OperationPolicy>;
  maxConcurrent?: number | null;
  overdraftFloor?: Decimal | null;
  /** Free-allowance reset window mode (WS9). Absent/omitted means `"calendar_month"`. */
  allowancePeriod?: AllowancePeriod;
  /** Timestamp the current plan was assigned — the anchor for non-calendar periods. */
  planAssignedAt?: Date | null;
}

/**
 * Result of acquiring (or renewing) a lease — the atomic admission hold.
 *
 * A lease is the *only* admission control (interface plan §3/D4): it holds
 * ``amount`` against ``available = balance − Σ(active holds)`` under one lock so
 * concurrent operations see each other and ``maxConcurrent`` is real. On failure
 * ``error`` carries a business code (``insufficient_credits``, ``concurrency_limit``,
 * ``cap_reached``, ``feature_not_entitled``, ``invalid_amount``, ``lease_not_found``,
 * ``lease_expired``, ``lease_released``) for the manager to map to a typed exception.
 */
export interface LeaseResult {
  leaseId: string;
  userId: string;
  amount: Decimal;
  available: Decimal;
  reservedTotal: Decimal;
  billingMode: BillingMode;
  expiresAt: string;
  error?: string | null;
}

/**
 * Result of releasing a lease without charging (interface plan §3).
 *
 * Idempotent and safe on missing/already-finalized leases: ``released`` is
 * ``true`` only when this call transitioned an active/expired lease to released.
 * ``reason`` is one of ``released``, ``already_released``, ``already_settled``,
 * ``not_found`` — never a bare void (resolves H1).
 */
export interface ReleaseResult {
  leaseId: string;
  userId: string;
  released: boolean;
  reason?: string | null;
}

/**
 * Advisory affordability check — UI only, non-locking, may be stale (D4/H3).
 *
 * Never used for admission control; that is exclusively the lease (``reserve``).
 */
export interface CanAffordResult {
  affordable: boolean;
  /** Effective spending power including allowance: `balance − reserved (+ allowance headroom)`. */
  spendable: Decimal;
  worstCase: Decimal;
  reason?: string | null;
}

/** Advisory available-balance read: ``available = balance − reserved`` (D4/H3). */
export interface AvailableResult {
  userId: string;
  balance: Decimal;
  reserved: Decimal;
  available: Decimal;
}

/** Result of checking a user's feature entitlement. */
export interface CheckFeatureResult {
  userId: string;
  feature: string;
  value: unknown;
  hasFeature: boolean;
}

/**
 * Advisory, non-locking read of a per-feature invocation-count limit (UI only).
 *
 * Mirrors `CapCheckResult`: `limited: false` means no `FeatureLimit` is
 * configured for this feature on the user's plan (unlimited) — `limit`,
 * `used`, `remaining`, and the period bounds are zeroed/empty in that case,
 * and `action` is `null`. Never used for admission control; that is
 * exclusively the atomic check-and-increment inside `deduct`/`reserve`.
 */
export interface FeatureLimitResult {
  userId: string;
  feature: string;
  limited: boolean;
  limit: number;
  used: number;
  remaining: number;
  periodStart: string;
  periodEnd: string;
  action: "deny" | "warn" | "notify" | null;
}

export interface SetUserPlanResult {
  userId: string;
  planId: string;
  planAssignedAt?: string | null;
}

/** Result of refunding a credit deduction. */
export interface RefundResult {
  refundTransactionId: string;
  originalTransactionId: string;
  userId: string;
  amount: Decimal;
  newBalance: Decimal;
  error?: string | null;
  /** Per-tier LIFO restoration split of `amount` (credit tiers); `null` when not computed. */
  tierBreakdown?: Record<string, Decimal> | null;
}

/** Result of sweeping expired credits. */
export interface SweepResult {
  expiredCount: number;
  expiredAmount: Decimal;
  dryRun: boolean;
  /** Per-tier split of `expiredAmount` (credit tiers); `null` when not computed. */
  expiredByTier?: Record<string, Decimal> | null;
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
