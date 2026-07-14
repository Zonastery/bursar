import Decimal from "decimal.js";
import { StoreError } from "../errors.js";
import { resolveCalendarWindow } from "../allowance.js";
import { camelToSnakeKeys, snakeToCamelKeys } from "../case-utils.js";
import type { AllowancePeriod, FeatureLimitPeriod } from "../allowance.js";
import type {
  AddCreditsResult,
  AddTeamMemberResult,
  AggregateStats,
  AllowanceResult,
  AvailableResult,
  BalanceResult,
  BillingMode,
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
  MigratePlanUsersResult,
  OperationPolicy,
  PaginatedTransactions,
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
  BucketBalance,
  BucketBalancesResult,
  TopUserRow,
} from "../types.js";
import { CreditStore } from "./credit-store.js";
import type { CreateLeaseOptions, SettleLeaseOptions } from "./credit-store.js";
import { BalanceRepository } from "../repositories/balance.js";
import { DeductionRepository } from "../repositories/deduction.js";
import { LeaseRepository } from "../repositories/lease.js";
import { PricingRepository } from "../repositories/pricing.js";
import { PlanRepository } from "../repositories/plan.js";
import { AnalyticsRepository } from "../repositories/analytics.js";
import { TeamRepository } from "../repositories/team.js";
import { BucketRepository } from "../repositories/bucket.js";

const ZERO = new Decimal(0);

const DEFAULT_LEASE_TTL_SECONDS = 600;
const DEFAULT_PAGE_SIZE = 50;

function dec(value: unknown, fallback: Decimal = ZERO): Decimal {
  if (value === null || value === undefined) return fallback;
  if (value instanceof Decimal) return value;
  try {
    return new Decimal(typeof value === "string" ? value : String(value));
  } catch {
    throw new StoreError(`Failed to parse Decimal value: ${String(value)}`);
  }
}

function decParam(value: Decimal): string {
  return value.toString();
}

function featureWindowEnd(start: Date, period: FeatureLimit["period"]): Date {
  return resolveCalendarWindow(start, period).end;
}

function decRecord(raw: unknown): Record<string, Decimal> | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const out: Record<string, Decimal> = {};
  for (const [k, v] of Object.entries(raw as Record<string, unknown>)) {
    out[k] = dec(v);
  }
  return out;
}

function parseFeatureLimits(raw: unknown): Record<string, FeatureLimit> {
  if (!raw || typeof raw !== "object") return {};
  const out: Record<string, FeatureLimit> = {};
  for (const [k, v] of Object.entries(raw as Record<string, unknown>)) {
    if (v === null || typeof v !== "object" || Object.getPrototypeOf(v) !== Object.prototype) {
      out[k] = {
        value: v,
        maxCalls: 0,
        period: "monthly",
        onExceed: "deny",
      } as FeatureLimit;
    } else {
      const fl = v as Record<string, unknown>;
      const valueRaw = fl["value"];
      out[k] = {
        ...(valueRaw !== undefined ? { value: valueRaw } : {}),
        maxCalls: Number(fl.max_calls ?? 0),
        period: (String(fl.period ?? "monthly") as FeatureLimit["period"]) ?? "monthly",
        onExceed: (String(fl.on_exceed ?? "deny") as FeatureLimit["onExceed"]) ?? "deny",
      } as FeatureLimit;
    }
  }
  return out;
}

function parsePerOperation(raw: unknown): Record<string, OperationPolicy> {
  if (!raw || typeof raw !== "object") return {};
  const out: Record<string, OperationPolicy> = {};
  for (const [k, v] of Object.entries(raw as Record<string, unknown>)) {
    const op = snakeToCamelKeys((v ?? {}) as Record<string, unknown>);
    out[k] = {
      billingMode: (String(op.billingMode ?? "strict") as BillingMode) ?? "strict",
      maxConcurrent: op.maxConcurrent != null ? Number(op.maxConcurrent) : null,
      overdraftFloor: op.overdraftFloor != null ? dec(op.overdraftFloor) : null,
    };
  }
  return out;
}

export interface PgPool {
  query(text: string, params?: unknown[]): Promise<{ rows: unknown[] }>;
  end(): Promise<void>;
}

export interface PgPoolConstructor {
  new (config: { connectionString: string }): PgPool;
}

export class PostgresStore extends CreditStore {
  private databaseUrl: string;
  private poolCtor: PgPoolConstructor | null = null;
  private pool: PgPool | null = null;
  private poolPromise: Promise<PgPool> | null = null;
  private closed = false;
  private ownsPool: boolean;

  private _balanceRepo: BalanceRepository | null = null;
  private _deductionRepo: DeductionRepository | null = null;
  private _leaseRepo: LeaseRepository | null = null;
  private _pricingRepo: PricingRepository | null = null;
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

  private get pricingRepo(): PricingRepository {
    if (!this._pricingRepo) {
      this._pricingRepo = new PricingRepository(this.callproc.bind(this));
    }
    return this._pricingRepo;
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

  constructor(databaseUrl: string, poolOrCtor?: PgPool | PgPoolConstructor) {
    super();
    this.databaseUrl = databaseUrl;
    if (poolOrCtor && typeof (poolOrCtor as PgPool).query === "function") {
      this.pool = poolOrCtor as PgPool;
      this.ownsPool = false;
    } else {
      this.poolCtor = (poolOrCtor as PgPoolConstructor | undefined) ?? null;
      this.ownsPool = true;
    }
  }

  private async getPool(): Promise<PgPool> {
    if (this.closed) throw new StoreError("Store has been closed");
    if (!this.poolPromise) {
      this.poolPromise = (async () => {
        const Pool = await this.getPoolCtor();
        this.pool = new Pool({ connectionString: this.databaseUrl });
        return this.pool;
      })();
    }
    return this.poolPromise;
  }

  private async getPoolCtor(): Promise<PgPoolConstructor> {
    if (this.poolCtor) return this.poolCtor;
    const mod = await import("pg");
    this.poolCtor = mod.Pool as unknown as PgPoolConstructor;
    return this.poolCtor;
  }

  private async query(text: string, params?: unknown[]): Promise<unknown[]> {
    const pool = await this.getPool();
    const res = await pool.query(text, params);
    return res.rows;
  }

  async close(): Promise<void> {
    this.closed = true;
    if (this.ownsPool && this.poolPromise) {
      // poolPromise may still be pending (lazy getPool() awaiting dynamic
      // import). Await it first so the pool is guaranteed to exist before
      // we call .end(). Without this, checking `this.pool` alone would
      // miss the case where the IIFE hasn't assigned the pool yet.
      await this.poolPromise;
      if (this.pool) {
        await this.pool.end();
      }
      this.pool = null;
      this.poolPromise = null;
    }
  }

  private static readonly RPC_NAME_RE = /^[a-z_][a-z0-9_]*$/;

  private async callproc(name: string, params: unknown[]): Promise<unknown[]> {
    if (!PostgresStore.RPC_NAME_RE.test(name)) {
      throw new StoreError(`Invalid RPC name: ${name}`);
    }
    const placeholders = params.map((_, i) => `$${i + 1}`).join(", ");
    const rows = await this.query(`SELECT * FROM public.${name}(${placeholders})`, params);
    if (rows.length === 1) {
      const row = rows[0] as Record<string, unknown>;
      const keys = Object.keys(row);
      if (keys.length === 1) {
        const v = row[keys[0]];
        if (v !== null && typeof v === "object" && !Array.isArray(v)) {
          return [v];
        }
      }
    }
    return rows;
  }

  async setup(_databaseUrl?: string | null): Promise<SetupResult> {
    throw new StoreError(
      "PostgresStore.setup() does not run migrations. Apply the bundled SQL " +
        "migrations first — run `bursar migrate` via the Python CLI, or execute the " +
        "files in `python/src/bursar/sql/*.sql` (in filename order) against your " +
        "database. This store assumes the schema already exists.",
    );
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
      bucket ?? null,
      idempotencyKey ?? null,
    );
    if ("error" in row && row.error) {
      throw new StoreError(`credits_add: ${String(row.error)}`);
    }
    return {
      transactionId: String(row.id ?? ""),
      userId: String(row.user_id ?? userId),
      amount: dec(row.amount, amount),
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
    const idempotencyKey = options?.idempotencyKey ?? null;
    const minBalance = options?.minBalance ?? ZERO;
    const model = options?.model ?? null;
    const metadata = options?.metadata ?? {};
    const periodStart = options?.periodStart ?? null;
    const feature = options?.feature ?? null;
    const featureLimit = options?.featureLimit ?? null;
    const featurePeriodStart = options?.featurePeriodStart ?? null;
    const featurePeriodEnd =
      featureLimit != null && featurePeriodStart != null
        ? featureWindowEnd(featurePeriodStart, featureLimit.period)
        : null;

    const row = await this.deductionRepo.deductWithAllowance({
      userId,
      amount: decParam(amount),
      idempotencyKey,
      minBalance: decParam(minBalance),
      model,
      metadata: JSON.stringify(metadata ?? {}),
      skipAllowance: false,
      periodStart: periodStart != null ? periodStart.toISOString().slice(0, 10) : null,
      feature,
      featureMaxCalls: featureLimit != null ? featureLimit.maxCalls : null,
      featureOnExceed: featureLimit != null ? featureLimit.onExceed : null,
      featurePeriodStart:
        featurePeriodStart != null ? featurePeriodStart.toISOString().slice(0, 10) : null,
      featurePeriodEnd:
        featurePeriodEnd != null ? featurePeriodEnd.toISOString().slice(0, 10) : null,
    });

    if ("error" in row && row.error) {
      return {
        transactionId: "",
        userId,
        amount: ZERO,
        allowanceConsumed: ZERO,
        balanceAfter: dec(row.balance_after),
        idempotent: false,
        capWarning: null,
        featureLimitWarning: null,
        error: String(row.error),
      };
    }

    return {
      transactionId: String(row.transaction_id ?? ""),
      userId,
      amount: dec(row.amount),
      allowanceConsumed: dec(row.allowance_consumed),
      balanceAfter: dec(row.balance_after),
      idempotent: Boolean(row.idempotent),
      capWarning: row.cap_warning != null ? String(row.cap_warning) : null,
      featureLimitWarning:
        row.feature_limit_warning != null ? String(row.feature_limit_warning) : null,
      bucketBreakdown: decRecord(row.bucket_breakdown),
    };
  }

  async createLease(
    userId: string,
    amount: Decimal,
    operationType: string,
    options?: CreateLeaseOptions,
  ): Promise<LeaseResult> {
    const billingMode = options?.billingMode ?? "strict";
    const floor = options?.floor ?? ZERO;
    const overdraftFloor = options?.overdraftFloor ?? null;
    const periodStart = options?.periodStart ?? null;
    const feature = options?.feature ?? null;
    const featureLimit = options?.featureLimit ?? null;
    const featurePeriodStart = options?.featurePeriodStart ?? null;
    const featurePeriodEnd =
      featureLimit != null && featurePeriodStart != null
        ? featureWindowEnd(featurePeriodStart, featureLimit.period)
        : null;
    const row = await this.leaseRepo.createLease({
      userId,
      amount: decParam(amount),
      operationType,
      billingMode,
      floor: decParam(floor),
      maxConcurrent: options?.maxConcurrent ?? null,
      ttlSeconds: options?.ttlSeconds ?? DEFAULT_LEASE_TTL_SECONDS,
      model: options?.model ?? null,
      overdraftFloor: overdraftFloor != null ? decParam(overdraftFloor) : null,
      metadata: JSON.stringify(options?.metadata ?? {}),
      periodStart: periodStart != null ? periodStart.toISOString().slice(0, 10) : null,
      feature,
      featureMaxCalls: featureLimit != null ? featureLimit.maxCalls : null,
      featureOnExceed: featureLimit != null ? featureLimit.onExceed : null,
      featurePeriodStart:
        featurePeriodStart != null ? featurePeriodStart.toISOString().slice(0, 10) : null,
      featurePeriodEnd:
        featurePeriodEnd != null ? featurePeriodEnd.toISOString().slice(0, 10) : null,
    });

    if (!row || Object.keys(row).length === 0) {
      return {
        leaseId: "",
        userId,
        amount: ZERO,
        available: ZERO,
        reservedTotal: ZERO,
        billingMode,
        expiresAt: "",
        error: "no result",
      };
    }
    if ("error" in row && row.error) {
      return {
        leaseId: "",
        userId,
        amount: ZERO,
        available: dec(row.available),
        reservedTotal: dec(row.reserved),
        billingMode,
        expiresAt: "",
        error: String(row.error),
      };
    }
    return {
      leaseId: String(row.lease_id ?? ""),
      userId: String(row.user_id ?? userId),
      amount: dec(row.amount),
      available: dec(row.available),
      reservedTotal: dec(row.reserved),
      billingMode: (String(row.billing_mode ?? billingMode) as BillingMode) ?? billingMode,
      expiresAt: String(row.expires_at ?? ""),
    };
  }

  async settleLease(
    userId: string,
    leaseId: string,
    amount: Decimal,
    options?: SettleLeaseOptions,
  ): Promise<DeductionResult> {
    const minBalance = options?.minBalance ?? ZERO;
    const periodStart = options?.periodStart ?? null;
    const feature = options?.feature ?? null;
    const featureLimit = options?.featureLimit ?? null;
    const featurePeriodStart = options?.featurePeriodStart ?? null;
    const featurePeriodEnd =
      featureLimit != null && featurePeriodStart != null
        ? featureWindowEnd(featurePeriodStart, featureLimit.period)
        : null;
    const row = await this.leaseRepo.settleLease({
      userId,
      leaseId,
      amount: decParam(amount),
      idempotencyKey: options?.idempotencyKey ?? null,
      minBalance: decParam(minBalance),
      model: options?.model ?? null,
      metadata: JSON.stringify(options?.metadata ?? {}),
      skipAllowance: false,
      periodStart: periodStart != null ? periodStart.toISOString().slice(0, 10) : null,
      feature,
      featureMaxCalls: featureLimit != null ? featureLimit.maxCalls : null,
      featureOnExceed: featureLimit != null ? featureLimit.onExceed : null,
      featurePeriodStart:
        featurePeriodStart != null ? featurePeriodStart.toISOString().slice(0, 10) : null,
      featurePeriodEnd:
        featurePeriodEnd != null ? featurePeriodEnd.toISOString().slice(0, 10) : null,
    });

    if (!row || Object.keys(row).length === 0) {
      return {
        transactionId: "",
        userId,
        amount: ZERO,
        allowanceConsumed: ZERO,
        balanceAfter: ZERO,
        idempotent: false,
        capWarning: null,
        featureLimitWarning: null,
        error: "no result",
      };
    }
    if ("error" in row && row.error) {
      return {
        transactionId: "",
        userId,
        amount: ZERO,
        allowanceConsumed: ZERO,
        balanceAfter: dec(row.balance_after),
        idempotent: false,
        capWarning: null,
        featureLimitWarning: null,
        error: String(row.error),
      };
    }
    return {
      transactionId: String(row.transaction_id ?? ""),
      userId,
      amount: dec(row.amount),
      allowanceConsumed: dec(row.allowance_consumed),
      balanceAfter: dec(row.balance_after),
      idempotent: Boolean(row.idempotent),
      capWarning: row.cap_warning != null ? String(row.cap_warning) : null,
      featureLimitWarning:
        row.feature_limit_warning != null ? String(row.feature_limit_warning) : null,
      bucketBreakdown: decRecord(row.bucket_breakdown),
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
    const row = await this.leaseRepo.renewLease(userId, leaseId, ttlSeconds);
    if ("error" in row && row.error) {
      return {
        leaseId,
        userId,
        amount: ZERO,
        available: ZERO,
        reservedTotal: ZERO,
        billingMode: "strict",
        expiresAt: "",
        error: String(row.error),
      };
    }
    return {
      leaseId: String(row.lease_id ?? leaseId),
      userId,
      amount: dec(row.amount),
      available: dec(row.available),
      reservedTotal: dec(row.reserved),
      billingMode: String(row.billing_mode ?? "strict") as BillingMode,
      expiresAt: String(row.expires_at ?? ""),
    };
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

  async getActivePricing(): Promise<PricingConfigResult | null> {
    return this._loadActivePricing();
  }

  private normalizePricingConfig(
    row: Record<string, unknown>,
    defaultVersion: number,
  ): PricingConfigResult {
    const config = row.config as Record<string, unknown> | undefined;
    if (!config) {
      return { id: String(row.id ?? ""), config: {}, version: defaultVersion };
    }

    const rawFlatJobs: unknown =
      config && typeof config.metering === "object" && config.metering !== null
        ? (config.metering as Record<string, unknown>).flat_jobs
        : undefined;

    const result: PricingConfigResult = {
      id: String(row.id ?? ""),
      config: snakeToCamelKeys(config),
      version: Number(row.version ?? defaultVersion),
    };

    if (rawFlatJobs) {
      const metering = result.config.metering as Record<string, unknown> | undefined;
      if (metering) metering.flatJobs = rawFlatJobs;
    }

    return result;
  }

  private async _loadActivePricing(): Promise<PricingConfigResult | null> {
    const row = await this.pricingRepo.getActivePricing();
    if (!row || !row.config) return null;
    return this.normalizePricingConfig(row, 0);
  }

  async setActivePricing(config: Record<string, unknown>, label?: string | null): Promise<string> {
    const row = await this.pricingRepo.setActivePricing(
      JSON.stringify(camelToSnakeKeys(config)),
      label ?? null,
    );
    return String(row.id ?? "");
  }

  async getPricingHistory(): Promise<PricingConfigHistoryItem[]> {
    const rows = await this.pricingRepo.getPricingHistory();
    if (!rows) return [];
    return (rows as Record<string, unknown>[]).map((r) => ({
      id: String(r.id ?? ""),
      version: Number(r.version ?? 0),
      label: (r.label as string) ?? null,
      active: Boolean(r.active ?? false),
      createdAt: String(r.created_at ?? ""),
    }));
  }

  async getPricingConfig(version: number): Promise<PricingConfigResult | null> {
    const row = await this.pricingRepo.getPricingConfig(version);
    if (!row || !row.config) return null;
    return this.normalizePricingConfig(row, version);
  }

  async activatePricing(version: number): Promise<string> {
    const row = await this.pricingRepo.activatePricing(version);
    return String(row.id ?? "");
  }

  async migratePlanUsers(
    planKey: string,
    targetConfigVersion?: number | null,
  ): Promise<MigratePlanUsersResult> {
    const row = await this.planRepo.migratePlanUsers(planKey, targetConfigVersion ?? null);
    return {
      planKey: String(row.plan_key ?? planKey),
      targetPlanId: String(row.target_plan_id ?? ""),
      targetConfigVersion: Number(row.target_config_version ?? 0),
      migratedCount: Number(row.migrated_count ?? 0),
    };
  }

  async getUserPlan(userId: string): Promise<GetUserPlanResult> {
    const row = await this.planRepo.getUserPlan(userId);
    if (!row) {
      return {
        userId,
        planId: null,
        planLabel: null,
        allowanceAmount: ZERO,
        allowancePeriod: "calendar_month" as AllowancePeriod,
        entitlements: {},
        billingMode: "strict" as BillingMode,
      };
    }
    return {
      userId: String(row.user_id ?? userId),
      planId: (row.plan_id as string) ?? null,
      planLabel: (row.plan_label as string) ?? null,
      allowanceAmount: dec(row.allowance_amount),
      entitlements: parseFeatureLimits(row.entitlements) as unknown as Record<
        string,
        {
          value?: unknown;
          maxCalls?: number;
          period?: FeatureLimitPeriod;
          onExceed?: "deny" | "warn" | "notify";
        }
      >,
      billingMode: (String(row.billing_mode ?? "strict") as BillingMode) ?? "strict",
      perOperation: parsePerOperation(row.per_operation),
      maxConcurrent: row.max_concurrent != null ? Number(row.max_concurrent) : null,
      overdraftFloor: row.overdraft_floor != null ? dec(row.overdraft_floor) : null,
      allowancePeriod: (row.allowance_period as AllowancePeriod | undefined) ?? "calendar_month",
      planAssignedAt: row.plan_assigned_at != null ? new Date(String(row.plan_assigned_at)) : null,
      configVersion: row.config_version != null ? Number(row.config_version) : null,
    };
  }

  async checkFeature(userId: string, feature: string): Promise<CheckFeatureResult> {
    const plan = await this.getUserPlan(userId);
    const present = Object.prototype.hasOwnProperty.call(plan.entitlements, feature);
    const value = present
      ? ((plan.entitlements[feature] as Record<string, unknown>)?.["value"] ?? null)
      : null;
    return {
      userId,
      feature,
      value,
      hasFeature: present && value !== null && value !== undefined && value !== false,
    };
  }

  async setUserPlan(
    userId: string,
    planId: string,
    planAssignedAt?: Date | null,
  ): Promise<SetUserPlanResult> {
    const row = await this.planRepo.setUserPlan(
      userId,
      planId,
      planAssignedAt?.toISOString() ?? null,
    );
    return {
      userId: String(row.user_id ?? userId),
      planId: String(row.plan_id ?? planId),
      planAssignedAt: row.plan_assigned_at != null ? String(row.plan_assigned_at) : null,
    };
  }

  async unsetUserPlan(userId: string): Promise<{ userId: string }> {
    const row = await this.planRepo.unsetUserPlan(userId);
    return { userId: String(row.user_id ?? userId) };
  }

  async checkAllowance(userId: string, periodStart?: Date | null): Promise<AllowanceResult> {
    const row = await this.planRepo.checkAllowance(
      userId,
      periodStart != null ? periodStart.toISOString().slice(0, 10) : null,
    );
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

  async checkFeatureLimit(
    userId: string,
    feature: string,
    maxCalls: number,
    periodStart: Date,
    periodEnd: Date,
  ): Promise<FeatureLimitResult> {
    const row = await this.planRepo.checkFeatureLimit(
      userId,
      feature,
      maxCalls,
      periodStart.toISOString().slice(0, 10),
      periodEnd.toISOString().slice(0, 10),
    );
    if (!row) {
      return {
        userId,
        feature,
        limited: false,
        limit: 0,
        used: 0,
        remaining: 0,
        periodStart: "",
        periodEnd: "",
        action: null,
      };
    }
    return {
      userId: String(row.user_id ?? userId),
      feature: String(row.feature ?? feature),
      limited: Boolean(row.limited ?? false),
      limit: Number(row.limit ?? maxCalls),
      used: Number(row.used ?? 0),
      remaining: Number(row.remaining ?? Math.max(maxCalls - Number(row.used ?? 0), 0)),
      periodStart: String(row.period_start ?? ""),
      periodEnd: String(row.period_end ?? ""),
      action: (row.action as FeatureLimitResult["action"]) ?? null,
    };
  }

  async revokeCreditsByTxType(userId: string, txType: string): Promise<Record<string, unknown>> {
    return this.deductionRepo.revokeCreditsByTxType(userId, txType);
  }

  async refundCredits(
    transactionId: string,
    amount?: Decimal,
    reason?: string,
    metadata?: CreditMetadata | null,
  ): Promise<RefundResult> {
    const row = await this.deductionRepo.refundCredits(
      transactionId,
      amount != null ? decParam(amount) : null,
      reason ?? null,
      JSON.stringify(metadata ?? {}),
    );
    if ("error" in row && row.error) {
      return {
        refundTransactionId: "",
        originalTransactionId: transactionId,
        userId: String(row.user_id ?? ""),
        amount: ZERO,
        newBalance: dec(row.new_balance),
        error: String(row.error),
      };
    }
    return {
      refundTransactionId: String(row.refund_transaction_id ?? ""),
      originalTransactionId: transactionId,
      userId: String(row.user_id ?? ""),
      amount: dec(row.amount),
      newBalance: dec(row.new_balance),
      bucketBreakdown: decRecord(row.bucket_breakdown),
    };
  }

  async spendByUser(start: Date, end: Date): Promise<SpendByUserRow[]> {
    const rows = await this.analyticsRepo.spendByUser(start.toISOString(), end.toISOString());
    return (rows ?? []).map((r) => ({
      userId: String(r.user_id ?? ""),
      totalSpend: dec(r.total_spend),
      transactionCount: Number(r.transaction_count ?? 0),
    }));
  }

  async spendByModel(start: Date, end: Date): Promise<SpendByModelRow[]> {
    const rows = await this.analyticsRepo.spendByModel(start.toISOString(), end.toISOString());
    return (rows ?? []).map((r) => ({
      model: String(r.model ?? ""),
      totalSpend: dec(r.total_spend),
      transactionCount: Number(r.transaction_count ?? 0),
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
      transactionCount: Number(r.transaction_count ?? 0),
    }));
  }

  async listUserTransactions(
    userId: string,
    options?: ListTransactionsOptions,
  ): Promise<PaginatedTransactions> {
    const rows = await this.analyticsRepo.listUserTransactions(
      userId,
      options?.types ?? null,
      options?.fromDate?.toISOString() ?? null,
      options?.toDate?.toISOString() ?? null,
      options?.limit ?? DEFAULT_PAGE_SIZE,
      options?.offset ?? 0,
    );
    const items = (rows ?? []).map((r) => ({
      id: String(r.id ?? ""),
      userId: String(r.user_id ?? ""),
      amount: dec(r.amount),
      type: String(r.type ?? ""),
      referenceType: r.reference_type != null ? String(r.reference_type) : null,
      referenceId: r.reference_id != null ? String(r.reference_id) : null,
      metadata: (r.metadata ?? null) as Record<string, unknown> | null,
      createdAt: String(r.created_at ?? ""),
    }));
    const total = rows.length > 0 ? Number(rows[0].total_count ?? 0) : 0;
    return { items, total };
  }

  async listUsageEvents(
    userId: string,
    options?: ListUsageEventsOptions,
  ): Promise<PaginatedTransactions> {
    const rows = await this.analyticsRepo.listUsageEvents(
      userId,
      options?.fromDate?.toISOString() ?? null,
      options?.toDate?.toISOString() ?? null,
      options?.limit ?? DEFAULT_PAGE_SIZE,
      options?.offset ?? 0,
    );
    const items = (rows ?? []).map((r) => ({
      id: String(r.id ?? ""),
      userId: String(r.user_id ?? ""),
      amount: dec(r.amount),
      type: String(r.type ?? ""),
      referenceType: r.reference_type != null ? String(r.reference_type) : null,
      referenceId: r.reference_id != null ? String(r.reference_id) : null,
      metadata: (r.metadata ?? null) as Record<string, unknown> | null,
      createdAt: String(r.created_at ?? ""),
    }));
    const total = rows.length > 0 ? Number(rows[0].total_count ?? 0) : 0;
    return { items, total };
  }

  async aggregateStats(start: Date, end: Date): Promise<AggregateStats> {
    const row = await this.analyticsRepo.aggregateStats(start.toISOString(), end.toISOString());
    return {
      totalCreditsConsumed: dec(row.total_credits_consumed),
      activeUsers: Number(row.active_users ?? 0),
      avgDailySpend: dec(row.avg_daily_spend),
      topModel: String(row.top_model ?? ""),
      topUser: String(row.top_user ?? ""),
    };
  }

  async createTeam(name: string, initialBalance: Decimal = ZERO): Promise<CreateTeamResult> {
    const row = await this.teamRepo.createTeam(name, decParam(initialBalance));
    return {
      teamId: String(row.team_id ?? ""),
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

  async deductTeam(
    teamId: string,
    userId: string,
    amount: Decimal,
    metadata?: CreditMetadata | null,
    idempotencyKey?: string | null,
  ): Promise<TeamDeductionResult> {
    const meta: Record<string, unknown> = { ...(metadata ?? {}) };
    if (idempotencyKey) meta.idempotency_key = idempotencyKey;
    const row = await this.teamRepo.deductTeam(
      teamId,
      userId,
      decParam(amount),
      JSON.stringify(meta),
    );
    if ("error" in row && row.error) {
      return {
        transactionId: "",
        teamId,
        userId,
        amount: ZERO,
        teamBalanceAfter: dec(row.team_balance_after),
        error: String(row.error),
      };
    }
    return {
      transactionId: String(row.transaction_id ?? ""),
      teamId: String(row.team_id ?? teamId),
      userId: String(row.user_id ?? userId),
      amount: dec(row.amount, amount.negated()),
      teamBalanceAfter: dec(row.team_balance_after),
    };
  }

  async sweepExpiredCredits(dryRun = false, userId?: string): Promise<SweepResult> {
    const row = await this.bucketRepo.sweepExpiredCredits(dryRun, userId ?? null);
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
}
