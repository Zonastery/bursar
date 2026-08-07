import Decimal from "decimal.js";
import { randomUUID } from "node:crypto";
import { StoreClosedError, StoreError } from "../../errors.js";
import {
  canonicalBursarConfigDict,
  canonicalCatalogRolloutDict,
  loadCatalogRollout,
  loadConfigFromDict,
  validateCatalogRollout,
  type CatalogRollout,
} from "../../config.js";
import {
  PostgresClient,
  type PostgresConnectionOptions,
  type PostgresPool,
  type PostgresPoolConstructor,
} from "../../shared/postgres-client.js";
import type {
  AddCreditsResult,
  AddTeamMemberResult,
  AggregateStats,
  AllowanceResult,
  AvailableResult,
  BalanceResult,
  CheckFeatureResult,
  CreateTeamResult,
  CreditMetadata,
  DailySpendRow,
  DeductionResult,
  DeductWithAllowanceOptions,
  ExecuteGrantProgramRequest,
  GrantProgramAwardResult,
  GetUserPlanResult,
  LeaseResult,
  LeasePricingContext,
  ListLedgerEntriesOptions,
  ListUsageChargesOptions,
  ListQuotaEventsOptions,
  ListUsageEntriesOptions,
  PlanMigrationBatchResult,
  PlanMigrationStartResult,
  QuotaEvent,
  QuotaState,
  LedgerPage,
  UsageChargePage,
  UsageRecordResult,
  CatalogRevisionSummary,
  CatalogRevision,
  RefundResult,
  ReleaseResult,
  SetUserPlanResult,
  SpendByModelRow,
  SpendByUserRow,
  SweepResult,
  TeamBalanceResult,
  TeamDeductionResult,
  TeamMember,
  BucketBalance,
  BucketBalancesResult,
  TopUserRow,
  LedgerEntry,
} from "../types/index.js";
import { CreditStore } from "../store.js";
import type { CreateLeaseOptions, SettleLeaseOptions } from "../store.js";
import { BalanceRepository } from "./repositories/balance.js";
import { DeductionRepository } from "./repositories/deduction.js";
import { LeaseRepository } from "./repositories/lease.js";
import { CatalogRepository } from "./repositories/catalog.js";
import { PlanRepository } from "./repositories/plan.js";
import { AnalyticsRepository } from "./repositories/analytics.js";
import { TeamRepository } from "./repositories/team.js";
import { BucketRepository } from "./repositories/bucket.js";
import {
  ZERO,
  decimalParameter as decParam,
  decimalRecord as decRecord,
  decimalValue as dec,
  mapLedgerEntry,
  mapUsageCharge,
  normalizeCatalogRevision,
  parseAdmissionOperations,
  parseEntitlements,
} from "./value-mappers.js";

const DEFAULT_LEASE_TTL_SECONDS = 600;
const DEFAULT_PAGE_SIZE = 50;
const MAX_PAGE_SIZE = 200;

function requireText(value: unknown, context: string): string {
  if (typeof value !== "string" || value.length === 0) {
    throw new StoreError(`${context} returned a missing or invalid identifier`);
  }
  return value;
}

export type PgPool = PostgresPool;
export type PgPoolConstructor = PostgresPoolConstructor;

/** Construction options for the PostgreSQL credit store. */
export interface PostgresStoreOptions extends PostgresConnectionOptions {
  /** PostgreSQL connection string or an application-owned pool. */
  postgres: string | PgPool;
  /** Tenant UUID bound to every store transaction. */
  tenantId: string;
  /** Injectable `pg.Pool` constructor for custom runtimes and tests. */
  poolConstructor?: PgPoolConstructor;
  usageBackend?: "postgres" | "clickhouse";
}

export class PostgresStore extends CreditStore {
  private readonly postgres: PostgresClient;

  private _balanceRepo: BalanceRepository | null = null;
  private _deductionRepo: DeductionRepository | null = null;
  private _leaseRepo: LeaseRepository | null = null;
  private _catalogRepo: CatalogRepository | null = null;
  private _planRepo: PlanRepository | null = null;
  private _analyticsRepo: AnalyticsRepository | null = null;
  private _teamRepo: TeamRepository | null = null;
  private _bucketRepo: BucketRepository | null = null;

  private get balanceRepo(): BalanceRepository {
    if (!this._balanceRepo) {
      this._balanceRepo = new BalanceRepository(this.callproc.bind(this));
    }
    return this._balanceRepo;
  }

  private get deductionRepo(): DeductionRepository {
    if (!this._deductionRepo) {
      this._deductionRepo = new DeductionRepository(this.callproc.bind(this));
    }
    return this._deductionRepo;
  }

  private get leaseRepo(): LeaseRepository {
    if (!this._leaseRepo) {
      this._leaseRepo = new LeaseRepository(this.callproc.bind(this));
    }
    return this._leaseRepo;
  }

  private get catalogRepo(): CatalogRepository {
    if (!this._catalogRepo) {
      this._catalogRepo = new CatalogRepository(this.callproc.bind(this));
    }
    return this._catalogRepo;
  }

  private get planRepo(): PlanRepository {
    if (!this._planRepo) {
      this._planRepo = new PlanRepository(this.callproc.bind(this));
    }
    return this._planRepo;
  }

  private get analyticsRepo(): AnalyticsRepository {
    if (!this._analyticsRepo) {
      this._analyticsRepo = new AnalyticsRepository(this.callproc.bind(this));
    }
    return this._analyticsRepo;
  }

  private get teamRepo(): TeamRepository {
    if (!this._teamRepo) {
      this._teamRepo = new TeamRepository(this.callproc.bind(this));
    }
    return this._teamRepo;
  }

  private get bucketRepo(): BucketRepository {
    if (!this._bucketRepo) {
      this._bucketRepo = new BucketRepository(this.callproc.bind(this));
    }
    return this._bucketRepo;
  }

  constructor(options: PostgresStoreOptions) {
    super();
    if (typeof options !== "object" || options === null) {
      throw new TypeError("PostgresStore options are required");
    }
    if (typeof options.postgres !== "string" && options.poolConstructor !== undefined) {
      throw new TypeError("poolConstructor cannot be used with an existing PostgreSQL pool");
    }
    this.postgres = new PostgresClient(options.postgres, {
      tenantId: options.tenantId,
      usageBackend: options.usageBackend,
      poolConstructor: options.poolConstructor,
      connectionTimeoutMs: options.connectionTimeoutMs,
      statementTimeoutMs: options.statementTimeoutMs,
      idleTransactionTimeoutMs: options.idleTransactionTimeoutMs,
      idleTimeoutMs: options.idleTimeoutMs,
      maxConnections: options.maxConnections,
      applicationName: options.applicationName,
      onPoolError: options.onPoolError,
      closedError: () => new StoreClosedError("Credit store has been closed"),
    });
  }

  private async query(text: string, params?: unknown[]): Promise<unknown[]> {
    return this.postgres.query(text, params);
  }

  async close(): Promise<void> {
    await this.postgres.close();
  }

  private static readonly RPC_NAME_RE = /^[a-z_][a-z0-9_]*$/;
  private static readonly SCALAR_RPC_NAMES = new Set([
    "apply_due_plan_assignment_changes",
    "assign_plan",
    "release_lease",
    "remove_team_member",
    "set_plan_revision_pin",
    "set_team_member",
    "start_plan_migration",
    "unassign_plan",
  ]);

  private async callproc(name: string, params: unknown[]): Promise<unknown[]> {
    if (!PostgresStore.RPC_NAME_RE.test(name)) {
      throw new StoreError(`Invalid RPC name: ${name}`);
    }
    const placeholders = params.map((_, i) => `$${i + 1}`).join(", ");
    const rows = await this.query(`SELECT * FROM bursar.${name}(${placeholders})`, params);
    if (PostgresStore.SCALAR_RPC_NAMES.has(name) && rows.length === 1) {
      const row = rows[0] as Record<string, unknown>;
      const keys = Object.keys(row);
      const key = keys[0];
      if (keys.length === 1 && key !== undefined) {
        return [row[key]];
      }
    }
    return rows;
  }

  async getBalance(userId: string): Promise<BalanceResult> {
    const row = await this.balanceRepo.getBalance(userId);
    if (!row) {
      return { userId, balance: ZERO, lifetimePurchased: ZERO };
    }
    return {
      userId: String(row.user_id ?? userId),
      balance: dec(row.balance),
      lifetimePurchased: dec(row.lifetime_purchased),
    };
  }

  async addCredits(
    userId: string,
    amount: Decimal,
    type = "adjustment",
    metadata?: CreditMetadata | null,
    expiresAt?: Date | null,
    bucket?: string | null,
    idempotencyKey?: string | null,
  ): Promise<AddCreditsResult> {
    const meta: Record<string, unknown> = { ...(metadata ?? {}) };
    if (expiresAt) {
      meta.expires_at = expiresAt instanceof Date ? expiresAt.toISOString() : String(expiresAt);
    }
    const row = await this.balanceRepo.addCredits(
      userId,
      decParam(amount),
      type,
      JSON.stringify(meta),
      expiresAt ? expiresAt.toISOString() : null,
      bucket ?? null,
      idempotencyKey ?? `credit:${randomUUID()}`,
    );
    if ("error" in row && row.error) {
      throw new StoreError(`post_credit: ${String(row.error)}`);
    }
    return {
      entryId: requireText(row.entry_id, "post_credit"),
      userId: String(row.user_id ?? userId),
      amount: dec(row.amount),
      newBalance: dec(row.new_balance),
      lifetimePurchased: dec(row.lifetime_purchased),
      bucket: String(row.bucket ?? "default"),
      idempotent: Boolean(row.idempotent),
    };
  }

  async deductWithAllowance(
    userId: string,
    amount: Decimal,
    options?: DeductWithAllowanceOptions,
  ): Promise<DeductionResult> {
    const idempotencyKey = options?.idempotencyKey ?? `usage:${randomUUID()}`;
    const operation =
      options?.operation ??
      (typeof options?.metadata?.operation === "string" ? options.metadata.operation : "usage");
    const model = options?.model ?? null;
    const region = options?.region ?? null;
    const metadata = options?.metadata ?? {};
    const feature = options?.feature ?? null;
    const measures = options?.measures ?? {};
    const dimensions = options?.dimensions ?? {};

    const row = await this.deductionRepo.deductWithAllowance({
      userId,
      operation,
      amount: decParam(amount),
      idempotencyKey,
      feature,
      model,
      region,
      measures: JSON.stringify(measures),
      dimensions: JSON.stringify(dimensions),
      metadata: JSON.stringify(metadata ?? {}),
    });

    if ("error" in row && row.error) {
      return {
        entryId: null,
        usageChargeId: row.charge_id != null ? String(row.charge_id) : null,
        userId,
        amount: dec(row.amount),
        allowanceConsumed: dec(row.allowance_consumed),
        balanceAfter: row.balance_after == null ? null : dec(row.balance_after),
        idempotent: false,
        error: String(row.error),
        bucketBreakdown: null,
      };
    }

    return {
      entryId:
        row.entry_id == null ? null : requireText(row.entry_id, "charge_usage_for_operation"),
      usageChargeId: requireText(row.charge_id, "charge_usage_for_operation"),
      userId,
      amount: dec(row.amount),
      allowanceConsumed: dec(row.allowance_consumed),
      balanceAfter: dec(row.balance_after),
      idempotent: Boolean(row.idempotent),
      error: null,
      bucketBreakdown: decRecord(row.bucket_breakdown),
    };
  }

  async recordUsage(
    userId: string,
    operation: string,
    amount: Decimal,
    options?: DeductWithAllowanceOptions,
  ): Promise<UsageRecordResult> {
    const idempotencyKey = options?.idempotencyKey ?? `usage-record:${randomUUID()}`;
    const row = await this.deductionRepo.recordUsage({
      userId,
      operation,
      amount: decParam(amount),
      idempotencyKey,
      feature: options?.feature ?? null,
      model: options?.model ?? null,
      region: options?.region ?? null,
      measures: JSON.stringify(options?.measures ?? {}),
      dimensions: JSON.stringify(options?.dimensions ?? {}),
      metadata: JSON.stringify(options?.metadata ?? {}),
    });
    if (row.error_code !== null) {
      return {
        usageId: null,
        userId,
        requested: dec(row.requested),
        idempotent: false,
        error: row.error_code,
      };
    }
    return {
      usageId: requireText(row.charge_id, "record_usage"),
      userId,
      requested: dec(row.requested),
      idempotent: Boolean(row.replayed),
      error: null,
    };
  }

  async createLease(
    userId: string,
    amount: Decimal,
    operationType: string,
    options?: CreateLeaseOptions,
  ): Promise<LeaseResult> {
    const row = await this.leaseRepo.createLease({
      userId,
      amount: decParam(amount),
      operationType,
      idempotencyKey: options?.idempotencyKey ?? `lease:${randomUUID()}`,
      ttlSeconds: options?.ttlSeconds ?? DEFAULT_LEASE_TTL_SECONDS,
      metadata: JSON.stringify(options?.metadata ?? {}),
      feature: options?.feature ?? null,
      measures: JSON.stringify(options?.measures ?? {}),
      dimensions: JSON.stringify(options?.dimensions ?? {}),
      minimumBalance: options?.floor == null ? null : decParam(options.floor),
      maxConcurrent: options?.maxConcurrent ?? null,
    });

    const availability = await this.getAvailable(userId);
    if ("error" in row && row.error) {
      return {
        leaseId: null,
        userId,
        amount: row.amount == null ? null : dec(row.amount),
        available: availability.available,
        reservedTotal: availability.reserved,
        minimumBalance: row.minimum_balance == null ? null : dec(row.minimum_balance),
        billingMode: options?.billingMode ?? "strict",
        expiresAt: null,
        error: String(row.error),
      };
    }
    return {
      leaseId: requireText(row.lease_id, "create_lease_for_operation"),
      userId: String(row.user_id ?? userId),
      amount: dec(row.amount),
      available: availability.available,
      reservedTotal: availability.reserved,
      minimumBalance: dec(row.minimum_balance),
      billingMode: dec(row.minimum_balance).lt(0) ? "overdraft" : "strict",
      expiresAt: requireText(row.expires_at, "create_lease_for_operation"),
      error: null,
    };
  }

  async settleLease(
    userId: string,
    leaseId: string,
    amount: Decimal,
    options?: SettleLeaseOptions,
  ): Promise<DeductionResult> {
    const row = await this.leaseRepo.settleLease({
      userId,
      leaseId,
      amount: decParam(amount),
      idempotencyKey: options?.idempotencyKey ?? `lease:${leaseId}:settle`,
      feature: options?.feature ?? null,
      model: options?.model ?? null,
      region: options?.region ?? null,
      measures: JSON.stringify(options?.measures ?? {}),
      dimensions: JSON.stringify(options?.dimensions ?? {}),
      metadata: JSON.stringify(options?.metadata ?? {}),
    });

    if ("error" in row && row.error) {
      return {
        entryId: null,
        usageChargeId: row.charge_id != null ? String(row.charge_id) : null,
        userId,
        amount: dec(row.amount),
        allowanceConsumed: dec(row.allowance_consumed),
        balanceAfter: row.balance_after == null ? null : dec(row.balance_after),
        idempotent: false,
        error: String(row.error),
        bucketBreakdown: null,
      };
    }
    return {
      entryId: row.entry_id == null ? null : requireText(row.entry_id, "settle_lease"),
      usageChargeId: requireText(row.charge_id, "settle_lease"),
      userId,
      amount: dec(row.amount),
      allowanceConsumed: dec(row.allowance_consumed),
      balanceAfter: dec(row.balance_after),
      idempotent: Boolean(row.idempotent),
      error: null,
      bucketBreakdown: decRecord(row.bucket_breakdown),
    };
  }

  async getLeasePricingContext(
    userId: string,
    leaseId: string,
  ): Promise<LeasePricingContext | null> {
    const row = await this.leaseRepo.getPricingContext(userId, leaseId);
    if (!row) return null;
    return {
      catalogVersion: row.catalog_revision_no,
      planId: row.plan_id ?? null,
      planKey: row.plan_key ?? null,
      rateCard: row.rate_card ?? null,
    };
  }

  async releaseLease(userId: string, leaseId: string): Promise<ReleaseResult> {
    const row = await this.leaseRepo.releaseLease(userId, leaseId);
    return {
      leaseId,
      userId,
      released: Boolean(row.released),
      reason: row.reason != null ? String(row.reason) : null,
    };
  }

  async renewLease(userId: string, leaseId: string, ttlSeconds: number): Promise<LeaseResult> {
    if (!Number.isInteger(ttlSeconds) || ttlSeconds < 1) {
      throw new RangeError("ttlSeconds must be a positive integer");
    }
    const row = await this.leaseRepo.renewLease(userId, leaseId, ttlSeconds);
    const availability = await this.getAvailable(userId);
    if (row.error !== null) {
      return {
        leaseId: null,
        userId,
        amount: row.amount == null ? null : dec(row.amount),
        available: availability.available,
        reservedTotal: availability.reserved,
        minimumBalance: row.minimum_balance == null ? null : dec(row.minimum_balance),
        billingMode:
          row.minimum_balance != null && dec(row.minimum_balance).lt(0) ? "overdraft" : "strict",
        expiresAt: null,
        error: row.error,
      };
    }
    const minimumBalance = dec(row.minimum_balance);
    return {
      leaseId: requireText(row.lease_id, "renew_lease"),
      userId,
      amount: dec(row.amount),
      available: availability.available,
      reservedTotal: availability.reserved,
      minimumBalance,
      billingMode: minimumBalance.lt(0) ? "overdraft" : "strict",
      expiresAt: requireText(row.expires_at, "renew_lease"),
      error: null,
    };
  }

  async expireLeases(limit = 100): Promise<number> {
    if (!Number.isInteger(limit) || limit < 1 || limit > 1000) {
      throw new RangeError("lease expiry limit must be an integer between 1 and 1000");
    }
    return this.leaseRepo.expireLeases(limit);
  }

  async getAvailable(userId: string): Promise<AvailableResult> {
    const row = await this.balanceRepo.getAvailable(userId);
    return {
      userId,
      balance: dec(row.balance),
      reserved: dec(row.reserved),
      available: dec(row.available),
    };
  }

  async getActiveCatalog(): Promise<CatalogRevision | null> {
    return this.loadActiveCatalog();
  }

  private async loadActiveCatalog(): Promise<CatalogRevision | null> {
    const row = await this.catalogRepo.getActiveCatalog();
    if (!row || !row.config) return null;
    return normalizeCatalogRevision(row, 0);
  }

  async publishAndActivateCatalog(
    config: Record<string, unknown>,
    label?: string | null,
    rollout?: CatalogRollout | Record<string, unknown> | null,
  ): Promise<string> {
    const canonical = canonicalBursarConfigDict(config);
    const parsed = loadConfigFromDict(canonical);
    const rolloutDocument = canonicalCatalogRolloutDict(
      validateCatalogRollout(parsed, loadCatalogRollout(rollout ?? {})),
    );
    const row = await this.catalogRepo.publishAndActivateCatalog(
      JSON.stringify(canonical),
      label ?? null,
      rolloutDocument,
    );
    return row.id;
  }

  async publishCatalogDraft(
    config: Record<string, unknown>,
    label?: string | null,
  ): Promise<string> {
    const canonical = canonicalBursarConfigDict(config);
    const row = await this.catalogRepo.publishCatalogDraft(
      JSON.stringify(canonical),
      label ?? null,
    );
    return row.id;
  }

  async getCatalogHistory(): Promise<CatalogRevisionSummary[]> {
    const rows = await this.catalogRepo.getCatalogHistory();
    if (!rows) return [];
    return (rows as Record<string, unknown>[]).map((r) => ({
      id: String(r.id ?? ""),
      version: Number(r.version ?? 0),
      label: (r.label as string) ?? null,
      active: Boolean(r.active ?? false),
      createdAt: String(r.created_at ?? ""),
    }));
  }

  async getCatalogRevision(version: number): Promise<CatalogRevision | null> {
    const row = await this.catalogRepo.getCatalogRevision(version);
    if (!row || !row.config) return null;
    return normalizeCatalogRevision(row, version);
  }

  async activateCatalogRevision(
    version: number,
    rollout?: CatalogRollout | Record<string, unknown> | null,
  ): Promise<string> {
    const target = await this.getCatalogRevision(version);
    const parsedRollout = loadCatalogRollout(rollout ?? {});
    if (target != null) {
      validateCatalogRollout(
        loadConfigFromDict(target.config as Record<string, unknown>),
        parsedRollout,
      );
    }
    const row = await this.catalogRepo.activateCatalogRevision(
      version,
      canonicalCatalogRolloutDict(parsedRollout),
    );
    return row.id;
  }

  async getUserPlan(userId: string): Promise<GetUserPlanResult> {
    const row = await this.planRepo.getUserPlan(userId);
    if (!row) {
      return {
        userId,
        planId: null,
        planKey: null,
        planLabel: null,
        allowance: null,
        entitlements: {},
        creditPolicy: null,
        admission: null,
        allowedOperations: [],
        catalogRevisionPinned: false,
      };
    }
    let allowance: GetUserPlanResult["allowance"] = null;
    if (row.credit_allowance_amount != null) {
      const priority = row.credit_allowance_priority;
      const resetUnit = row.credit_allowance_reset_unit;
      const resetCount = row.credit_allowance_reset_count;
      const resetAnchor = row.credit_allowance_reset_anchor;
      const resetTimezone = row.credit_allowance_reset_timezone;
      if (
        priority == null ||
        resetUnit == null ||
        resetCount == null ||
        resetAnchor == null ||
        resetTimezone == null
      ) {
        throw new StoreError("get_user_plan returned an incomplete allowance policy");
      }
      allowance = {
        amount: dec(row.credit_allowance_amount),
        priority: Number(priority),
        resetUnit: String(resetUnit),
        resetCount: Number(resetCount),
        resetAnchor: String(resetAnchor),
        resetTimezone: String(resetTimezone),
      };
    }
    const admissionOperations = parseAdmissionOperations(row.operation_admission);
    return {
      userId: String(row.user_id ?? userId),
      planId: (row.plan_id as string) ?? null,
      planKey: (row.plan_key as string) ?? null,
      planLabel: (row.plan_label as string) ?? null,
      allowance,
      entitlements: parseEntitlements(row.entitlements),
      rateCard: row.rate_card != null ? String(row.rate_card) : null,
      creditPolicy:
        row.credit_policy_type == null
          ? null
          : {
              type: String(row.credit_policy_type) as "prepaid" | "credit_line",
              creditLimit: row.credit_limit == null ? null : dec(row.credit_limit),
            },
      admission:
        row.admission_max_in_flight == null && Object.keys(admissionOperations).length === 0
          ? null
          : {
              maxInFlight:
                row.admission_max_in_flight == null ? null : Number(row.admission_max_in_flight),
              operations: admissionOperations,
            },
      allowedOperations: Array.isArray(row.allowed_operations)
        ? row.allowed_operations.map(String)
        : [],
      planAssignedAt: row.plan_assigned_at != null ? new Date(String(row.plan_assigned_at)) : null,
      assignmentSourceType:
        row.assignment_source_type == null ? null : String(row.assignment_source_type),
      assignmentSourceId:
        row.assignment_source_id == null ? null : String(row.assignment_source_id),
      catalogRevisionPinned: row.catalog_revision_pinned === true,
      catalogVersion: row.catalog_revision_no != null ? Number(row.catalog_revision_no) : null,
    };
  }

  async checkFeature(userId: string, feature: string): Promise<CheckFeatureResult> {
    const entitlement = await this.planRepo.getEntitlement(userId, feature);
    const value = entitlement?.feature_value ?? null;
    return {
      userId,
      feature,
      value,
      hasFeature: entitlement != null && value !== null && value !== undefined && value !== false,
    };
  }

  async setUserPlan(
    userId: string,
    planKey: string,
    planAssignedAt?: Date | null,
  ): Promise<SetUserPlanResult> {
    const row = await this.planRepo.setUserPlan(
      userId,
      planKey,
      planAssignedAt?.toISOString() ?? null,
    );
    return {
      userId: String(row.user_id ?? userId),
      planId: requireText(row.plan_id, "assign_plan"),
      planAssignedAt: row.plan_assigned_at != null ? String(row.plan_assigned_at) : null,
    };
  }

  async unsetUserPlan(userId: string): Promise<{ userId: string }> {
    const row = await this.planRepo.unsetUserPlan(userId);
    return { userId: String(row.user_id ?? userId) };
  }

  async setPlanRevisionPin(userId: string, pinned: boolean): Promise<boolean> {
    return this.planRepo.setPlanRevisionPin(userId, pinned);
  }

  async applyDuePlanChanges(limit = 100): Promise<number> {
    if (!Number.isInteger(limit) || limit < 1 || limit > 1000) {
      throw new RangeError("plan change limit must be an integer between 1 and 1000");
    }
    return this.planRepo.applyDuePlanChanges(limit);
  }

  async startPlanMigration(
    fromPlanId: string | null,
    toPlanId: string,
  ): Promise<PlanMigrationStartResult> {
    const migrationId = await this.planRepo.startPlanMigration(fromPlanId, toPlanId);
    if (!migrationId) throw new StoreError("start_plan_migration returned no migration id");
    return { migrationId };
  }

  async migratePlanBatch(
    migrationId: string,
    batchSize?: number,
  ): Promise<PlanMigrationBatchResult> {
    const row = await this.planRepo.migratePlanBatch(migrationId, batchSize);
    return {
      migrated: Number(row.migrated ?? 0),
      done: Boolean(row.done),
      nextCursor: row.next_cursor != null ? String(row.next_cursor) : null,
    };
  }

  async getQuotaState(userId: string, quotaKey?: string | null): Promise<QuotaState[]> {
    const rows = await this.planRepo.getQuotaState(userId, quotaKey);
    return rows.map((row) => ({
      userId: row.user_id,
      quotaKey: row.quota_key,
      operation: row.operation_key,
      measure: row.measure_key,
      limit: dec(row.quota_limit),
      consumed: dec(row.consumed),
      reserved: dec(row.reserved),
      remaining: dec(row.remaining),
      overage: dec(row.overage),
      enforcement: row.enforcement,
      windowStart: row.window_start,
      windowEnd: row.window_end,
      emitAtPercent: row.emit_at_percent,
    }));
  }

  async listQuotaEvents(userId: string, options?: ListQuotaEventsOptions): Promise<QuotaEvent[]> {
    const limit = options?.limit ?? 100;
    if (!Number.isInteger(limit) || limit < 1 || limit > 500) {
      throw new RangeError("quota event limit must be an integer between 1 and 500");
    }
    if (options?.afterId != null && options.after == null) {
      throw new TypeError("afterId requires after");
    }
    const rows = await this.planRepo.listQuotaEvents(
      userId,
      options?.after?.toISOString() ?? null,
      limit,
      options?.idempotencyKey ?? null,
      options?.afterId ?? null,
    );
    return rows.map((row) => ({
      eventId: row.event_id,
      quotaKey: row.quota_key,
      operation: row.operation_key,
      measure: row.measure_key,
      eventType: row.event_type,
      thresholdPercent: row.threshold_percent,
      idempotencyKey: row.idempotency_key,
      usageChargeId: row.usage_charge_id,
      createdAt: row.created_at,
    }));
  }

  async checkAllowance(userId: string): Promise<AllowanceResult> {
    const row = await this.planRepo.checkAllowance(userId);
    if (!row) {
      return { planId: "", allowanceRemaining: ZERO, periodStart: "", periodEnd: "" };
    }
    return {
      planId: String(row.plan_id ?? ""),
      allowanceRemaining: dec(row.allowance_remaining),
      periodStart: String(row.period_start ?? ""),
      periodEnd: String(row.period_end ?? ""),
    };
  }

  async revokeCreditsByEntryType(
    userId: string,
    entryType: string,
  ): Promise<Record<string, unknown>> {
    return this.deductionRepo.revokeCreditsByEntryType(userId, entryType);
  }

  async refundCredits(
    entryId: string,
    amount?: Decimal,
    reason?: string,
    metadata?: CreditMetadata | null,
    idempotencyKey?: string | null,
  ): Promise<RefundResult> {
    const row = await this.deductionRepo.refundCredits(
      entryId,
      amount != null ? decParam(amount) : null,
      idempotencyKey ?? `refund:${entryId}:${amount != null ? decParam(amount) : "remaining"}`,
      reason ?? null,
      JSON.stringify(metadata ?? {}),
    );
    if ("error" in row && row.error) {
      return {
        refundEntryId: null,
        originalEntryId: entryId,
        userId: row.user_id ?? null,
        amount: row.amount == null ? null : dec(row.amount),
        newBalance: row.new_balance == null ? null : dec(row.new_balance),
        error: String(row.error),
        bucketBreakdown: null,
      };
    }
    return {
      refundEntryId: requireText(row.refund_entry_id, "refund_credit_by_entry"),
      originalEntryId: entryId,
      userId: String(row.user_id ?? ""),
      amount: dec(row.amount),
      newBalance: dec(row.new_balance),
      error: null,
      bucketBreakdown: decRecord(row.bucket_breakdown),
    };
  }

  async aggregateStats(start: Date, end: Date): Promise<AggregateStats> {
    if (!(start instanceof Date) || !(end instanceof Date) || end <= start) {
      throw new RangeError("aggregateStats requires end after start");
    }
    const row = await this.analyticsRepo.aggregateStats(start.toISOString(), end.toISOString());
    return {
      totalCreditsConsumed: dec(row.total_credits_consumed),
      activeUsers: Number(row.active_users ?? 0),
      avgDailySpend: dec(row.avg_daily_spend),
      topModel: String(row.top_model ?? ""),
      topUser: String(row.top_user ?? ""),
    };
  }

  async spendByUser(start: Date, end: Date): Promise<SpendByUserRow[]> {
    const rows = await this.analyticsRepo.spendByUser(start.toISOString(), end.toISOString());
    return (rows ?? []).map((r) => ({
      userId: String(r.user_id ?? ""),
      totalSpend: dec(r.total_spend),
      entryCount: Number(r.entry_count ?? 0),
    }));
  }

  async spendByModel(start: Date, end: Date): Promise<SpendByModelRow[]> {
    const rows = await this.analyticsRepo.spendByModel(start.toISOString(), end.toISOString());
    return (rows ?? []).map((r) => ({
      model: String(r.model ?? ""),
      totalSpend: dec(r.total_spend),
      entryCount: Number(r.entry_count ?? 0),
    }));
  }

  async topUsers(limit: number, start: Date, end: Date): Promise<TopUserRow[]> {
    const rows = await this.analyticsRepo.topUsers(limit, start.toISOString(), end.toISOString());
    return (rows ?? []).map((r) => ({
      userId: String(r.user_id ?? ""),
      totalSpend: dec(r.total_spend),
    }));
  }

  async dailySpend(start: Date, end: Date): Promise<DailySpendRow[]> {
    const rows = await this.analyticsRepo.dailySpend(start.toISOString(), end.toISOString());
    return (rows ?? []).map((r) => ({
      date: String(r.date ?? ""),
      totalSpend: dec(r.total_spend),
      entryCount: Number(r.entry_count ?? 0),
    }));
  }

  async getLedgerEntry(userId: string, entryId: string): Promise<LedgerEntry | null> {
    const row = await this.analyticsRepo.getLedgerEntry(userId, entryId);
    if (!row) return null;
    return mapLedgerEntry(row);
  }

  private async listLedgerPage(
    userId: string,
    options: ListLedgerEntriesOptions | ListUsageEntriesOptions | undefined,
    usageOnly: boolean,
  ): Promise<LedgerPage> {
    const limit = options?.limit ?? DEFAULT_PAGE_SIZE;
    if (!Number.isInteger(limit) || limit < 1 || limit > MAX_PAGE_SIZE) {
      throw new RangeError(`limit must be an integer between 1 and ${MAX_PAGE_SIZE}`);
    }
    const cursor = options?.cursor ?? null;
    const entryTypes = usageOnly
      ? ["usage"]
      : ((options as ListLedgerEntriesOptions | undefined)?.entryTypes ?? null);
    const rows = await this.analyticsRepo.listLedgerEntries(
      userId,
      entryTypes,
      options?.fromDate?.toISOString() ?? null,
      options?.toDate?.toISOString() ?? null,
      limit + 1,
      cursor?.createdAt ?? null,
      cursor?.entryId ?? null,
      usageOnly,
    );
    const hasMore = rows.length > limit;
    const items = rows.slice(0, limit).map(mapLedgerEntry);
    const last = items.at(-1);
    return {
      items,
      nextCursor: hasMore && last ? { createdAt: last.createdAt, entryId: last.entryId } : null,
    };
  }

  async listLedgerEntries(userId: string, options?: ListLedgerEntriesOptions): Promise<LedgerPage> {
    return this.listLedgerPage(userId, options, false);
  }

  async listUsageEntries(userId: string, options?: ListUsageEntriesOptions): Promise<LedgerPage> {
    return this.listLedgerPage(userId, options, true);
  }

  async listUsageCharges(
    userId: string,
    options?: ListUsageChargesOptions,
  ): Promise<UsageChargePage> {
    const limit = options?.limit ?? DEFAULT_PAGE_SIZE;
    if (!Number.isInteger(limit) || limit < 1 || limit > MAX_PAGE_SIZE) {
      throw new RangeError(`limit must be an integer between 1 and ${MAX_PAGE_SIZE}`);
    }
    const cursor = options?.cursor ?? null;
    const rows = await this.analyticsRepo.listUsageCharges(
      userId,
      options?.fromDate?.toISOString() ?? null,
      options?.toDate?.toISOString() ?? null,
      limit + 1,
      cursor?.eventAt ?? null,
      cursor?.usageId ?? null,
      options?.includeRecordOnly ?? true,
    );
    const hasMore = rows.length > limit;
    const items = rows.slice(0, limit).map(mapUsageCharge);
    const last = items.at(-1);
    return {
      items,
      nextCursor: hasMore && last ? { eventAt: last.eventAt, usageId: last.usageId } : null,
    };
  }

  async createTeam(
    ownerSubjectId: string,
    name: string,
    initialBalance: Decimal = ZERO,
  ): Promise<CreateTeamResult> {
    const row = await this.teamRepo.createTeam(ownerSubjectId, name, decParam(initialBalance));
    if (row.error_code) throw new StoreError(String(row.error_code));
    return {
      teamId: requireText(row.team_id, "create_team"),
      name: String(row.name ?? name),
    };
  }

  async getTeamBalance(teamId: string): Promise<TeamBalanceResult> {
    const row = await this.teamRepo.getTeamBalance(teamId);
    if (!row) {
      return { teamId, name: "", balance: ZERO, memberCount: 0 };
    }
    if ("error" in row && row.error) {
      return { teamId, name: "", balance: ZERO, memberCount: 0 };
    }
    return {
      teamId: String(row.team_id ?? teamId),
      name: String(row.name ?? ""),
      balance: dec(row.balance),
      memberCount: Number(row.member_count ?? 0),
    };
  }

  async addTeamMember(
    teamId: string,
    userId: string,
    role = "member",
    spendCap?: Decimal | null,
  ): Promise<AddTeamMemberResult> {
    const row = await this.teamRepo.addTeamMember(
      teamId,
      userId,
      role,
      spendCap != null ? decParam(spendCap) : null,
    );
    return {
      teamId: String(row.team_id ?? teamId),
      userId: String(row.user_id ?? userId),
      role: String(row.role ?? role),
    };
  }

  async getTeamMembers(teamId: string): Promise<TeamMember[]> {
    const rows = await this.teamRepo.getTeamMembers(teamId);
    return (rows ?? []).map((r) => ({
      userId: String(r.user_id ?? ""),
      role: String(r.role ?? "member"),
      spendCap: r.spend_cap != null ? dec(r.spend_cap) : null,
      totalSpent: dec(r.total_spent),
    }));
  }

  async removeTeamMember(teamId: string, userId: string): Promise<boolean> {
    return this.teamRepo.removeTeamMember(teamId, userId);
  }

  async deductTeam(
    teamId: string,
    userId: string,
    amount: Decimal,
    metadata?: CreditMetadata | null,
    idempotencyKey?: string | null,
  ): Promise<TeamDeductionResult> {
    const meta: Record<string, unknown> = { ...(metadata ?? {}) };
    const effectiveIdempotencyKey = idempotencyKey ?? `team-usage:${randomUUID()}`;
    meta.idempotency_key = effectiveIdempotencyKey;
    const operation =
      typeof meta.operation === "string" && meta.operation.length > 0
        ? meta.operation
        : "team_usage";
    const row = await this.teamRepo.deductTeam(
      teamId,
      userId,
      decParam(amount),
      effectiveIdempotencyKey,
      operation,
      JSON.stringify(meta),
    );
    if ("error" in row && row.error) {
      return {
        entryId: null,
        teamId,
        userId,
        amount: dec(row.amount),
        teamBalanceAfter: row.team_balance_after == null ? null : dec(row.team_balance_after),
        idempotent: false,
        error: String(row.error),
      };
    }
    return {
      entryId: requireText(row.entry_id, "deduct_team"),
      teamId: String(row.team_id ?? teamId),
      userId: String(row.user_id ?? userId),
      amount: dec(row.amount),
      teamBalanceAfter: dec(row.team_balance_after),
      idempotent: Boolean(row.replayed),
      error: null,
    };
  }

  async sweepExpiredCredits(dryRun = false, userId?: string, limit = 100): Promise<SweepResult> {
    const row = await this.bucketRepo.sweepExpiredCredits(dryRun, userId, limit);
    return {
      expiredCount: Number(row.expired_count ?? 0),
      expiredAmount: dec(row.expired_amount),
      dryRun,
      expiredByBucket: decRecord(row.expired_by_bucket),
    };
  }

  async getBucketBalances(userId: string): Promise<BucketBalancesResult> {
    const envelope = await this.bucketRepo.getBucketBalances(userId);
    const bucketRows = (envelope.buckets as Record<string, unknown>[] | undefined) ?? [];
    const buckets: BucketBalance[] = bucketRows.map((row) => ({
      bucketKey: String(row.bucket_key ?? ""),
      label: String(row.label ?? ""),
      priority: Number(row.priority ?? 0),
      expires: Boolean(row.expires ?? false),
      balance: dec(row.balance),
    }));
    return { userId, buckets, totalBalance: dec(envelope.total_balance) };
  }

  async executeGrantProgram(
    request: ExecuteGrantProgramRequest,
  ): Promise<GrantProgramAwardResult[]> {
    const rows = await this.balanceRepo.executeGrantProgram({
      trigger: request.trigger,
      programKey: request.programKey,
      subjectId: request.subjectId,
      eventKey: request.eventKey,
      referrerSubjectId: request.referrerSubjectId ?? null,
      region: request.region ?? null,
      metadata: JSON.stringify(request.metadata ?? {}),
    });
    return rows.map((row) => ({
      grantEventId: row.grant_event_id ?? null,
      grantAwardId: row.grant_award_id ?? null,
      recipientSubjectId: row.recipient_subject_id ?? null,
      ledgerEntryId: row.ledger_entry_id ?? null,
      amount: dec(row.amount),
      replayed: Boolean(row.replayed),
      error: row.error_code ?? null,
    }));
  }
}
