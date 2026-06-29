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
 * Parse a Postgres NUMERIC column into an exact `Decimal`. Postgres returns
 * NUMERIC as a *string* via `pg`, so this preserves full precision (contract
 * §1). `null`/`undefined` become the supplied fallback.
 */
function dec(value: unknown, fallback: Decimal = ZERO): Decimal {
  if (value === null || value === undefined) return fallback;
  if (value instanceof Decimal) return value;
  try {
    return new Decimal(typeof value === "string" ? value : String(value));
  } catch {
    return fallback;
  }
}

/** A money serialized for an SQL parameter: send as a decimal string. */
function decParam(value: Decimal): string {
  return value.toString();
}

/**
 * Minimal interface for a PG pool (real or mock).
 */
export interface PgPool {
  query(text: string, params?: unknown[]): Promise<{ rows: unknown[] }>;
  end(): Promise<void>;
}

export interface PgPoolConstructor {
  new (config: { connectionString: string }): PgPool;
}

/**
 * Credit store backed by a raw Postgres connection.
 *
 * Uses dependency injection for the PG pool constructor — supply a real ``pg.Pool``
 * for production, or a mock for tests.
 *
 * Args:
 *   databaseUrl: Postgres connection string.
 *   poolCtor: Optional PG Pool constructor (default: loads ``pg`` on first use).
 */
export class PostgresStore implements CreditStore {
  private databaseUrl: string;
  private poolCtor: PgPoolConstructor;
  private pool: PgPool | null = null;

  constructor(databaseUrl: string, poolCtor?: PgPoolConstructor) {
    this.databaseUrl = databaseUrl;
    this.poolCtor = poolCtor ?? null!; // lazy-loaded
  }

  private async getPool(): Promise<PgPool> {
    if (!this.pool) {
      const Pool = await this.getPoolCtor();
      this.pool = new Pool({ connectionString: this.databaseUrl });
    }
    return this.pool;
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
    if (this.pool) {
      await this.pool.end();
      this.pool = null;
    }
  }

  /**
   * Call a SQL function and return its result rows.
   *
   * ducto's RPCs come in two shapes:
   *   1. **Scalar JSONB** (`RETURNS JSONB`) — `pg` returns one row whose single
   *      column holds the parsed object. We unwrap that to `[object]`.
   *   2. **Set-returning** (`RETURNS TABLE/SETOF`) — many rows of named columns.
   *      We return them as-is.
   *
   * The previous `rows[0]` heuristic was fragile: a single-row set-returning
   * function whose first column happened to be an object would be misread. We
   * instead detect the JSONB-scalar case precisely: exactly one row with exactly
   * one column whose value is a non-array object. Lists therefore always return
   * all their rows.
   */
  private async callproc(name: string, params: unknown[]): Promise<unknown[]> {
    const placeholders = params.map((_, i) => `$${i + 1}`).join(", ");
    const rows = await this.query(`SELECT * FROM ${name}(${placeholders})`, params);
    if (rows.length === 1) {
      const row = rows[0] as Record<string, unknown>;
      const keys = Object.keys(row);
      if (keys.length === 1) {
        const v = row[keys[0]];
        if (v !== null && typeof v === "object" && !Array.isArray(v)) {
          // Scalar JSONB result: unwrap to the parsed object.
          return [v];
        }
      }
    }
    return rows;
  }

  async setup(_databaseUrl?: string | null): Promise<SetupResult> {
    // H17: do NOT silently report success for a no-op. ducto's schema is managed
    // as a set of ordered SQL migrations bundled with the Python package; this
    // store does not embed them. Surface that clearly instead of green-lighting
    // a missing schema (which would only fail later as missing-RPC errors).
    throw new StoreError(
      "PostgresStore.setup() does not run migrations. Apply the bundled SQL " +
        "migrations first — run `ducto migrate` via the Python CLI, or execute the " +
        "files in `python/src/ducto/sql/*.sql` (in filename order) against your " +
        "database. This store assumes the schema already exists.",
    );
  }

  async getBalance(userId: string): Promise<BalanceResult> {
    const rows = await this.callproc("get_credits_balance", [userId]);
    if (!rows || rows.length === 0) {
      return { userId, balance: ZERO, lifetimePurchased: ZERO };
    }
    const row = rows[0] as Record<string, unknown>;
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
    const rows = await this.callproc("credits_add", [
      userId,
      decParam(amount),
      type,
      JSON.stringify(meta),
    ]);
    const row = (rows?.[0] ?? {}) as Record<string, unknown>;
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
    const rows = await this.callproc("reserve_credits", [
      userId,
      decParam(amount),
      operationType,
      JSON.stringify(metadata ?? {}),
      decParam(minBalance),
    ]);

    if (!rows || rows.length === 0) {
      return {
        reservationId: "",
        userId,
        amount: ZERO,
        balance: ZERO,
        reservedTotal: ZERO,
        error: "no result",
      };
    }

    const row = (rows[0] as Record<string, unknown>) ?? {};
    if ("error" in row) {
      return {
        reservationId: "",
        userId,
        amount: ZERO,
        balance: dec(row.balance),
        reservedTotal: dec(row.reserved),
        error: String(row.error),
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

    const rows = await this.callproc("deduct_credits", [
      userId,
      reservationId,
      decParam(amount),
      JSON.stringify(meta),
    ]);

    if (!rows || rows.length === 0) {
      return {
        transactionId: "",
        userId,
        amount: amount.negated(),
        allowanceConsumed: ZERO,
        balanceAfter: ZERO,
        idempotent: false,
        capWarning: null,
        error: "no result",
      };
    }

    const row = (rows[0] as Record<string, unknown>) ?? {};
    if ("error" in row) {
      return {
        transactionId: "",
        userId,
        amount: amount.negated(),
        allowanceConsumed: ZERO,
        balanceAfter: dec(row.new_balance),
        idempotent: false,
        capWarning: null,
        error: String(row.error),
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

    const rows = await this.callproc("deduct_with_allowance", [
      userId,
      decParam(amount),
      idempotencyKey,
      decParam(minBalance),
      model,
      JSON.stringify(metadata ?? {}),
    ]);

    const row = (rows?.[0] ?? {}) as Record<string, unknown>;
    if ("error" in row && row.error) {
      // Map the SQL error envelope to DeductionResult.error (the manager maps
      // codes to typed exceptions). cap_reached / insufficient_credits /
      // invalid_amount all flow through here without throwing.
      return {
        transactionId: "",
        userId,
        amount: ZERO,
        allowanceConsumed: ZERO,
        balanceAfter: dec(row.balance_after),
        idempotent: false,
        capWarning: null,
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
    };
  }

  async getActivePricing(): Promise<PricingConfigResult | null> {
    const rows = await this.callproc("get_active_pricing_config", []);
    if (!rows || rows.length === 0) return null;
    const row = rows[0] as Record<string, unknown> | undefined;
    if (!row || !row.config) return null;
    return row as unknown as PricingConfigResult;
  }

  async setActivePricing(config: PricingConfigData, label?: string | null): Promise<string> {
    const rows = await this.callproc("set_active_pricing_config", [
      JSON.stringify(config),
      label ?? null,
    ]);
    const row = (rows?.[0] ?? {}) as Record<string, unknown>;
    return String(row.id ?? "");
  }

  // ── Plan management ────────────────────────────────────────────────

  async getUserPlan(userId: string): Promise<GetUserPlanResult> {
    const rows = await this.callproc("get_user_plan", [userId]);
    if (!rows || rows.length === 0) {
      return { userId, planId: null, planName: null, freeAllowance: ZERO, features: {} };
    }
    const row = rows[0] as Record<string, unknown>;
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
    const rows = await this.callproc("set_user_plan", [userId, planId]);
    const row = (rows?.[0] ?? {}) as Record<string, unknown>;
    return {
      userId: String(row.user_id ?? userId),
      planId: String(row.plan_id ?? planId),
    };
  }

  async checkAllowance(userId: string): Promise<AllowanceResult> {
    const rows = await this.callproc("check_plan_allowance", [userId]);
    if (!rows || rows.length === 0) {
      return { planId: "", allowanceRemaining: ZERO, periodStart: "", periodEnd: "" };
    }
    const row = rows[0] as Record<string, unknown>;
    return {
      planId: String(row.plan_id ?? ""),
      allowanceRemaining: dec(row.allowance_remaining),
      periodStart: String(row.period_start ?? ""),
      periodEnd: String(row.period_end ?? ""),
    };
  }

  async incrementUsageWindow(userId: string, planId: string, amount: Decimal): Promise<void> {
    await this.callproc("increment_usage_window", [userId, planId, decParam(amount)]);
  }

  // ── Spend caps and rate limiting ──────────────────────────────────────

  async checkSpendCap(
    userId: string,
    model?: string | null,
    amount?: Decimal,
  ): Promise<CapCheckResult> {
    const rows = await this.callproc("check_spend_cap", [
      userId,
      model ?? null,
      decParam(amount ?? ZERO),
    ]);
    if (!rows || rows.length === 0) {
      return { capped: false, currentSpend: ZERO, limit: ZERO, action: null };
    }
    const row = rows[0] as Record<string, unknown>;
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
    const rows = await this.callproc("refund_credits", [
      transactionId,
      amount != null ? decParam(amount) : null,
      reason ?? null,
      JSON.stringify(metadata ?? {}),
    ]);
    const row = (rows?.[0] ?? {}) as Record<string, unknown>;
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
    };
  }

  // ── Usage analytics ──────────────────────────────────────────────────

  async spendByUser(start: Date, end: Date): Promise<SpendByUserRow[]> {
    const rows = await this.callproc("spend_by_user", [start.toISOString(), end.toISOString()]);
    return (rows ?? []).map((r) => {
      const row = r as Record<string, unknown>;
      return {
        userId: String(row.user_id ?? ""),
        totalSpend: dec(row.total_spend),
        transactionCount: Number(row.transaction_count ?? 0),
      };
    });
  }

  async spendByModel(start: Date, end: Date): Promise<SpendByModelRow[]> {
    const rows = await this.callproc("spend_by_model", [start.toISOString(), end.toISOString()]);
    return (rows ?? []).map((r) => {
      const row = r as Record<string, unknown>;
      return {
        model: String(row.model ?? ""),
        totalSpend: dec(row.total_spend),
        transactionCount: Number(row.transaction_count ?? 0),
      };
    });
  }

  async topUsers(limit: number, start: Date, end: Date): Promise<TopUserRow[]> {
    const rows = await this.callproc("top_users", [limit, start.toISOString(), end.toISOString()]);
    return (rows ?? []).map((r) => {
      const row = r as Record<string, unknown>;
      return {
        userId: String(row.user_id ?? ""),
        totalSpend: dec(row.total_spend),
      };
    });
  }

  async dailySpend(start: Date, end: Date): Promise<DailySpendRow[]> {
    const rows = await this.callproc("daily_spend", [start.toISOString(), end.toISOString()]);
    return (rows ?? []).map((r) => {
      const row = r as Record<string, unknown>;
      return {
        date: String(row.date ?? ""),
        totalSpend: dec(row.total_spend),
        transactionCount: Number(row.transaction_count ?? 0),
      };
    });
  }

  // ── Transaction listing ─────────────────────────────────────────────

  async listUserTransactions(
    userId: string,
    options?: ListTransactionsOptions,
  ): Promise<PaginatedTransactions> {
    const rows = await this.callproc("list_user_transactions", [
      userId,
      options?.types ?? null,
      options?.fromDate?.toISOString() ?? null,
      options?.toDate?.toISOString() ?? null,
      options?.limit ?? 50,
      options?.offset ?? 0,
    ]);
    const items = (rows ?? []).map((r) => {
      const row = r as Record<string, unknown>;
      return {
        id: String(row.id ?? ""),
        userId: String(row.user_id ?? ""),
        amount: dec(row.amount),
        type: String(row.type ?? ""),
        referenceType: row.reference_type != null ? String(row.reference_type) : null,
        referenceId: row.reference_id != null ? String(row.reference_id) : null,
        metadata: (row.metadata as Record<string, unknown> | null) ?? null,
        createdAt: String(row.created_at ?? ""),
      };
    });
    const total =
      rows.length > 0 ? Number((rows[0] as Record<string, unknown>).total_count ?? 0) : 0;
    return { items, total };
  }

  async listUsageEvents(
    userId: string,
    options?: ListUsageEventsOptions,
  ): Promise<PaginatedTransactions> {
    const rows = await this.callproc("list_usage_events", [
      userId,
      options?.fromDate?.toISOString() ?? null,
      options?.toDate?.toISOString() ?? null,
      options?.limit ?? 50,
      options?.offset ?? 0,
    ]);
    const items = (rows ?? []).map((r) => {
      const row = r as Record<string, unknown>;
      return {
        id: String(row.id ?? ""),
        userId: String(row.user_id ?? ""),
        amount: dec(row.amount),
        type: String(row.type ?? ""),
        referenceType: row.reference_type != null ? String(row.reference_type) : null,
        referenceId: row.reference_id != null ? String(row.reference_id) : null,
        metadata: (row.metadata as Record<string, unknown> | null) ?? null,
        createdAt: String(row.created_at ?? ""),
      };
    });
    const total =
      rows.length > 0 ? Number((rows[0] as Record<string, unknown>).total_count ?? 0) : 0;
    return { items, total };
  }

  // ── Aggregate stats ────────────────────────────────────────────────

  async aggregateStats(start: Date, end: Date): Promise<AggregateStats> {
    const rows = await this.callproc("aggregate_stats", [start.toISOString(), end.toISOString()]);
    const row = (rows?.[0] ?? {}) as Record<string, unknown>;
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
    const rows = await this.callproc("create_team", [name, decParam(initialBalance)]);
    const row = (rows?.[0] ?? {}) as Record<string, unknown>;
    return {
      teamId: String(row.team_id ?? ""),
      name: String(row.name ?? name),
    };
  }

  async getTeamBalance(teamId: string): Promise<TeamBalanceResult> {
    const rows = await this.callproc("get_team_balance", [teamId]);
    if (!rows || rows.length === 0) {
      return { teamId, name: "", balance: ZERO, memberCount: 0 };
    }
    const row = rows[0] as Record<string, unknown>;
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
    const rows = await this.callproc("add_team_member", [
      teamId,
      userId,
      role,
      spendCap != null ? decParam(spendCap) : null,
    ]);
    const row = (rows?.[0] ?? {}) as Record<string, unknown>;
    return {
      teamId: String(row.team_id ?? teamId),
      userId: String(row.user_id ?? userId),
      role: String(row.role ?? role),
    };
  }

  async getTeamMembers(teamId: string): Promise<TeamMember[]> {
    const rows = await this.callproc("get_team_members", [teamId]);
    return (rows ?? []).map((r) => {
      const row = r as Record<string, unknown>;
      return {
        userId: String(row.user_id ?? ""),
        role: String(row.role ?? "member"),
        spendCap: row.spend_cap != null ? dec(row.spend_cap) : null,
        totalSpent: dec(row.total_spent),
      };
    });
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
    const rows = await this.callproc("deduct_team", [
      teamId,
      userId,
      decParam(amount),
      JSON.stringify(meta),
    ]);
    const row = (rows?.[0] ?? {}) as Record<string, unknown>;
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

  // ── Credit expiry ────────────────────────────────────────────────────

  async sweepExpiredCredits(dryRun = false): Promise<SweepResult> {
    const rows = await this.callproc("expire_credits", [dryRun]);
    const row = (rows?.[0] ?? {}) as Record<string, unknown>;
    return {
      expiredCount: Number(row.expired_count ?? 0),
      expiredAmount: dec(row.expired_amount),
      dryRun,
    };
  }
}
