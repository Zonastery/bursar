import type { Decimal } from "decimal.js";
import type {
  AddCreditsResult,
  AddTeamMemberResult,
  AggregateStats,
  AllowanceResult,
  BalanceResult,
  CapCheckResult,
  CheckFeatureResult,
  CreateTeamResult,
  CreditMetadata,
  DailySpendRow,
  DeductionResult,
  DeductWithAllowanceOptions,
  GetUserPlanResult,
  ListTransactionsOptions,
  ListUsageEventsOptions,
  PaginatedTransactions,
  PricingConfigData,
  PricingConfigResult,
  RefundResult,
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
