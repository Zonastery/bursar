import type { Decimal } from "decimal.js";
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
  GetUserPlanResult,
  LeaseResult,
  ListTransactionsOptions,
  ListUsageEventsOptions,
  PaginatedTransactions,
  PricingConfigData,
  PricingConfigResult,
  RefundResult,
  ReleaseResult,
  ReserveResult,
  SetUserPlanResult,
  SetupResult,
  SpendByModelRow,
  SpendByUserRow,
  SweepResult,
  TeamBalanceResult,
  TeamDeductionResult,
  TeamMember,
  TopUserRow,
} from "../types.js";

/** Options for atomically acquiring a lease (interface plan §3 / D4). */
export interface CreateLeaseOptions {
  billingMode?: BillingMode;
  floor?: Decimal;
  maxConcurrent?: number | null;
  ttlSeconds?: number;
  model?: string | null;
  overdraftFloor?: Decimal | null;
  metadata?: CreditMetadata | null;
}

/** Options for charging the actual cost against a lease (interface plan §3 / D5). */
export interface SettleLeaseOptions {
  idempotencyKey?: string | null;
  minBalance?: Decimal;
  model?: string | null;
  metadata?: CreditMetadata | null;
}

/** Interface for credit storage backends. */
export interface CreditStore {
  setup(databaseUrl?: string | null): Promise<SetupResult>;
  getBalance(userId: string): Promise<BalanceResult>;
  addCredits(
    userId: string,
    amount: Decimal,
    type?: string,
    metadata?: CreditMetadata | null,
    expiresAt?: Date | null,
  ): Promise<AddCreditsResult>;
  reserveCredits(
    userId: string,
    amount: Decimal,
    operationType: string,
    metadata?: CreditMetadata | null,
    minBalance?: Decimal,
  ): Promise<ReserveResult>;
  deductCredits(
    userId: string,
    reservationId: string,
    amount: Decimal,
    idempotencyKey?: string | null,
    metadata?: CreditMetadata | null,
  ): Promise<DeductionResult>;
  /**
   * Atomically calculate-and-charge in one server-side transaction:
   * consume free allowance, enforce spend caps, apply the balance floor,
   * and debit the net amount — idempotency-keyed end-to-end. See contract §2.
   */
  deductWithAllowance(
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
  createLease(
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
  settleLease(
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
  releaseLease(userId: string, leaseId: string): Promise<ReleaseResult>;

  /**
   * Extend an active lease's TTL (long batch/agentic jobs, resolves B4).
   *
   * Returns ``error="lease_expired"`` if the TTL already elapsed and
   * ``error="lease_not_found"`` if missing/other-user/finalized.
   */
  renewLease(userId: string, leaseId: string, ttlSeconds: number): Promise<LeaseResult>;

  /**
   * Advisory, non-locking read of ``available = balance − Σ active holds``.
   *
   * For UI only — never an admission gate (D4/H3); may be stale the instant read.
   */
  getAvailable(userId: string): Promise<AvailableResult>;

  getActivePricing(): Promise<PricingConfigResult | null>;
  setActivePricing(config: PricingConfigData, label?: string | null): Promise<string>;

  // ── Plan management ────────────────────────────────────────────────
  getUserPlan(userId: string): Promise<GetUserPlanResult>;
  setUserPlan(userId: string, planId: string): Promise<SetUserPlanResult>;
  checkFeature(userId: string, feature: string): Promise<CheckFeatureResult>;
  checkAllowance(userId: string): Promise<AllowanceResult>;
  incrementUsageWindow(userId: string, planId: string, amount: Decimal): Promise<void>;

  // ── Spend caps and rate limiting ────────────────────────────────────
  checkSpendCap(userId: string, model?: string | null, amount?: Decimal): Promise<CapCheckResult>;

  // ── Refunds ────────────────────────────────────────────────────────
  refundCredits(
    transactionId: string,
    amount?: Decimal,
    reason?: string,
    metadata?: CreditMetadata | null,
  ): Promise<RefundResult>;

  // ── Credit expiry ────────────────────────────────────────────────────
  sweepExpiredCredits(dryRun?: boolean): Promise<SweepResult>;

  // ── Usage analytics ──────────────────────────────────────────────────
  spendByUser(start: Date, end: Date): Promise<SpendByUserRow[]>;
  spendByModel(start: Date, end: Date): Promise<SpendByModelRow[]>;
  topUsers(limit: number, start: Date, end: Date): Promise<TopUserRow[]>;
  dailySpend(start: Date, end: Date): Promise<DailySpendRow[]>;

  // ── Transaction listing ─────────────────────────────────────────────
  listUserTransactions(
    userId: string,
    options?: ListTransactionsOptions,
  ): Promise<PaginatedTransactions>;
  listUsageEvents(userId: string, options?: ListUsageEventsOptions): Promise<PaginatedTransactions>;

  // ── Aggregate stats ────────────────────────────────────────────────
  aggregateStats(start: Date, end: Date): Promise<AggregateStats>;

  // ── Team/shared balance pools ──────────────────────────────────────
  createTeam(name: string, initialBalance?: Decimal): Promise<CreateTeamResult>;
  getTeamBalance(teamId: string): Promise<TeamBalanceResult>;
  addTeamMember(
    teamId: string,
    userId: string,
    role?: string,
    spendCap?: Decimal | null,
  ): Promise<AddTeamMemberResult>;
  getTeamMembers(teamId: string): Promise<TeamMember[]>;
  deductTeam(
    teamId: string,
    userId: string,
    amount: Decimal,
    metadata?: CreditMetadata | null,
    idempotencyKey?: string | null,
  ): Promise<TeamDeductionResult>;
}
