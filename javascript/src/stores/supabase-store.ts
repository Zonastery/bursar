import Decimal from "decimal.js";
import { StoreError } from "../errors.js";
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
import type { CreditStore } from "./credit-store.js";

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
export class HttpxSupabaseStore implements CreditStore {
  private url: string;
  private key: string;

  constructor(url: string, key: string) {
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
        "bundled SQL migrations via the Python CLI (`ducto migrate`) or by executing " +
        "`python/src/ducto/sql/*.sql` (in filename order) against your database.",
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
    });
    const code = this.errorCode(row);
    if (code) throw new StoreError(`credits_add: ${code}`);
    return {
      transactionId: String(row.id ?? ""),
      userId: String(row.user_id ?? userId),
      amount: dec(row.amount, amount),
      newBalance: dec(row.new_balance),
      lifetimePurchased: dec(row.lifetime_purchased),
    };
  }

  async reserveCredits(
    userId: string,
    amount: Decimal,
    operationType: string,
    metadata?: CreditMetadata | null,
    minBalance: Decimal = new Decimal(5),
  ): Promise<ReserveResult> {
    const row = await this.rpc("reserve_credits", {
      p_user_id: userId,
      p_amount: decParam(amount),
      p_operation_type: operationType,
      p_metadata: metadata ?? {},
      p_min_balance: decParam(minBalance),
    });

    const code = this.errorCode(row);
    if (code) {
      return {
        reservationId: "",
        userId,
        amount: ZERO,
        balance: dec(row.balance),
        reservedTotal: dec(row.reserved),
        error: code,
      };
    }

    return {
      reservationId: String(row.reservation_id ?? ""),
      userId: String(row.user_id ?? userId),
      amount: dec(row.amount),
      balance: dec(row.balance),
      reservedTotal: dec(row.reserved),
    };
  }

  async deductCredits(
    userId: string,
    reservationId: string,
    amount: Decimal,
    idempotencyKey?: string | null,
    metadata?: CreditMetadata | null,
  ): Promise<DeductionResult> {
    const meta: Record<string, unknown> = { ...(metadata ?? {}) };
    if (idempotencyKey) meta.idempotency_key = idempotencyKey;

    const row = await this.rpc("deduct_credits", {
      p_user_id: userId,
      p_reservation_id: reservationId,
      p_amount: decParam(amount),
      p_metadata: meta,
    });

    const code = this.errorCode(row);
    if (code) {
      return {
        transactionId: "",
        userId,
        amount: amount.negated(),
        allowanceConsumed: ZERO,
        balanceAfter: dec(row.new_balance),
        idempotent: false,
        capWarning: null,
        error: code,
      };
    }

    return {
      transactionId: String(row.id ?? ""),
      userId: String(row.user_id ?? userId),
      amount: dec(row.amount, amount.negated()),
      allowanceConsumed: ZERO,
      balanceAfter: dec(row.new_balance),
      idempotent: Boolean(row.idempotent),
      capWarning: null,
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

    const row = await this.rpc("deduct_with_allowance", {
      p_user_id: userId,
      p_amount: decParam(amount),
      p_idempotency_key: idempotencyKey,
      p_min_balance: decParam(minBalance),
      p_model: model,
      p_metadata: metadata ?? {},
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
    };
  }

  async getActivePricing(): Promise<PricingConfigResult | null> {
    const row = await this.rpc("get_active_pricing_config", {});
    if (!row || Object.keys(row).length === 0) return null;
    const code = this.errorCode(row);
    if (code) throw new StoreError(`get_active_pricing_config: ${code}`);
    return row as unknown as PricingConfigResult;
  }

  async setActivePricing(config: PricingConfigData, label?: string | null): Promise<string> {
    const row = await this.rpc("set_active_pricing_config", {
      p_config: config,
      p_label: label ?? null,
    });
    const code = this.errorCode(row);
    if (code) throw new StoreError(`set_active_pricing_config: ${code}`);
    return String(row.id ?? "");
  }

  // ── Plan management ────────────────────────────────────────────────

  async getUserPlan(userId: string): Promise<GetUserPlanResult> {
    const row = await this.rpc("get_user_plan", { p_user_id: userId });
    if (!row || Object.keys(row).length === 0) {
      return { userId, planId: null, planName: null, freeAllowance: ZERO, features: {} };
    }
    const code = this.errorCode(row);
    if (code) throw new StoreError(`get_user_plan: ${code}`);
    return {
      userId: String(row.user_id ?? userId),
      planId: (row.plan_id as string) ?? null,
      planName: (row.plan_name as string) ?? null,
      freeAllowance: dec(row.free_allowance),
      features: (row.features as Record<string, unknown>) ?? {},
    };
  }

  async checkFeature(userId: string, feature: string): Promise<CheckFeatureResult> {
    const plan = await this.getUserPlan(userId);
    const present = Object.prototype.hasOwnProperty.call(plan.features, feature);
    const value = present ? plan.features[feature] : null;
    return {
      userId,
      feature,
      value,
      // M6: presence-vs-truthiness — numeric 0 / "" count as present.
      hasFeature: present && value !== null && value !== undefined && value !== false,
    };
  }

  async setUserPlan(userId: string, planId: string): Promise<SetUserPlanResult> {
    const row = await this.rpc("set_user_plan", {
      p_user_id: userId,
      p_plan_key: planId,
    });
    const code = this.errorCode(row);
    if (code) throw new StoreError(`set_user_plan: ${code}`);
    return {
      userId: String(row.user_id ?? userId),
      planId: String(row.plan_id ?? planId),
    };
  }

  async checkAllowance(userId: string): Promise<AllowanceResult> {
    const row = await this.rpc("check_plan_allowance", { p_user_id: userId });
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

  async sweepExpiredCredits(dryRun = false): Promise<SweepResult> {
    const row = await this.rpc("expire_credits", {
      p_dry_run: dryRun,
    });
    const code = this.errorCode(row);
    if (code) throw new StoreError(`expire_credits: ${code}`);
    return {
      expiredCount: Number(row.expired_count ?? 0),
      expiredAmount: dec(row.expired_amount),
      dryRun,
    };
  }
}
