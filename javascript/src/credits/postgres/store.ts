import { Decimal } from "decimal.js";
import { z } from "zod";

import { StoreClosedError, StoreError } from "../../errors.js";
import {
  canonicalBursarConfigDict,
  canonicalCatalogRolloutDict,
  loadCatalogRollout,
  loadConfigFromDict,
  validateCatalogRollout,
  type BursarConfigData,
  type CatalogRollout,
} from "../../config.js";
import {
  PostgresClient,
  type PostgresConnectionOptions,
  type PostgresPool,
  type PostgresPoolConstructor,
} from "../../shared/postgres-client.js";
import type { JsonObject, PostgresParams, PostgresValue } from "../../shared/json.js";
import type {
  AddCreditsResult,
  AddTeamMemberResult,
  AggregateStats,
  AllowanceResult,
  AvailableResult,
  BalanceResult,
  CheckFeatureResult,
  CreditMetadata,
  CreateTeamResult,
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
  RevokeCreditsResult,
  ReleaseResult,
  SetUserPlanResult,
  SpendByModelRow,
  SpendByUserRow,
  SweepResult,
  TeamBalanceResult,
  TeamDeductionResult,
  TeamMember,
  TeamRole,
  BucketBalance,
  BucketBalancesResult,
  TopUserRow,
  UnsetUserPlanResult,
  LedgerEntry,
} from "../types/index.js";
import { requireStableKey } from "../../shared/idempotency.js";
import type {
  AddCreditsOptions,
  DeductTeamOptions,
  RefundCreditsOptions,
} from "../service-types.js";
import { CreditStore } from "../store.js";
import type { CreateLeaseOptions, CreateTeamOptions, SettleLeaseOptions } from "../store.js";
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
} from "./value-mappers.js";

const DEFAULT_LEASE_TTL_SECONDS = 600;
const DEFAULT_PAGE_SIZE = 50;
const MAX_PAGE_SIZE = 200;

function requireText(value: PostgresValue | undefined, context: string): string {
  const parsed = z.string().min(1).safeParse(value);
  if (!parsed.success) {
    throw new StoreError(`${context} returned a missing or invalid identifier`);
  }
  return parsed.data;
}

export type PgPool = PostgresPool;
export type PgPoolConstructor = PostgresPoolConstructor;

/** Construction options for the PostgreSQL credit store. */
export interface PostgresStoreOptions extends PostgresConnectionOptions {
  /** PostgreSQL connection string or an application-owned pool. */
  postgres: string | PgPool;
  /** Tenant UUID bound to every store transaction. */
  tenantId: string;
  /** Explicit financial namespace for catalog provider references. */
  providerEnvironment: NonNullable<PostgresConnectionOptions["providerEnvironment"]>;
  /** Injectable `pg.Pool` constructor for custom runtimes and tests. */
  poolConstructor?: PgPoolConstructor;
  usageBackend?: "postgres" | "clickhouse";
}

export class PostgresStore extends CreditStore {
  readonly providerEnvironment: NonNullable<PostgresConnectionOptions["providerEnvironment"]>;
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
    if (!z.object({}).safeParse(options).success) {
      throw new TypeError("PostgresStore options are required");
    }
    if (options.poolConstructor !== undefined && !z.string().safeParse(options.postgres).success) {
      throw new TypeError("poolConstructor cannot be used with an existing PostgreSQL pool");
    }
    this.providerEnvironment = options.providerEnvironment;
    this.postgres = new PostgresClient(options.postgres, {
      tenantId: options.tenantId,
      providerEnvironment: options.providerEnvironment,
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

  private async query(text: string, params?: PostgresParams) {
    return this.postgres.query(text, params);
  }

  async close(): Promise<void> {
    await this.postgres.close();
  }

  private static readonly RPC_NAME_RE = /^[a-z_][a-z0-9_]*$/;
  private static readonly SCALAR_RPC_NAMES = new Set([
    "apply_due_plan_assignment_changes",
    "release_lease",
    "remove_team_member",
    "set_plan_revision_pin",
    "set_team_member",
    "start_plan_migration",
    "unassign_plan",
  ]);

  private async callproc(name: string, params: PostgresParams): Promise<PostgresValue[]> {
    if (!PostgresStore.RPC_NAME_RE.test(name)) {
      throw new StoreError(`Invalid RPC name: ${name}`);
    }
    const placeholders = params.map((_, i) => `$${i + 1}`).join(", ");
    const rows = await this.query(`SELECT * FROM bursar.${name}(${placeholders})`, params);
    if (PostgresStore.SCALAR_RPC_NAMES.has(name) && rows.length === 1) {
      const row = rows[0];
      if (row === undefined) return rows;
      const keys = Object.keys(row);
      const key = keys[0];
      if (keys.length === 1 && key !== undefined) {
        const value = row[key];
        if (value !== undefined) return [value];
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
      userId: row.user_id,
      balance: dec(row.balance),
      lifetimePurchased: dec(row.lifetime_purchased),
    };
  }

  async addCredits(
    userId: string,
    amount: Decimal,
    options: AddCreditsOptions,
  ): Promise<AddCreditsResult> {
    const stableKey = requireStableKey(options?.idempotencyKey);
    const meta: CreditMetadata = { ...(options?.metadata ?? {}) };
    if (options?.expiresAt) {
      meta.expires_at = options.expiresAt.toISOString();
    }
    const row = await this.balanceRepo.addCredits(
      userId,
      decParam(amount),
      options?.type ?? "adjustment",
      JSON.stringify(meta),
      options?.expiresAt?.toISOString() ?? null,
      options?.bucket ?? null,
      stableKey,
    );
    if (row.error !== null) {
      throw new StoreError(`post_credit: ${row.error}`);
    }
    return {
      entryId: requireText(row.entry_id, "post_credit"),
      userId: row.user_id,
      amount: dec(row.amount),
      newBalance: dec(row.new_balance),
      lifetimePurchased: dec(row.lifetime_purchased),
      bucket: row.bucket,
      idempotent: row.idempotent,
    };
  }

  async deductWithAllowance(
    userId: string,
    amount: Decimal,
    options: DeductWithAllowanceOptions,
  ): Promise<DeductionResult> {
    const idempotencyKey = requireStableKey(options?.idempotencyKey);
    const operation =
      options?.operation ??
      z.string().min(1).safeParse(options?.metadata?.operation).data ??
      "usage";
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

    if (row.error !== null) {
      return {
        entryId: null,
        usageChargeId: row.charge_id != null ? String(row.charge_id) : null,
        userId,
        amount: dec(row.amount),
        allowanceConsumed: dec(row.allowance_consumed),
        balanceAfter: row.balance_after == null ? null : dec(row.balance_after),
        idempotent: false,
        error: row.error,
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
      idempotent: row.idempotent,
      error: null,
      bucketBreakdown: decRecord(row.bucket_breakdown),
    };
  }

  async recordUsage(
    userId: string,
    operation: string,
    amount: Decimal,
    options: DeductWithAllowanceOptions,
  ): Promise<UsageRecordResult> {
    const idempotencyKey = requireStableKey(options?.idempotencyKey);
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
      idempotent: row.replayed,
      error: null,
    };
  }

  async createLease(
    userId: string,
    amount: Decimal,
    operationType: string,
    options: CreateLeaseOptions,
  ): Promise<LeaseResult> {
    const idempotencyKey = requireStableKey(options?.idempotencyKey);
    const row = await this.leaseRepo.createLease({
      userId,
      amount: decParam(amount),
      operationType,
      idempotencyKey,
      ttlSeconds: options?.ttlSeconds ?? DEFAULT_LEASE_TTL_SECONDS,
      metadata: JSON.stringify(options?.metadata ?? {}),
      feature: options?.feature ?? null,
      measures: JSON.stringify(options?.measures ?? {}),
      dimensions: JSON.stringify(options?.dimensions ?? {}),
      minimumBalance: options?.floor == null ? null : decParam(options.floor),
      maxConcurrent: options?.maxConcurrent ?? null,
    });

    const availability = await this.getAvailable(userId);
    if (row.error !== null) {
      return {
        leaseId: null,
        userId,
        amount: row.amount == null ? null : dec(row.amount),
        available: availability.available,
        reservedTotal: availability.reserved,
        minimumBalance: row.minimum_balance == null ? null : dec(row.minimum_balance),
        billingMode: options?.billingMode ?? "strict",
        expiresAt: null,
        error: row.error,
      };
    }
    return {
      leaseId: requireText(row.lease_id, "create_lease_for_operation"),
      userId: row.user_id,
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
    const idempotencyKey =
      options?.idempotencyKey === undefined
        ? `lease:${leaseId}:settle`
        : requireStableKey(options.idempotencyKey);
    const row = await this.leaseRepo.settleLease({
      userId,
      leaseId,
      amount: decParam(amount),
      idempotencyKey,
      feature: options?.feature ?? null,
      model: options?.model ?? null,
      region: options?.region ?? null,
      measures: JSON.stringify(options?.measures ?? {}),
      dimensions: JSON.stringify(options?.dimensions ?? {}),
      metadata: JSON.stringify(options?.metadata ?? {}),
    });

    if (row.error !== null) {
      return {
        entryId: null,
        usageChargeId: row.charge_id != null ? String(row.charge_id) : null,
        userId,
        amount: dec(row.amount),
        allowanceConsumed: dec(row.allowance_consumed),
        balanceAfter: row.balance_after == null ? null : dec(row.balance_after),
        idempotent: false,
        error: row.error,
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
      idempotent: row.idempotent,
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
      released: row.released,
      reason: row.reason,
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
    if (row === null) {
      return { userId, balance: ZERO, reserved: ZERO, available: ZERO };
    }
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
    return row === null ? null : normalizeCatalogRevision(row);
  }

  async publishAndActivateCatalog(
    config: BursarConfigData | JsonObject,
    label?: string | null,
    rollout?: CatalogRollout | null,
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
    config: BursarConfigData | JsonObject,
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
    return rows.map((r) => ({
      id: r.id,
      version: r.version,
      label: r.label,
      active: r.active,
      createdAt: r.created_at,
    }));
  }

  async getCatalogRevision(version: number): Promise<CatalogRevision | null> {
    const row = await this.catalogRepo.getCatalogRevision(version);
    return row === null ? null : normalizeCatalogRevision(row);
  }

  async activateCatalogRevision(version: number, rollout?: CatalogRollout | null): Promise<string> {
    const target = await this.getCatalogRevision(version);
    const parsedRollout = loadCatalogRollout(rollout ?? {});
    if (target != null) {
      validateCatalogRollout(loadConfigFromDict(target.config), parsedRollout);
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
        rateCard: null,
        creditPolicy: null,
        admission: null,
        allowedOperations: [],
        planAssignedAt: null,
        planAssignmentEndsAt: null,
        assignmentSourceType: null,
        assignmentSourceId: null,
        catalogRevisionPinned: false,
        catalogVersion: null,
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
        priority,
        resetUnit,
        resetCount,
        resetAnchor,
        resetTimezone,
      };
    }
    const admissionOperations = Object.fromEntries(
      Object.entries(row.operation_admission).map(([operation, policy]) => [
        operation,
        { maxInFlight: policy.max_in_flight },
      ]),
    );
    return {
      userId: row.user_id,
      planId: row.plan_id,
      planKey: row.plan_key,
      planLabel: row.plan_label,
      allowance,
      entitlements: row.entitlements,
      rateCard: row.rate_card,
      creditPolicy:
        row.credit_policy_type == null
          ? null
          : {
              type: row.credit_policy_type,
              creditLimit: row.credit_limit == null ? null : dec(row.credit_limit),
            },
      admission:
        row.admission_max_in_flight == null && Object.keys(admissionOperations).length === 0
          ? null
          : {
              maxInFlight: row.admission_max_in_flight,
              operations: admissionOperations,
            },
      allowedOperations: row.allowed_operations,
      planAssignedAt: row.plan_assigned_at,
      planAssignmentEndsAt: row.plan_assignment_ends_at,
      assignmentSourceType: row.assignment_source_type,
      assignmentSourceId: row.assignment_source_id,
      catalogRevisionPinned: row.catalog_revision_pinned,
      catalogVersion: row.catalog_revision_no,
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
      userId: row.user_id,
      planId: requireText(row.plan_id, "set_subject_plan"),
      planKey: row.plan_key,
      planAssignedAt: row.plan_assigned_at,
      assignmentState: row.assignment_state,
    };
  }

  async unsetUserPlan(userId: string): Promise<UnsetUserPlanResult> {
    const row = await this.planRepo.unsetUserPlan(userId);
    return { userId: row.user_id };
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
    return { migrationId };
  }

  async migratePlanBatch(
    migrationId: string,
    batchSize?: number,
  ): Promise<PlanMigrationBatchResult> {
    const row = await this.planRepo.migratePlanBatch(migrationId, batchSize);
    return {
      migrated: row.migrated,
      done: row.done,
      nextCursor: row.next_cursor,
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

  async checkAllowance(userId: string): Promise<AllowanceResult | null> {
    const row = await this.planRepo.checkAllowance(userId);
    if (!row) return null;
    return {
      planId: row.plan_id,
      allowanceRemaining: dec(row.allowance_remaining),
      periodStart: row.period_start,
      periodEnd: row.period_end,
    };
  }

  async revokeCreditsByEntryType(userId: string, entryType: string): Promise<RevokeCreditsResult> {
    const row = await this.deductionRepo.revokeCreditsByEntryType(userId, entryType);
    if (row.error_code !== null) {
      throw new StoreError(`revoke_subject_credits_by_operation failed: ${row.error_code}`);
    }
    return {
      userId: row.user_id,
      entryType: row.entry_type,
      revoked: dec(row.revoked),
      balanceAfter: dec(row.balance_after),
    };
  }

  async refundCredits(entryId: string, options: RefundCreditsOptions): Promise<RefundResult> {
    const stableKey = requireStableKey(options?.idempotencyKey);
    const row = await this.deductionRepo.refundCredits(
      entryId,
      options?.amount != null ? decParam(new Decimal(options.amount)) : null,
      stableKey,
      options?.reason ?? null,
      JSON.stringify(options?.metadata ?? {}),
    );
    if (row.error !== null) {
      return {
        refundEntryId: null,
        originalEntryId: entryId,
        userId: row.user_id ?? null,
        amount: row.amount == null ? null : dec(row.amount),
        newBalance: row.new_balance == null ? null : dec(row.new_balance),
        error: row.error,
        bucketBreakdown: null,
      };
    }
    return {
      refundEntryId: requireText(row.refund_entry_id, "refund_credit_by_entry"),
      originalEntryId: entryId,
      userId: requireText(row.user_id, "refund_credit_by_entry"),
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
      activeUsers: row.active_users,
      avgDailySpend: dec(row.avg_daily_spend),
      topModel: row.top_model,
      topUser: row.top_user,
    };
  }

  async spendByUser(start: Date, end: Date): Promise<SpendByUserRow[]> {
    const rows = await this.analyticsRepo.spendByUser(start.toISOString(), end.toISOString());
    return (rows ?? []).map((r) => ({
      userId: r.user_id,
      totalSpend: dec(r.total_spend),
      entryCount: r.entry_count,
    }));
  }

  async spendByModel(start: Date, end: Date): Promise<SpendByModelRow[]> {
    const rows = await this.analyticsRepo.spendByModel(start.toISOString(), end.toISOString());
    return (rows ?? []).map((r) => ({
      model: r.model,
      totalSpend: dec(r.total_spend),
      entryCount: r.entry_count,
    }));
  }

  async topUsers(limit: number, start: Date, end: Date): Promise<TopUserRow[]> {
    const rows = await this.analyticsRepo.topUsers(limit, start.toISOString(), end.toISOString());
    return (rows ?? []).map((r) => ({
      userId: r.user_id,
      totalSpend: dec(r.total_spend),
    }));
  }

  async dailySpend(start: Date, end: Date): Promise<DailySpendRow[]> {
    const rows = await this.analyticsRepo.dailySpend(start.toISOString(), end.toISOString());
    return (rows ?? []).map((r) => ({
      date: r.date,
      totalSpend: dec(r.total_spend),
      entryCount: r.entry_count,
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
      : options && "entryTypes" in options
        ? (options.entryTypes ?? null)
        : null;
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
    options: CreateTeamOptions,
  ): Promise<CreateTeamResult> {
    const idempotencyKey = requireStableKey(options?.idempotencyKey);
    const initialBalance = options?.initialBalance ?? ZERO;
    const row = await this.teamRepo.createTeam(
      ownerSubjectId,
      name,
      idempotencyKey,
      decParam(initialBalance),
    );
    if (row.error_code !== null) throw new StoreError(row.error_code);
    return {
      teamId: requireText(row.team_id, "create_team"),
      name: requireText(row.name, "create_team"),
      idempotent: row.idempotent,
    };
  }

  async getTeamBalance(teamId: string): Promise<TeamBalanceResult | null> {
    const row = await this.teamRepo.getTeamBalance(teamId);
    if (!row) return null;
    return {
      teamId: row.team_id,
      name: row.name,
      balance: dec(row.balance),
      memberCount: row.member_count,
    };
  }

  async addTeamMember(
    teamId: string,
    userId: string,
    role: TeamRole = "member",
    spendCap?: Decimal | null,
  ): Promise<AddTeamMemberResult> {
    const row = await this.teamRepo.addTeamMember(
      teamId,
      userId,
      role,
      spendCap != null ? decParam(spendCap) : null,
    );
    return {
      teamId: row.team_id,
      userId: row.user_id,
      role: row.role,
    };
  }

  async getTeamMembers(teamId: string): Promise<TeamMember[]> {
    const rows = await this.teamRepo.getTeamMembers(teamId);
    return rows.map((r) => ({
      userId: r.user_id,
      role: r.role,
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
    options: DeductTeamOptions,
  ): Promise<TeamDeductionResult> {
    const meta: CreditMetadata = { ...(options?.metadata ?? {}) };
    const effectiveIdempotencyKey = requireStableKey(options?.idempotencyKey);
    meta.idempotency_key = effectiveIdempotencyKey;
    const operation = z.string().min(1).safeParse(meta.operation).data ?? "team_usage";
    const row = await this.teamRepo.deductTeam(
      teamId,
      userId,
      decParam(amount),
      effectiveIdempotencyKey,
      operation,
      JSON.stringify(meta),
    );
    if (row.error !== null) {
      return {
        entryId: null,
        teamId,
        userId,
        amount: dec(row.amount),
        teamBalanceAfter: row.team_balance_after == null ? null : dec(row.team_balance_after),
        idempotent: false,
        error: row.error,
      };
    }
    return {
      entryId: requireText(row.entry_id, "deduct_team"),
      teamId: row.team_id,
      userId: row.user_id,
      amount: dec(row.amount),
      teamBalanceAfter: dec(row.team_balance_after),
      idempotent: row.replayed,
      error: null,
    };
  }

  async sweepExpiredCredits(dryRun = false, userId?: string, limit = 100): Promise<SweepResult> {
    const row = await this.bucketRepo.sweepExpiredCredits(dryRun, userId, limit);
    return {
      expiredCount: row.expired_count,
      expiredAmount: dec(row.expired_amount),
      dryRun,
      expiredByBucket: Object.fromEntries(
        Object.entries(row.expired_by_bucket).map(([key, value]) => [key, dec(value)]),
      ),
    };
  }

  async getBucketBalances(userId: string): Promise<BucketBalancesResult> {
    const envelope = await this.bucketRepo.getBucketBalances(userId);
    const buckets: BucketBalance[] = envelope.buckets.map((row) => ({
      bucketKey: row.bucket_key,
      label: row.label,
      priority: row.priority,
      expires: row.expires,
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
    return rows.map((row) => {
      if (row.error_code !== null) {
        return {
          grantEventId: null,
          grantAwardId: null,
          recipientSubjectId: null,
          ledgerEntryId: null,
          amount: null,
          replayed: false,
          error: row.error_code,
        };
      }
      return {
        grantEventId: requireText(row.grant_event_id, "execute_grant_program"),
        grantAwardId: requireText(row.grant_award_id, "execute_grant_program"),
        recipientSubjectId: requireText(row.recipient_subject_id, "execute_grant_program"),
        ledgerEntryId: requireText(row.ledger_entry_id, "execute_grant_program"),
        amount: dec(row.amount),
        replayed: row.replayed,
        error: null,
      };
    });
  }
}
