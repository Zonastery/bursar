import Decimal from "decimal.js";
import { StoreError } from "../errors.js";
import { resolveCalendarWindow } from "../allowance.js";
import type { AllowancePeriod, FeatureLimitPeriod } from "../allowance.js";
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
  UserTransactionRow,
} from "../types.js";
import { CreditStore } from "./credit-store.js";
import type { CreateLeaseOptions, SettleLeaseOptions } from "./credit-store.js";

const ZERO = new Decimal(0);

/**
 * Parse a JSON money value into an exact `Decimal`. Supabase returns NUMERIC as
 * a JSON number or string; we coerce via `String(x)` so binary-float precision
 * is never introduced (contract §1). `null`/`undefined` → fallback.
 */
function dec(value: unknown, fallback: Decimal = ZERO): Decimal {
  if (value === null || value === undefined) return fallback;
  if (value instanceof Decimal) return value;
  try {
    return new Decimal(String(value));
  } catch {
    return fallback;
  }
}

/** A money serialized for a JSON RPC parameter: send as a decimal string. */
function decParam(value: Decimal): string {
  return value.toString();
}

/**
 * Derive the feature-limit window END from its (already calendar-aligned)
 * START — the manager only resolves/threads the start (mirrors Python
 * `base.py`); the RPC needs an explicit end for its `WHERE` clause, so this
 * store computes it via `resolveCalendarWindow` (idempotent on an aligned
 * start — re-resolving it yields the identical window).
 */
function featureWindowEnd(start: Date, period: FeatureLimit["period"]): Date {
  return resolveCalendarWindow(start, period).end;
}

/**
 * Parse a JSON `{tier_key: "3.0000", ...}` object (e.g. `tier_breakdown`,
 * `expired_by_bucket`) into `Record<string, Decimal>`, converting every value
 * the same way scalar money fields are (never left as a raw string/number).
 * Returns `null` when `raw` is not an object (absent/error responses).
 */
function decRecord(raw: unknown): Record<string, Decimal> | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const out: Record<string, Decimal> = {};
  for (const [k, v] of Object.entries(raw as Record<string, unknown>)) {
    out[k] = dec(v);
  }
  return out;
}

/** Parse the ``entitlements`` JSONB map into typed `FeatureLimit` records. */
function parseFeatureLimits(raw: unknown): Record<string, FeatureLimit> {
  if (!raw || typeof raw !== "object") return {};
  const out: Record<string, FeatureLimit> = {};
  for (const [k, v] of Object.entries(raw as Record<string, unknown>)) {
    if (v === null || typeof v !== "object" || Object.getPrototypeOf(v) !== Object.prototype) {
      // Plain or non-plain-object value (e.g. true, 20, ""): wrap as value.
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
        maxCalls: Number(fl.max_calls ?? fl.maxCalls ?? 0),
        period: (String(fl.period ?? "monthly") as FeatureLimit["period"]) ?? "monthly",
        onExceed:
          (String(
            fl.onExceed ?? fl.on_exceed ?? fl.action ?? "deny",
          ) as FeatureLimit["onExceed"]) ?? "deny",
      } as FeatureLimit;
    }
  }
  return out;
}

/** Parse the ``per_operation`` JSONB map into typed `OperationPolicy` records. */
function parsePerOperation(raw: unknown): Record<string, OperationPolicy> {
  if (!raw || typeof raw !== "object") return {};
  const out: Record<string, OperationPolicy> = {};
  for (const [k, v] of Object.entries(raw as Record<string, unknown>)) {
    const op = (v ?? {}) as Record<string, unknown>;
    out[k] = {
      billingMode:
        (String(op.billing_mode ?? op.billingMode ?? "strict") as BillingMode) ?? "strict",
      maxConcurrent:
        op.max_concurrent != null
          ? Number(op.max_concurrent)
          : op.maxConcurrent != null
            ? Number(op.maxConcurrent)
            : null,
      overdraftFloor:
        op.overdraft_floor != null
          ? dec(op.overdraft_floor)
          : op.overdraftFloor != null
            ? dec(op.overdraftFloor)
            : null,
    };
  }
  return out;
}

/**
 * Credit store backed by Supabase RPCs via raw HTTP (fetch).
 *
 * No supabase-js dependency — makes direct POST requests to the Supabase REST API.
 *
 * Error handling (M10 parity): network/fetch failures and JSON-decode errors are
 * wrapped in `StoreError`; a non-2xx HTTP response throws `StoreError`. *Business*
 * outcomes that the RPC returns as a `{"error": code}` envelope (insufficient
 * credits, cap reached, over-refund, …) are NOT thrown — they are surfaced on the
 * result model's `.error` field (e.g. `DeductionResult.error`), consistent with
 * how the Postgres store and the Python SDK behave. The manager maps codes to
 * typed exceptions.
 *
 * Args:
 *   url: Supabase project URL (e.g. ``https://<project>.supabase.co``).
 *   key: Supabase ``service_role`` key.
 */
export class HttpxSupabaseStore extends CreditStore {
  private url: string;
  private key: string;

  constructor(url: string, key: string, pricingCacheTtl: number = 300) {
    super(pricingCacheTtl);
    this.url = url.replace(/\/+$/, "");
    this.key = key;
  }

  /** POST to an RPC, returning the parsed JSON body. Wraps transport/parse errors. */
  private async post(fn: string, params: Record<string, unknown>): Promise<unknown> {
    let resp: Response;
    try {
      resp = await fetch(`${this.url}/rest/v1/rpc/${fn}`, {
        method: "POST",
        headers: {
          apikey: this.key,
          authorization: `Bearer ${this.key}`,
          "content-type": "application/json",
        },
        body: JSON.stringify(params),
      });
    } catch (err) {
      // Network / DNS / connection-refused etc.
      throw new StoreError(
        `Supabase RPC ${fn} request failed: ${err instanceof Error ? err.message : String(err)}`,
      );
    }

    if (!resp.ok) {
      let text = "";
      try {
        text = await resp.text();
      } catch {
        // ignore body-read failures
      }
      throw new StoreError(`Supabase RPC ${fn} failed (${resp.status}): ${text}`);
    }

    try {
      return await resp.json();
    } catch (err) {
      throw new StoreError(
        `Supabase RPC ${fn} returned invalid JSON: ${err instanceof Error ? err.message : String(err)}`,
      );
    }
  }

  /**
   * GET a table row from the Supabase REST API.
   * Uses URL query params for filtering (Supabase REST-style syntax).
   */
  private async restGet(
    table: string,
    params: Record<string, string>,
  ): Promise<Record<string, unknown>[]> {
    const qs = new URLSearchParams(params).toString();
    let resp: Response;
    try {
      resp = await fetch(`${this.url}/rest/v1/${table}?${qs}`, {
        method: "GET",
        headers: {
          apikey: this.key,
          authorization: `Bearer ${this.key}`,
        },
      });
    } catch (err) {
      throw new StoreError(
        `Supabase GET ${table} failed: ${err instanceof Error ? err.message : String(err)}`,
      );
    }

    if (!resp.ok) {
      let text = "";
      try {
        text = await resp.text();
      } catch {
        // ignore
      }
      throw new StoreError(`Supabase GET ${table} failed (${resp.status}): ${text}`);
    }

    try {
      return (await resp.json()) as Record<string, unknown>[];
    } catch (err) {
      throw new StoreError(
        `Supabase GET ${table} returned invalid JSON: ${err instanceof Error ? err.message : String(err)}`,
      );
    }
  }

  /** RPC returning a single JSONB object. */
  private async rpc(fn: string, params: Record<string, unknown>): Promise<Record<string, unknown>> {
    const data = await this.post(fn, params);
    if (data === null || data === undefined) return {};
    if (Array.isArray(data)) {
      const first = data[0];
      return first != null && typeof first === "object" ? (first as Record<string, unknown>) : {};
    }
    if (typeof data === "object") return data as Record<string, unknown>;
    return { value: data };
  }

  /** RPC returning a set of rows. Always returns ALL rows. */
  private async rpcAll(
    fn: string,
    params: Record<string, unknown>,
  ): Promise<Record<string, unknown>[]> {
    const data = await this.post(fn, params);
    if (data === null || data === undefined) return [];
    if (!Array.isArray(data)) return [data as Record<string, unknown>];
    return data.filter((r: unknown): r is Record<string, unknown> => r != null);
  }

  /**
   * Return the business-error code if `row` is an `{"error": code}` envelope,
   * else null. An unexpected `error` value that is not a known business code is
   * still surfaced (callers decide), but recognised codes are the contract set.
   */
  private errorCode(row: Record<string, unknown>): string | null {
    if ("error" in row && row.error) {
      return String(row.error);
    }
    return null;
  }

  async setup(_databaseUrl?: string | null): Promise<SetupResult> {
    throw new StoreError(
      "HttpxSupabaseStore.setup() cannot run migrations over the REST API. Apply the " +
        "bundled SQL migrations via the Python CLI (`bursar migrate`) or by executing " +
        "`python/src/bursar/sql/*.sql` (in filename order) against your database.",
    );
  }

  async getBalance(userId: string): Promise<BalanceResult> {
    const row = await this.rpc("get_credits_balance", { p_user_id: userId });
    const code = this.errorCode(row);
    if (code) throw new StoreError(`get_credits_balance: ${code}`);
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
    // NOTE: threaded through defensively as a new named param to the
    // `credits_add` RPC. Confirm the SQL migration adds a `p_idempotency_key`
    // parameter (with a default) before relying on this against a real
    // database (see 011_lazy_expiry.sql).
    idempotencyKey?: string | null,
  ): Promise<AddCreditsResult> {
    const meta: Record<string, unknown> = { ...(metadata ?? {}) };
    if (expiresAt) {
      meta.expires_at = expiresAt instanceof Date ? expiresAt.toISOString() : String(expiresAt);
    }
    const row = await this.rpc("credits_add", {
      p_user_id: userId,
      p_amount: decParam(amount),
      p_type: type,
      p_metadata: meta,
      p_bucket: bucket ?? null,
      p_idempotency_key: idempotencyKey ?? null,
    });
    const code = this.errorCode(row);
    if (code) throw new StoreError(`credits_add: ${code}`);
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

    const row = await this.rpc("deduct_with_allowance", {
      p_user_id: userId,
      p_amount: decParam(amount),
      p_idempotency_key: idempotencyKey,
      p_min_balance: decParam(minBalance),
      p_model: model,
      p_metadata: metadata ?? {},
      p_period_start: periodStart != null ? periodStart.toISOString().slice(0, 10) : null,
      p_feature: feature,
      p_feature_max_calls: featureLimit != null ? featureLimit.maxCalls : null,
      p_feature_action: featureLimit != null ? featureLimit.onExceed : null,
      p_feature_period_start:
        featurePeriodStart != null ? featurePeriodStart.toISOString().slice(0, 10) : null,
      p_feature_period_end:
        featurePeriodEnd != null ? featurePeriodEnd.toISOString().slice(0, 10) : null,
    });

    const code = this.errorCode(row);
    if (code) {
      return {
        transactionId: "",
        userId,
        amount: ZERO,
        allowanceConsumed: ZERO,
        balanceAfter: dec(row.balance_after),
        idempotent: false,
        capWarning: null,
        featureLimitWarning: null,
        error: code,
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

  // ── Lease lifecycle (atomic admission) ─────────────────────────────

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
    const row = await this.rpc("create_lease", {
      p_user_id: userId,
      p_amount: decParam(amount),
      p_operation_type: operationType,
      p_billing_mode: billingMode,
      p_floor: decParam(floor),
      p_max_concurrent: options?.maxConcurrent ?? null,
      p_ttl_seconds: options?.ttlSeconds ?? 600,
      p_model: options?.model ?? null,
      p_overdraft_floor: overdraftFloor != null ? decParam(overdraftFloor) : null,
      p_metadata: options?.metadata ?? {},
      p_period_start: periodStart != null ? periodStart.toISOString().slice(0, 10) : null,
      p_feature: feature,
      p_feature_max_calls: featureLimit != null ? featureLimit.maxCalls : null,
      p_feature_action: featureLimit != null ? featureLimit.onExceed : null,
      p_feature_period_start:
        featurePeriodStart != null ? featurePeriodStart.toISOString().slice(0, 10) : null,
      p_feature_period_end:
        featurePeriodEnd != null ? featurePeriodEnd.toISOString().slice(0, 10) : null,
    });

    const code = this.errorCode(row);
    if (code) {
      return {
        leaseId: "",
        userId,
        amount: ZERO,
        available: dec(row.available),
        reservedTotal: dec(row.reserved),
        billingMode,
        expiresAt: "",
        error: code,
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
    const row = await this.rpc("settle_lease", {
      p_user_id: userId,
      p_lease_id: leaseId,
      p_amount: decParam(amount),
      p_idempotency_key: options?.idempotencyKey ?? null,
      p_min_balance: decParam(minBalance),
      p_model: options?.model ?? null,
      p_metadata: options?.metadata ?? {},
      p_period_start: periodStart != null ? periodStart.toISOString().slice(0, 10) : null,
      p_feature: feature,
      p_feature_max_calls: featureLimit != null ? featureLimit.maxCalls : null,
      p_feature_action: featureLimit != null ? featureLimit.onExceed : null,
      p_feature_period_start:
        featurePeriodStart != null ? featurePeriodStart.toISOString().slice(0, 10) : null,
      p_feature_period_end:
        featurePeriodEnd != null ? featurePeriodEnd.toISOString().slice(0, 10) : null,
    });

    const code = this.errorCode(row);
    if (code) {
      return {
        transactionId: "",
        userId,
        amount: ZERO,
        allowanceConsumed: ZERO,
        balanceAfter: dec(row.balance_after),
        idempotent: false,
        capWarning: null,
        featureLimitWarning: null,
        error: code,
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
    const row = await this.rpc("release_lease", { p_user_id: userId, p_lease_id: leaseId });
    return {
      leaseId,
      userId,
      released: Boolean(row.released),
      reason: row.reason != null ? String(row.reason) : null,
    };
  }

  async renewLease(userId: string, leaseId: string, ttlSeconds: number): Promise<LeaseResult> {
    const row = await this.rpc("renew_lease", {
      p_user_id: userId,
      p_lease_id: leaseId,
      p_ttl_seconds: ttlSeconds,
    });
    const code = this.errorCode(row);
    if (code) {
      return {
        leaseId,
        userId,
        amount: ZERO,
        available: ZERO,
        reservedTotal: ZERO,
        billingMode: "strict",
        expiresAt: "",
        error: code,
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
    const row = await this.rpc("get_available_credits", { p_user_id: userId });
    return {
      userId,
      balance: dec(row.balance),
      reserved: dec(row.reserved),
      available: dec(row.available),
    };
  }

  async getActivePricing(): Promise<PricingConfigResult | null> {
    return this._getCachedPricing(() => this._loadActivePricing());
  }

  private async _loadActivePricing(): Promise<PricingConfigResult | null> {
    const row = await this.rpc("get_active_pricing_config", {});
    if (!row || Object.keys(row).length === 0) return null;
    const code = this.errorCode(row);
    if (code) throw new StoreError(`get_active_pricing_config: ${code}`);
    return row as unknown as PricingConfigResult;
  }

  async setActivePricing(config: Record<string, unknown>, label?: string | null): Promise<string> {
    const row = await this.rpc("set_active_pricing_config", {
      p_config: config,
      p_label: label ?? null,
    });
    const code = this.errorCode(row);
    if (code) throw new StoreError(`set_active_pricing_config: ${code}`);
    this.invalidatePricingCache();
    return String(row.id ?? "");
  }

  // H8: pricing history / activation — mirrors Python base.py:293-312.

  async getPricingHistory(): Promise<PricingConfigHistoryItem[]> {
    const rows = await this.rpc("get_pricing_history", {});
    if (!rows) return [];
    const arr = Array.isArray(rows) ? rows : [rows];
    return (arr as Record<string, unknown>[]).map((r) => ({
      id: String(r.id ?? ""),
      version: Number(r.version ?? 0),
      label: (r.label as string) ?? null,
      active: Boolean(r.active ?? false),
      createdAt: String(r.created_at ?? ""),
    }));
  }

  async getPricingConfig(version: number): Promise<PricingConfigResult | null> {
    const row = await this.rpc("get_pricing_config", { p_version: version });
    if (!row || Object.keys(row).length === 0) return null;
    const code = this.errorCode(row);
    if (code) throw new StoreError(`get_pricing_config: ${code}`);
    return {
      id: String(row.id ?? ""),
      config: row.config as Record<string, unknown>,
      version: Number(row.version ?? version),
    };
  }

  async activatePricing(version: number): Promise<string> {
    const row = await this.rpc("activate_pricing", { p_version: version });
    const code = this.errorCode(row);
    if (code) throw new StoreError(`activate_pricing: ${code}`);
    this.invalidatePricingCache();
    return String(row.id ?? "");
  }

  // ── Plan management ────────────────────────────────────────────────

  async getUserPlan(userId: string): Promise<GetUserPlanResult> {
    const row = await this.rpc("get_user_plan", { p_user_id: userId });
    if (!row || Object.keys(row).length === 0) {
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
    const code = this.errorCode(row);
    if (code) throw new StoreError(`get_user_plan: ${code}`);
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
      // M6: presence-vs-truthiness — numeric 0 / "" count as present.
      hasFeature: present && value !== null && value !== undefined && value !== false,
    };
  }

  async setUserPlan(
    userId: string,
    planId: string,
    planAssignedAt?: Date | null,
  ): Promise<SetUserPlanResult> {
    const params: Record<string, unknown> = {
      p_user_id: userId,
      p_plan_key: planId,
    };
    if (planAssignedAt != null) {
      params.p_plan_assigned_at = planAssignedAt.toISOString();
    }
    const row = await this.rpc("set_user_plan", params);
    const code = this.errorCode(row);
    if (code) throw new StoreError(`set_user_plan: ${code}`);
    return {
      userId: String(row.user_id ?? userId),
      planId: String(row.plan_id ?? planId),
      planAssignedAt: row.plan_assigned_at != null ? String(row.plan_assigned_at) : null,
    };
  }

  async unsetUserPlan(userId: string): Promise<{ userId: string }> {
    const row = await this.rpc("unset_user_plan", { p_user_id: userId });
    return { userId: String(row?.user_id ?? userId) };
  }

  async checkAllowance(userId: string, periodStart?: Date | null): Promise<AllowanceResult> {
    const row = await this.rpc("check_plan_allowance", {
      p_user_id: userId,
      p_period_start: periodStart != null ? periodStart.toISOString().slice(0, 10) : null,
    });
    if (!row || Object.keys(row).length === 0) {
      return { planId: "", allowanceRemaining: ZERO, periodStart: "", periodEnd: "" };
    }
    const code = this.errorCode(row);
    if (code) throw new StoreError(`check_plan_allowance: ${code}`);
    return {
      planId: String(row.plan_id ?? ""),
      allowanceRemaining: dec(row.allowance_remaining),
      periodStart: String(row.period_start ?? ""),
      periodEnd: String(row.period_end ?? ""),
    };
  }

  async incrementUsageWindow(userId: string, planId: string, amount: Decimal): Promise<void> {
    const row = await this.rpc("increment_usage_window", {
      p_user_id: userId,
      p_plan_id: planId,
      p_amount: decParam(amount),
    });
    const code = this.errorCode(row);
    if (code) throw new StoreError(`increment_usage_window: ${code}`);
  }

  /** Advisory, non-locking read of invocation-count usage (UI only). Mirrors `checkSpendCap`. */
  async checkFeatureLimit(
    userId: string,
    feature: string,
    maxCalls: number,
    periodStart: Date,
    periodEnd: Date,
  ): Promise<FeatureLimitResult> {
    const row = await this.rpc("check_feature_limit", {
      p_user_id: userId,
      p_feature: feature,
      p_max_calls: maxCalls,
      p_period_start: periodStart.toISOString().slice(0, 10),
      p_period_end: periodEnd.toISOString().slice(0, 10),
    });
    if (!row || Object.keys(row).length === 0) {
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
    const code = this.errorCode(row);
    if (code) throw new StoreError(`check_feature_limit: ${code}`);
    return {
      userId: String(row.user_id ?? userId),
      feature: String(row.feature ?? feature),
      limited: Boolean(row.limited ?? true),
      limit: Number(row.limit ?? maxCalls),
      used: Number(row.used ?? 0),
      remaining: Number(row.remaining ?? Math.max(maxCalls - Number(row.used ?? 0), 0)),
      periodStart: String(row.period_start ?? ""),
      periodEnd: String(row.period_end ?? ""),
      action: (row.action as FeatureLimitResult["action"]) ?? null,
    };
  }

  // ── Spend caps and rate limiting ──────────────────────────────────────

  async checkSpendCap(
    userId: string,
    model?: string | null,
    amount?: Decimal,
  ): Promise<CapCheckResult> {
    const row = await this.rpc("check_spend_cap", {
      p_user_id: userId,
      p_model: model ?? null,
      p_amount: decParam(amount ?? ZERO),
    });
    if (!row || Object.keys(row).length === 0) {
      return { capped: false, currentSpend: ZERO, limit: ZERO, action: null };
    }
    return {
      capped: Boolean(row.capped),
      currentSpend: dec(row.current_spend),
      limit: dec(row.cap_limit),
      action: (row.action as CapCheckResult["action"]) ?? null,
      model: row.model ? String(row.model) : undefined,
    };
  }

  // ── Revoke credits by tx type ──────────────────────────────────────────

  async revokeCreditsByTxType(userId: string, txType: string): Promise<Record<string, unknown>> {
    const row = await this.rpc("revoke_credits_by_tx_type", {
      p_user_id: userId,
      p_tx_type: txType,
    });
    const code = this.errorCode(row);
    if (code) throw new StoreError(`revoke_credits_by_tx_type: ${code}`);
    return row as Record<string, unknown>;
  }

  // ── Refunds ──────────────────────────────────────────────────────────

  async refundCredits(
    transactionId: string,
    amount?: Decimal,
    reason?: string,
    metadata?: CreditMetadata | null,
  ): Promise<RefundResult> {
    const row = await this.rpc("refund_credits", {
      p_transaction_id: transactionId,
      p_amount: amount != null ? decParam(amount) : null,
      p_reason: reason ?? null,
      p_metadata: metadata ?? {},
    });
    const code = this.errorCode(row);
    if (code) {
      return {
        refundTransactionId: "",
        originalTransactionId: transactionId,
        userId: String(row.user_id ?? ""),
        amount: ZERO,
        newBalance: dec(row.new_balance),
        error: code,
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

  // ── Usage analytics ──────────────────────────────────────────────────

  async spendByUser(start: Date, end: Date): Promise<SpendByUserRow[]> {
    const rows = await this.rpcAll("spend_by_user", {
      p_start: start.toISOString(),
      p_end: end.toISOString(),
    });
    return rows.map((row) => ({
      userId: String(row.user_id ?? ""),
      totalSpend: dec(row.total_spend),
      transactionCount: Number(row.transaction_count ?? 0),
    }));
  }

  async spendByModel(start: Date, end: Date): Promise<SpendByModelRow[]> {
    const rows = await this.rpcAll("spend_by_model", {
      p_start: start.toISOString(),
      p_end: end.toISOString(),
    });
    return rows.map((row) => ({
      model: String(row.model ?? ""),
      totalSpend: dec(row.total_spend),
      transactionCount: Number(row.transaction_count ?? 0),
    }));
  }

  async topUsers(limit: number, start: Date, end: Date): Promise<TopUserRow[]> {
    const rows = await this.rpcAll("top_users", {
      p_limit: limit,
      p_start: start.toISOString(),
      p_end: end.toISOString(),
    });
    return rows.map((row) => ({
      userId: String(row.user_id ?? ""),
      totalSpend: dec(row.total_spend),
    }));
  }

  async dailySpend(start: Date, end: Date): Promise<DailySpendRow[]> {
    const rows = await this.rpcAll("daily_spend", {
      p_start: start.toISOString(),
      p_end: end.toISOString(),
    });
    return rows.map((row) => ({
      date: String(row.date ?? ""),
      totalSpend: dec(row.total_spend),
      transactionCount: Number(row.transaction_count ?? 0),
    }));
  }

  // ── Transaction listing ─────────────────────────────────────────────

  async listUserTransactions(
    userId: string,
    options?: ListTransactionsOptions,
  ): Promise<PaginatedTransactions> {
    const rows = await this.rpcAll("list_user_transactions", {
      p_user_id: userId,
      p_types: options?.types ?? null,
      p_from_date: options?.fromDate?.toISOString() ?? null,
      p_to_date: options?.toDate?.toISOString() ?? null,
      p_limit: options?.limit ?? 50,
      p_offset: options?.offset ?? 0,
    });
    const items = rows.map((r) => ({
      id: String(r.id ?? ""),
      userId: String(r.user_id ?? ""),
      amount: dec(r.amount),
      type: String(r.type ?? ""),
      referenceType: r.reference_type != null ? String(r.reference_type) : null,
      referenceId: r.reference_id != null ? String(r.reference_id) : null,
      metadata: (r.metadata as Record<string, unknown> | null) ?? null,
      createdAt: String(r.created_at ?? ""),
    }));
    const total =
      rows.length > 0 ? Number((rows[0] as Record<string, unknown>).total_count ?? 0) : 0;
    return { items, total };
  }

  async listUsageEvents(
    userId: string,
    options?: ListUsageEventsOptions,
  ): Promise<PaginatedTransactions> {
    const rows = await this.rpcAll("list_usage_events", {
      p_user_id: userId,
      p_from_date: options?.fromDate?.toISOString() ?? null,
      p_to_date: options?.toDate?.toISOString() ?? null,
      p_limit: options?.limit ?? 50,
      p_offset: options?.offset ?? 0,
    });
    const items = rows.map((r) => ({
      id: String(r.id ?? ""),
      userId: String(r.user_id ?? ""),
      amount: dec(r.amount),
      type: String(r.type ?? ""),
      referenceType: r.reference_type != null ? String(r.reference_type) : null,
      referenceId: r.reference_id != null ? String(r.reference_id) : null,
      metadata: (r.metadata as Record<string, unknown> | null) ?? null,
      createdAt: String(r.created_at ?? ""),
    }));
    const total =
      rows.length > 0 ? Number((rows[0] as Record<string, unknown>).total_count ?? 0) : 0;
    return { items, total };
  }

  // ── Aggregate stats ────────────────────────────────────────────────

  async aggregateStats(start: Date, end: Date): Promise<AggregateStats> {
    const row = await this.rpc("aggregate_stats", {
      p_start: start.toISOString(),
      p_end: end.toISOString(),
    });
    return {
      totalCreditsConsumed: dec(row.total_credits_consumed),
      activeUsers: Number(row.active_users ?? 0),
      avgDailySpend: dec(row.avg_daily_spend),
      topModel: String(row.top_model ?? ""),
      topUser: String(row.top_user ?? ""),
    };
  }

  // ── Team/shared balance pools ────────────────────────────────────────

  async createTeam(name: string, initialBalance: Decimal = ZERO): Promise<CreateTeamResult> {
    const row = await this.rpc("create_team", {
      p_name: name,
      p_initial_balance: decParam(initialBalance),
    });
    const code = this.errorCode(row);
    if (code) throw new StoreError(`create_team: ${code}`);
    return {
      teamId: String(row.team_id ?? ""),
      name: String(row.name ?? name),
    };
  }

  async getTeamBalance(teamId: string): Promise<TeamBalanceResult> {
    const row = await this.rpc("get_team_balance", { p_team_id: teamId });
    if (!row || Object.keys(row).length === 0 || ("error" in row && row.error)) {
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
    const row = await this.rpc("add_team_member", {
      p_team_id: teamId,
      p_user_id: userId,
      p_role: role,
      p_spend_cap: spendCap != null ? decParam(spendCap) : null,
    });
    const code = this.errorCode(row);
    if (code) throw new StoreError(`add_team_member: ${code}`);
    return {
      teamId: String(row.team_id ?? teamId),
      userId: String(row.user_id ?? userId),
      role: String(row.role ?? role),
    };
  }

  async getTeamMembers(teamId: string): Promise<TeamMember[]> {
    const rows = await this.rpcAll("get_team_members", { p_team_id: teamId });
    return rows.map((row) => ({
      userId: String(row.user_id ?? ""),
      role: String(row.role ?? "member"),
      spendCap: row.spend_cap != null ? dec(row.spend_cap) : null,
      totalSpent: dec(row.total_spent),
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
    const row = await this.rpc("deduct_team", {
      p_team_id: teamId,
      p_user_id: userId,
      p_amount: decParam(amount),
      p_metadata: meta,
    });
    const code = this.errorCode(row);
    if (code) {
      return {
        transactionId: "",
        teamId,
        userId,
        amount: ZERO,
        teamBalanceAfter: dec(row.team_balance_after),
        error: code,
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

  // ── Credit expiry ────────────────────────────────────────────────────

  async sweepExpiredCredits(dryRun = false, userId?: string): Promise<SweepResult> {
    // NOTE: `userId` is threaded through defensively as a new named param to
    // `expire_credits`. Confirm the SQL migration adds a `p_user_id` parameter
    // (with a default of NULL, meaning "global sweep") before relying on this
    // against a real database (see 011_lazy_expiry.sql).
    const row = await this.rpc("expire_credits", {
      p_dry_run: dryRun,
      p_user_id: userId ?? null,
    });
    const code = this.errorCode(row);
    if (code) throw new StoreError(`expire_credits: ${code}`);
    return {
      expiredCount: Number(row.expired_count ?? 0),
      expiredAmount: dec(row.expired_amount),
      dryRun,
      expiredByBucket: decRecord(row.expired_by_bucket),
    };
  }

  // ── Credit tiers ─────────────────────────────────────────────────────

  // ── Transaction lookup ────────────────────────────────────────────────

  async getTransaction(userId: string, transactionId: string): Promise<UserTransactionRow | null> {
    const rows = await this.restGet("credit_transactions", {
      id: `eq.${transactionId}`,
      user_id: `eq.${userId}`,
      select: "*",
      limit: "1",
    });
    if (!rows || rows.length === 0) return null;
    const r = rows[0];
    return {
      id: String(r.id ?? ""),
      userId: String(r.user_id ?? userId),
      amount: dec(r.amount),
      type: String(r.type ?? ""),
      referenceType: r.reference_type != null ? String(r.reference_type) : null,
      referenceId: r.reference_id != null ? String(r.reference_id) : null,
      metadata: (r.metadata as Record<string, unknown> | null) ?? null,
      createdAt: String(r.created_at ?? ""),
    };
  }

  async getBucketBalances(userId: string): Promise<BucketBalancesResult> {
    // get_user_credit_buckets returns one JSONB envelope object (not a rowset):
    // {user_id, buckets: [...]} — use rpc() (single-object), not
    // rpcAll() (which is for genuine SETOF/TABLE-returning functions).
    const envelope = await this.rpc("get_user_credit_buckets", { p_user_id: userId });
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
