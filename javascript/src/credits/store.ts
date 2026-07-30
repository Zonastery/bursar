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
  BucketBalancesResult,
  CheckFeatureResult,
  CreateTeamResult,
  CreditMetadata,
  DailySpendRow,
  DeductionResult,
  DeductWithAllowanceOptions,
  ExecuteGrantProgramRequest,
  FeatureLimitResult,
  GrantProgramAwardResult,
  GetUserPlanResult,
  ListQuotaEventsOptions,
  MigratePlanUsersResult,
  PlanMigrationBatchResult,
  PlanMigrationStartResult,
  QuotaEvent,
  QuotaState,
  LeaseResult,
  LeasePricingContext,
  ListLedgerEntriesOptions,
  ListUsageEntriesOptions,
  LedgerPage,
  BursarConfigHistoryItem,
  BursarConfigResult,
  RefundResult,
  ReleaseResult,
  SetUserPlanResult,
  SpendByModelRow,
  SpendByUserRow,
  SweepResult,
  TeamBalanceResult,
  TeamDeductionResult,
  TeamMember,
  TopUserRow,
  LedgerEntry,
} from "./types/index.js";

export interface OperationUsageOptions {
  /** Entitlement feature that must be enabled for the operation. */
  feature?: string | null;
  model?: string | null;
  region?: string | null;
  measures?: Record<string, unknown> | null;
  dimensions?: Record<string, unknown> | null;
}

/** Options for atomically acquiring a lease (interface plan §3 / D4). */
export interface CreateLeaseOptions extends OperationUsageOptions {
  /**
   * Replay-safe acquisition key. Supplying the same key with the same request
   * returns the original lease; conflicting reuse is rejected.
   */
  idempotencyKey?: string | null;
  billingMode?: BillingMode;
  floor?: Decimal;
  maxConcurrent?: number | null;
  overdraftFloor?: Decimal | null;
  ttlSeconds?: number;
  metadata?: CreditMetadata | null;
}

/** Options for charging the actual cost against a lease (interface plan §3 / D5). */
export interface SettleLeaseOptions extends OperationUsageOptions {
  idempotencyKey?: string | null;
  metadata?: CreditMetadata | null;
}

/**
 * Abstract base for credit storage backends (WS8).
 *
 * Split into two tiers:
 *  - **Core** (abstract, must be implemented): balance/credit ops, the atomic
 *    lease lifecycle, bursar-config versioning, plan management, spend caps,
 *    refunds, and expiry sweeping. Every backend needs these.
 *  - **Optional capabilities** (concrete, default-throwing): usage analytics,
 *    ledger listing, and shared team-balance pools. A custom store that
 *    doesn't need these can skip them entirely — the default implementation
 *    throws {@link CapabilityNotSupportedError} instead of forcing a stub.
 */
export abstract class CreditStore {
  constructor() {}

  abstract getBalance(userId: string): Promise<BalanceResult>;
  abstract addCredits(
    userId: string,
    amount: Decimal,
    type?: string,
    metadata?: CreditMetadata | null,
    expiresAt?: Date | null,
    /** Target credit bucket; omitted/`null` resolves to the config's default bucket. */
    bucket?: string | null,
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
  // ``reserve``/``settle``/``release`` on the manager map onto these.
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

  /** Read the immutable catalog and rate card captured at lease admission. */
  abstract getLeasePricingContext(
    userId: string,
    leaseId: string,
  ): Promise<LeasePricingContext | null>;

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

  /** Expire a bounded batch of abandoned leases and release their reservations. */
  async expireLeases(_limit?: number): Promise<number> {
    throw new CapabilityNotSupportedError("expireLeases is not supported by this store");
  }

  /**
   * Advisory, non-locking read of ``available = balance − Σ active holds``.
   *
   * For UI only — never an admission gate (D4/H3); may be stale the instant read.
   */

  abstract getAvailable(userId: string): Promise<AvailableResult>;

  abstract getActivePricing(): Promise<BursarConfigResult | null>;
  abstract setActivePricing(
    config: Record<string, unknown>,
    label?: string | null,
  ): Promise<string>;
  abstract publishPricing(config: Record<string, unknown>, label?: string | null): Promise<string>;

  // H8: pricing history / activation — parity with Python base.py:293-312.
  abstract getPricingHistory(): Promise<BursarConfigHistoryItem[]>;
  abstract getBursarConfig(version: number): Promise<BursarConfigResult | null>;
  abstract activatePricing(version: number): Promise<string>;

  /** @deprecated Prefer the resumable migration methods for large populations. */
  abstract migratePlanUsers(
    planKey: string,
    targetConfigVersion?: number | null,
  ): Promise<MigratePlanUsersResult>;
  abstract getUserPlan(userId: string): Promise<GetUserPlanResult>;
  abstract setUserPlan(
    userId: string,
    planId: string,
    planAssignedAt?: Date | null,
  ): Promise<SetUserPlanResult>;
  abstract unsetUserPlan(userId: string): Promise<{ userId: string }>;
  abstract startPlanMigration(
    fromPlanId: string | null,
    toPlanId: string,
  ): Promise<PlanMigrationStartResult>;
  abstract migratePlanBatch(
    migrationId: string,
    batchSize?: number,
  ): Promise<PlanMigrationBatchResult>;
  abstract checkFeature(userId: string, feature: string): Promise<CheckFeatureResult>;
  abstract getQuotaState(userId: string, quotaKey?: string | null): Promise<QuotaState[]>;
  abstract listQuotaEvents(userId: string, options?: ListQuotaEventsOptions): Promise<QuotaEvent[]>;
  /** @deprecated Use `getQuotaState`; `feature` is interpreted as the quota key. */
  abstract checkFeatureLimit(userId: string, feature: string): Promise<FeatureLimitResult>;
  abstract checkAllowance(userId: string): Promise<AllowanceResult>;

  abstract revokeCreditsByEntryType(
    userId: string,
    entryType: string,
  ): Promise<Record<string, unknown>>;

  // ── Refunds ────────────────────────────────────────────────────────
  abstract refundCredits(
    entryId: string,
    amount?: Decimal,
    reason?: string,
    metadata?: CreditMetadata | null,
    /** Replay-safe key; pass a unique key for multiple equal partial refunds. */
    idempotencyKey?: string | null,
  ): Promise<RefundResult>;

  // ── Credit expiry ────────────────────────────────────────────────────
  /**
   * Sweep expired credit grants and debit the aggregate/tier balances.
   *
   * When `userId` is omitted (default), sweeps globally across every user —
   * unchanged behaviour/output shape from before this parameter existed. When
   * given, restricts the scan/expiry to that user's transactions only (used by
   * bounded expiry sweep operation).
   */
  abstract sweepExpiredCredits(
    dryRun?: boolean,
    userId?: string,
    limit?: number,
  ): Promise<SweepResult>;

  // ── Credit buckets ─────────────────────────────────────────────────────
  /**
   * Per-bucket credit balances for a user (credit buckets).
   *
   * Sorted by `priority` ascending. When no buckets are configured, synthesizes
   * a single `"default"` entry from the aggregate balance so the shape is
   * uniform either way.
   */
  abstract getBucketBalances(userId: string): Promise<BucketBalancesResult>;

  /** Execute one configured grant-program event. */
  async executeGrantProgram(
    _request: ExecuteGrantProgramRequest,
  ): Promise<GrantProgramAwardResult[]> {
    throw new CapabilityNotSupportedError("executeGrantProgram is not supported by this store");
  }

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
  async listLedgerEntries(
    _userId: string,
    _options?: ListLedgerEntriesOptions,
  ): Promise<LedgerPage> {
    throw new CapabilityNotSupportedError("listLedgerEntries is not supported by this store");
  }
  abstract listUsageEntries(userId: string, options?: ListUsageEntriesOptions): Promise<LedgerPage>;

  // ── Single transaction lookup (optional capability — WS8) ────────────
  /**
   * Fetch a single transaction by ID. Returns `null` when the transaction
   * does not exist or belongs to a different user.
   */
  async getLedgerEntry(_userId: string, _entryId: string): Promise<LedgerEntry | null> {
    throw new CapabilityNotSupportedError("getLedgerEntry is not supported by this store");
  }

  // ── Team/shared balance pools (optional capability — WS8) ────────────
  async createTeam(
    _ownerSubjectId: string,
    _name: string,
    _initialBalance?: Decimal,
  ): Promise<CreateTeamResult> {
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
  async removeTeamMember(_teamId: string, _userId: string): Promise<boolean> {
    throw new CapabilityNotSupportedError("removeTeamMember is not supported by this store");
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
