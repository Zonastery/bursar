import type { Decimal } from "decimal.js";
import { CapabilityNotSupportedError } from "../errors.js";
import type {
  AddCreditsResult,
  AddTeamMemberResult,
  AggregateStats,
  AllowanceResult,
  AvailableResult,
  BalanceResult,
  BillingMode,
  CapCheckResult,
  CheckFeatureResult,
  CreateTeamResult,
  CreditMetadata,
  DailySpendRow,
  DeductionResult,
  DeductWithAllowanceOptions,
  FeatureLimit,
  FeatureLimitResult,
  GetUserPlanResult,
  LeaseResult,
  ListTransactionsOptions,
  ListUsageEventsOptions,
  PaginatedTransactions,
  PricingConfigData,
  PricingConfigHistoryItem,
  PricingConfigResult,
  RefundResult,
  ReleaseResult,
  SetUserPlanResult,
  SetupResult,
  SpendByModelRow,
  SpendByUserRow,
  SweepResult,
  TeamBalanceResult,
  TeamDeductionResult,
  TeamMember,
  TierBalancesResult,
  TopUserRow,
} from "../types.js";

/**
 * Per-feature invocation-count limit enforcement inputs, threaded through the
 * three atomic ops by the manager (resolved once via `getUserPlan` +
 * `resolveCalendarWindow`). `feature` alone (with `featureLimit`/
 * `featurePeriodStart` omitted) still tags the inserted transaction's
 * `metadata.feature` — enforcement only runs when `featureLimit` is given.
 *
 * Mirrors Python `base.py` exactly: only the window START is threaded down
 * (not an explicit end) — the store derives the window end itself from
 * `featureLimit.period` (e.g. via `resolveCalendarWindow(featurePeriodStart,
 * featureLimit.period).end`), since `featurePeriodStart` is already
 * calendar-aligned so re-resolving it yields the identical window.
 */
export interface FeatureLimitOptions {
  /** Feature name tagged onto the transaction and checked against `featureLimit`. */
  feature?: string | null;
  /** The resolved `FeatureLimit` for `feature` (looked up from the user's plan). `null`/omitted skips enforcement. */
  featureLimit?: FeatureLimit | null;
  /** Calendar-aligned window start for `featureLimit.period` (resolved via `resolveCalendarWindow`). */
  featurePeriodStart?: Date | null;
}

/** Options for atomically acquiring a lease (interface plan §3 / D4). */
export interface CreateLeaseOptions extends FeatureLimitOptions {
  billingMode?: BillingMode;
  floor?: Decimal;
  maxConcurrent?: number | null;
  ttlSeconds?: number;
  model?: string | null;
  overdraftFloor?: Decimal | null;
  metadata?: CreditMetadata | null;
  /** Free-allowance window start override (WS9); defaults to calendar-month when omitted. */
  periodStart?: Date | null;
}

/** Options for charging the actual cost against a lease (interface plan §3 / D5). */
export interface SettleLeaseOptions extends FeatureLimitOptions {
  idempotencyKey?: string | null;
  minBalance?: Decimal;
  model?: string | null;
  metadata?: CreditMetadata | null;
  /** Free-allowance window start override (WS9); defaults to calendar-month when omitted. */
  periodStart?: Date | null;
}

/**
 * Abstract base for credit storage backends (WS8).
 *
 * Split into two tiers:
 *  - **Core** (abstract, must be implemented): balance/credit ops, the atomic
 *    lease lifecycle, pricing-config versioning, plan management, spend caps,
 *    refunds, and expiry sweeping. Every backend needs these.
 *  - **Optional capabilities** (concrete, default-throwing): usage analytics,
 *    transaction listing, and shared team-balance pools. A custom store that
 *    doesn't need these can skip them entirely — the default implementation
 *    throws {@link CapabilityNotSupportedError} instead of forcing a stub.
 */
export abstract class CreditStore {
  abstract setup(databaseUrl?: string | null): Promise<SetupResult>;
  abstract getBalance(userId: string): Promise<BalanceResult>;
  abstract addCredits(
    userId: string,
    amount: Decimal,
    type?: string,
    metadata?: CreditMetadata | null,
    expiresAt?: Date | null,
    /** Target credit tier (credit tiers); omitted/`null` resolves to the config's default tier. */
    tier?: string | null,
    /**
     * Replay-safe idempotency key (parity with the `deduct`/`settle`/`refund`
     * idempotency idiom). When a prior grant for this `userId` carries the same
     * key, the prior result is returned unchanged and no new credits are granted.
     */
    idempotencyKey?: string | null,
  ): Promise<AddCreditsResult>;
  /**
   * Atomically calculate-and-charge in one server-side transaction:
   * consume free allowance, enforce spend caps, apply the balance floor,
   * and debit the net amount — idempotency-keyed end-to-end. See contract §2.
   */
  abstract deductWithAllowance(
    userId: string,
    amount: Decimal,
    options?: DeductWithAllowanceOptions,
  ): Promise<DeductionResult>;

  // ── Lease lifecycle (atomic admission) ─────────────────────────────
  //
  // The lease is the canonical admission primitive (interface plan §3/D4).
  // ``reserve``/``settle``/``release``/``renew`` on the manager map onto these.
  // Leases reuse the credit_reservations table/records extended with a status
  // (active → settled | released | expired), a billing mode, and an overdraft
  // floor. ``available = balance − Σ(amount WHERE status='active' AND unexpired)``.

  /**
   * Atomically acquire a lease (hold) — the only admission control (D4).
   *
   * Under one critical section the store: (1) ensures the balance row exists;
   * (2) enforces ``maxConcurrent`` by counting active leases for ``(userId,
   * operationType)``; (3) enforces ``deny`` spend caps for ``amount``; (4) computes
   * ``available = balance − Σ active holds`` and rejects with
   * ``error="insufficient_credits"`` if ``available − amount < floor``; (5) inserts
   * an ``active`` lease expiring after ``ttlSeconds``. Business failures are
   * returned via ``LeaseResult.error``; the store never raises domain exceptions.
   */
  abstract createLease(
    userId: string,
    amount: Decimal,
    operationType: string,
    options?: CreateLeaseOptions,
  ): Promise<LeaseResult>;

  /**
   * Charge the actual cost against a lease, then mark it settled (D5).
   *
   * De-clamped: charges ``amount`` even if it exceeds the lease hold (overdraft),
   * and never clamps to the reserved ceiling. Spend caps are advisory at settle (a
   * breach sets ``capWarning`` but never blocks); no floor block, so the balance may
   * go negative in overdraft. ``amount === 0`` releases the lease without charging.
   * Lease-state failures (``lease_not_found``/``lease_expired``) are returned via
   * ``DeductionResult.error``; a replay returns the original result idempotently.
   */
  abstract settleLease(
    userId: string,
    leaseId: string,
    amount: Decimal,
    options?: SettleLeaseOptions,
  ): Promise<DeductionResult>;

  /**
   * Release a lease without charging (work failed/aborted) — idempotent (H1).
   *
   * Transitions an ``active``/``expired`` lease to ``released`` and reports
   * ``released=true``; otherwise reports ``released=false`` with a ``reason``.
   */
  abstract releaseLease(userId: string, leaseId: string): Promise<ReleaseResult>;

  /**
   * Extend an active lease's TTL (long batch/agentic jobs, resolves B4).
   *
   * Returns ``error="lease_expired"`` if the TTL already elapsed and
   * ``error="lease_not_found"`` if missing/other-user/finalized.
   */
  abstract renewLease(userId: string, leaseId: string, ttlSeconds: number): Promise<LeaseResult>;

  /**
   * Advisory, non-locking read of ``available = balance − Σ active holds``.
   *
   * For UI only — never an admission gate (D4/H3); may be stale the instant read.
   */
  abstract getAvailable(userId: string): Promise<AvailableResult>;

  abstract getActivePricing(): Promise<PricingConfigResult | null>;
  abstract setActivePricing(config: PricingConfigData, label?: string | null): Promise<string>;

  // H8: pricing history / activation — parity with Python base.py:293-312.
  abstract getPricingHistory(): Promise<PricingConfigHistoryItem[]>;
  abstract getPricingConfig(version: number): Promise<PricingConfigResult | null>;
  abstract activatePricing(version: number): Promise<string>;

  // ── Plan management ────────────────────────────────────────────────
  abstract getUserPlan(userId: string): Promise<GetUserPlanResult>;
  abstract setUserPlan(userId: string, planId: string): Promise<SetUserPlanResult>;
  abstract unsetUserPlan(userId: string): Promise<{ userId: string }>;
  abstract checkFeature(userId: string, feature: string): Promise<CheckFeatureResult>;
  // periodStart overrides the window key for rolling_30d/anniversary plans
  // (resolved by CreditManager via resolveAllowanceWindow); undefined keeps
  // the calendar-month default (WS9).
  abstract checkAllowance(userId: string, periodStart?: Date | null): Promise<AllowanceResult>;
  abstract incrementUsageWindow(userId: string, planId: string, amount: Decimal): Promise<void>;

  /**
   * Advisory, non-locking read of invocation-count usage (UI only).
   *
   * Mirrors `checkSpendCap`/`checkAllowance`: the caller (the manager) has
   * already resolved the `FeatureLimit` from the user's plan and the calendar
   * window via `resolveCalendarWindow` — this method only counts. Counting is
   * ledger-derived (see `deductWithAllowance`): the number of committed
   * `usage` transactions with `metadata.feature === feature` in
   * `[periodStart, periodEnd)`. Never used for admission control.
   */
  abstract checkFeatureLimit(
    userId: string,
    feature: string,
    maxCalls: number,
    periodStart: Date,
    periodEnd: Date,
  ): Promise<FeatureLimitResult>;

  // ── Spend caps and rate limiting ────────────────────────────────────
  abstract checkSpendCap(
    userId: string,
    model?: string | null,
    amount?: Decimal,
  ): Promise<CapCheckResult>;

  // ── Refunds ────────────────────────────────────────────────────────
  abstract refundCredits(
    transactionId: string,
    amount?: Decimal,
    reason?: string,
    metadata?: CreditMetadata | null,
  ): Promise<RefundResult>;

  // ── Credit expiry ────────────────────────────────────────────────────
  /**
   * Sweep expired credit grants and debit the aggregate/tier balances.
   *
   * When `userId` is omitted (default), sweeps globally across every user —
   * unchanged behaviour/output shape from before this parameter existed. When
   * given, restricts the scan/expiry to that user's transactions only (used by
   * `CreditManager`'s lazy-on-read expiry, `options.lazyExpiry`).
   */
  abstract sweepExpiredCredits(dryRun?: boolean, userId?: string): Promise<SweepResult>;

  // ── Credit tiers ─────────────────────────────────────────────────────
  /**
   * Per-tier credit balances for a user (credit tiers).
   *
   * Sorted by `priority` ascending. When no tiers are configured, synthesizes
   * a single `"default"` entry from the aggregate balance so the shape is
   * uniform either way.
   */
  abstract getCreditTiers(userId: string): Promise<TierBalancesResult>;

  // ── Usage analytics (optional capability — WS8) ──────────────────────
  async spendByUser(_start: Date, _end: Date): Promise<SpendByUserRow[]> {
    throw new CapabilityNotSupportedError("spendByUser is not supported by this store");
  }
  async spendByModel(_start: Date, _end: Date): Promise<SpendByModelRow[]> {
    throw new CapabilityNotSupportedError("spendByModel is not supported by this store");
  }
  async topUsers(_limit: number, _start: Date, _end: Date): Promise<TopUserRow[]> {
    throw new CapabilityNotSupportedError("topUsers is not supported by this store");
  }
  async dailySpend(_start: Date, _end: Date): Promise<DailySpendRow[]> {
    throw new CapabilityNotSupportedError("dailySpend is not supported by this store");
  }
  async aggregateStats(_start: Date, _end: Date): Promise<AggregateStats> {
    throw new CapabilityNotSupportedError("aggregateStats is not supported by this store");
  }

  // ── Transaction listing (optional capability — WS8) ──────────────────
  async listUserTransactions(
    _userId: string,
    _options?: ListTransactionsOptions,
  ): Promise<PaginatedTransactions> {
    throw new CapabilityNotSupportedError("listUserTransactions is not supported by this store");
  }
  abstract listUsageEvents(
    userId: string,
    options?: ListUsageEventsOptions,
  ): Promise<PaginatedTransactions>;

  // ── Team/shared balance pools (optional capability — WS8) ────────────
  async createTeam(_name: string, _initialBalance?: Decimal): Promise<CreateTeamResult> {
    throw new CapabilityNotSupportedError("createTeam is not supported by this store");
  }
  async getTeamBalance(_teamId: string): Promise<TeamBalanceResult> {
    throw new CapabilityNotSupportedError("getTeamBalance is not supported by this store");
  }
  async addTeamMember(
    _teamId: string,
    _userId: string,
    _role?: string,
    _spendCap?: Decimal | null,
  ): Promise<AddTeamMemberResult> {
    throw new CapabilityNotSupportedError("addTeamMember is not supported by this store");
  }
  async getTeamMembers(_teamId: string): Promise<TeamMember[]> {
    throw new CapabilityNotSupportedError("getTeamMembers is not supported by this store");
  }
  async deductTeam(
    _teamId: string,
    _userId: string,
    _amount: Decimal,
    _metadata?: CreditMetadata | null,
    _idempotencyKey?: string | null,
  ): Promise<TeamDeductionResult> {
    throw new CapabilityNotSupportedError("deductTeam is not supported by this store");
  }
}
