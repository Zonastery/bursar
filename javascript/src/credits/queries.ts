import type { CreditStore } from "./store.js";
import type {
  AggregateStats,
  AvailableResult,
  CatalogRevision,
  CheckFeatureResult,
  DailySpendRow,
  GetUserPlanResult,
  LedgerEntry,
  LedgerPage,
  ListLedgerEntriesOptions,
  ListQuotaEventsOptions,
  ListUsageChargesOptions,
  ListUsageEntriesOptions,
  PlanMigrationBatchResult,
  PlanMigrationStartResult,
  RevokeCreditsResult,
  QuotaEvent,
  QuotaState,
  SpendByModelRow,
  SpendByUserRow,
  TopUserRow,
  UsageChargePage,
  UsageAnalyticsStore,
  UsageChargeStore,
} from "./types/index.js";

/**
 * Stable read facade over CreditStore.
 *
 * CreditsService adds catalog-aware pricing, eventing, expiry, and charging workflows on top;
 * methods here intentionally contain no additional business behavior.
 */
export class CreditQueries {
  constructor(
    private readonly store: CreditStore,
    private readonly analytics: UsageAnalyticsStore = store,
    private readonly usageStore: UsageChargeStore = store,
  ) {}

  async getActiveCatalog(): Promise<CatalogRevision | null> {
    return this.store.getActiveCatalog();
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

  async revokeCreditsByEntryType(userId: string, entryType: string): Promise<RevokeCreditsResult> {
    return this.store.revokeCreditsByEntryType(userId, entryType);
  }

  async getLedgerEntry(userId: string, entryId: string): Promise<LedgerEntry | null> {
    return this.store.getLedgerEntry(userId, entryId);
  }

  async getAvailable(userId: string): Promise<AvailableResult> {
    return this.store.getAvailable(userId);
  }

  async aggregateStats(start: Date, end: Date): Promise<AggregateStats> {
    return this.analytics.aggregateStats(start, end);
  }

  async spendByUser(start: Date, end: Date): Promise<SpendByUserRow[]> {
    return this.analytics.spendByUser(start, end);
  }

  async spendByModel(start: Date, end: Date): Promise<SpendByModelRow[]> {
    return this.analytics.spendByModel(start, end);
  }

  async listLedgerEntries(userId: string, options?: ListLedgerEntriesOptions): Promise<LedgerPage> {
    return this.store.listLedgerEntries(userId, options);
  }

  async listUsageEntries(userId: string, options?: ListUsageEntriesOptions): Promise<LedgerPage> {
    return this.store.listUsageEntries(userId, options);
  }

  async listUsageCharges(
    userId: string,
    options?: ListUsageChargesOptions,
  ): Promise<UsageChargePage> {
    return this.usageStore.listUsageCharges(userId, options);
  }

  async topUsers(limit: number, start: Date, end: Date): Promise<TopUserRow[]> {
    return this.analytics.topUsers(limit, start, end);
  }

  async dailySpend(start: Date, end: Date): Promise<DailySpendRow[]> {
    return this.analytics.dailySpend(start, end);
  }
}
