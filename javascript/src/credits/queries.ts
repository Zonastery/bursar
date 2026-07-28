import type { CreditStore } from "./store.js";
import type {
  AggregateStats,
  AvailableResult,
  BursarConfigResult,
  CheckFeatureResult,
  DailySpendRow,
  FeatureLimitResult,
  GetUserPlanResult,
  LedgerEntry,
  LedgerPage,
  ListLedgerEntriesOptions,
  ListQuotaEventsOptions,
  ListUsageEntriesOptions,
  MigratePlanUsersResult,
  PlanMigrationBatchResult,
  PlanMigrationStartResult,
  QuotaEvent,
  QuotaState,
  SpendByModelRow,
  SpendByUserRow,
  TopUserRow,
} from "./types/index.js";

/**
 * Stable read/catalog facade over CreditStore.
 *
 * CreditsService adds pricing, eventing, expiry, and charging workflows on top;
 * methods here intentionally contain no additional business behavior.
 */
export class CreditQueries {
  constructor(private readonly store: CreditStore) {}

  async getActivePricing(): Promise<BursarConfigResult | null> {
    return this.store.getActivePricing();
  }

  async getUserPlan(userId: string): Promise<GetUserPlanResult> {
    return this.store.getUserPlan(userId);
  }

  async checkFeature(userId: string, feature: string): Promise<CheckFeatureResult> {
    return this.store.checkFeature(userId, feature);
  }

  async getQuotaState(userId: string, quotaKey?: string | null): Promise<QuotaState[]> {
    return this.store.getQuotaState(userId, quotaKey);
  }

  async listQuotaEvents(userId: string, options?: ListQuotaEventsOptions): Promise<QuotaEvent[]> {
    return this.store.listQuotaEvents(userId, options);
  }

  /** @deprecated Use `getQuotaState`; `feature` is interpreted as a quota key. */
  async checkFeatureLimit(userId: string, feature: string): Promise<FeatureLimitResult> {
    return this.store.checkFeatureLimit(userId, feature);
  }

  /** @deprecated Prefer resumable migrations for large populations. */
  async migratePlanUsers(
    planKey: string,
    targetConfigVersion?: number | null,
  ): Promise<MigratePlanUsersResult> {
    return this.store.migratePlanUsers(planKey, targetConfigVersion);
  }

  async startPlanMigration(
    fromPlanId: string | null,
    toPlanId: string,
  ): Promise<PlanMigrationStartResult> {
    return this.store.startPlanMigration(fromPlanId, toPlanId);
  }

  async migratePlanBatch(
    migrationId: string,
    batchSize?: number,
  ): Promise<PlanMigrationBatchResult> {
    return this.store.migratePlanBatch(migrationId, batchSize);
  }

  async revokeCreditsByEntryType(
    userId: string,
    entryType: string,
  ): Promise<Record<string, unknown>> {
    return this.store.revokeCreditsByEntryType(userId, entryType);
  }

  async getLedgerEntry(userId: string, entryId: string): Promise<LedgerEntry | null> {
    return this.store.getLedgerEntry(userId, entryId);
  }

  async getAvailable(userId: string): Promise<AvailableResult> {
    return this.store.getAvailable(userId);
  }

  async aggregateStats(start: Date, end: Date): Promise<AggregateStats> {
    return this.store.aggregateStats(start, end);
  }

  async spendByUser(start: Date, end: Date): Promise<SpendByUserRow[]> {
    return this.store.spendByUser(start, end);
  }

  async spendByModel(start: Date, end: Date): Promise<SpendByModelRow[]> {
    return this.store.spendByModel(start, end);
  }

  async listLedgerEntries(userId: string, options?: ListLedgerEntriesOptions): Promise<LedgerPage> {
    return this.store.listLedgerEntries(userId, options);
  }

  async listUsageEntries(userId: string, options?: ListUsageEntriesOptions): Promise<LedgerPage> {
    return this.store.listUsageEntries(userId, options);
  }

  async topUsers(limit: number, start: Date, end: Date): Promise<TopUserRow[]> {
    return this.store.topUsers(limit, start, end);
  }

  async dailySpend(start: Date, end: Date): Promise<DailySpendRow[]> {
    return this.store.dailySpend(start, end);
  }
}
