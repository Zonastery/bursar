import type {
  AddCreditsResult,
  AddTeamMemberResult,
  AllowanceResult,
  BalanceResult,
  CapCheckResult,
  CreateTeamResult,
  CreditMetadata,
  DailySpendRow,
  DeductionResult,
  GetUserPlanResult,
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

  private async callproc(name: string, params: unknown[]): Promise<unknown[]> {
    const placeholders = params.map((_, i) => `$${i + 1}`).join(", ");
    return await this.query(`SELECT * FROM ${name}(${placeholders})`, params);
  }

  async setup(_databaseUrl?: string | null): Promise<SetupResult> {
    const result: SetupResult = { tablesCreated: [], rpcsCreated: [], errors: [], success: true };
    // Run bundled SQL migrations — in a real package these would be embedded files
    // Here we expose the setup API; actual migration files belong in the consuming project
    return result;
  }

  async getBalance(userId: string): Promise<BalanceResult> {
    const rows = await this.callproc("get_credits_balance", [userId]);
    if (!rows || rows.length === 0) {
      return { userId, balance: 0, lifetimePurchased: 0 };
    }
    const row = rows[0] as Record<string, unknown>;
    return {
      userId: String(row.user_id ?? userId),
      balance: Number(row.balance ?? 0),
      lifetimePurchased: Number(row.lifetime_purchased ?? 0),
    };
  }

  async addCredits(
    userId: string,
    amount: number,
    type = "adjustment",
    metadata?: CreditMetadata | null,
    expiresAt?: Date | null,
  ): Promise<AddCreditsResult> {
    const meta: Record<string, unknown> = { ...(metadata ?? {}) };
    if (expiresAt) {
      meta.expires_at = expiresAt instanceof Date ? expiresAt.toISOString() : String(expiresAt);
    }
    const rows = await this.callproc("credits_add", [userId, amount, type, JSON.stringify(meta)]);
    const row = (rows?.[0] ?? {}) as Record<string, unknown>;
    return {
      transactionId: String(row.id ?? ""),
      userId: String(row.user_id ?? userId),
      amount: Number(row.amount ?? amount),
      newBalance: Number(row.new_balance ?? 0),
      lifetimePurchased: Number(row.lifetime_purchased ?? 0),
    };
  }

  async reserveCredits(
    userId: string,
    amount: number,
    operationType: string,
    metadata?: CreditMetadata | null,
    minBalance = 5,
  ): Promise<ReserveResult> {
    const rows = await this.callproc("reserve_credits", [
      userId,
      amount,
      operationType,
      JSON.stringify(metadata ?? {}),
      minBalance,
    ]);

    if (!rows || rows.length === 0) {
      return {
        reservationId: "",
        userId,
        amount: 0,
        balance: 0,
        reservedTotal: 0,
        error: "no result",
      };
    }

    const row = (rows[0] as Record<string, unknown>) ?? {};
    if ("error" in row) {
      return {
        reservationId: "",
        userId,
        amount: 0,
        balance: 0,
        reservedTotal: 0,
        error: String(row.error),
      };
    }

    return {
      reservationId: String(row.reservation_id ?? ""),
      userId: String(row.user_id ?? userId),
      amount: Number(row.amount ?? 0),
      balance: Number(row.balance ?? 0),
      reservedTotal: Number(row.reserved ?? 0),
    };
  }

  async deductCredits(
    userId: string,
    reservationId: string,
    amount: number,
    idempotencyKey?: string | null,
    metadata?: CreditMetadata | null,
  ): Promise<DeductionResult> {
    const meta: Record<string, unknown> = { ...(metadata ?? {}) };
    if (idempotencyKey) meta.idempotency_key = idempotencyKey;

    const rows = await this.callproc("deduct_credits", [
      userId,
      reservationId,
      amount,
      JSON.stringify(meta),
    ]);

    if (!rows || rows.length === 0) {
      return {
        transactionId: "",
        userId,
        amount: -amount,
        balanceAfter: 0,
        idempotent: false,
        error: "no result",
      };
    }

    const row = (rows[0] as Record<string, unknown>) ?? {};
    if ("error" in row) {
      return {
        transactionId: "",
        userId,
        amount: -amount,
        balanceAfter: 0,
        idempotent: false,
        error: String(row.error),
      };
    }

    return {
      transactionId: String(row.id ?? ""),
      userId: String(row.user_id ?? userId),
      amount: Number(row.amount ?? -amount),
      balanceAfter: Number(row.new_balance ?? 0),
      idempotent: Boolean(row.idempotent),
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
      return { userId, planId: null, planName: null, freeAllowance: 0 };
    }
    const row = rows[0] as Record<string, unknown>;
    return {
      userId: String(row.user_id ?? userId),
      planId: (row.plan_id as string) ?? null,
      planName: (row.plan_name as string) ?? null,
      freeAllowance: Number(row.free_allowance ?? 0),
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
      return { planId: "", allowanceRemaining: 0, periodStart: "", periodEnd: "" };
    }
    const row = rows[0] as Record<string, unknown>;
    return {
      planId: String(row.plan_id ?? ""),
      allowanceRemaining: Number(row.allowance_remaining ?? 0),
      periodStart: String(row.period_start ?? ""),
      periodEnd: String(row.period_end ?? ""),
    };
  }

  async incrementUsageWindow(userId: string, planId: string, amount: number): Promise<void> {
    await this.callproc("increment_usage_window", [userId, planId, amount]);
  }

  // ── Spend caps and rate limiting ──────────────────────────────────────

  async checkSpendCap(
    userId: string,
    model?: string | null,
    amount?: number,
  ): Promise<CapCheckResult> {
    const rows = await this.callproc("check_spend_cap", [userId, model ?? null, amount ?? 0]);
    if (!rows || rows.length === 0) {
      return { capped: false, currentSpend: 0, limit: 0, action: null };
    }
    const row = rows[0] as Record<string, unknown>;
    return {
      capped: Boolean(row.capped),
      currentSpend: Number(row.current_spend ?? 0),
      limit: Number(row.cap_limit ?? 0),
      action: (row.action as CapCheckResult["action"]) ?? null,
      model: row.model ? String(row.model) : undefined,
    };
  }

  // ── Refunds ──────────────────────────────────────────────────────────

  async refundCredits(
    transactionId: string,
    amount?: number,
    reason?: string,
    metadata?: CreditMetadata | null,
  ): Promise<RefundResult> {
    const rows = await this.callproc("refund_credits", [
      transactionId,
      amount ?? null,
      reason ?? null,
      JSON.stringify(metadata ?? {}),
    ]);
    const row = (rows?.[0] ?? {}) as Record<string, unknown>;
    if ("error" in row && row.error) {
      return {
        refundTransactionId: "",
        originalTransactionId: transactionId,
        userId: String(row.user_id ?? ""),
        amount: 0,
        newBalance: Number(row.new_balance ?? 0),
        error: String(row.error),
      };
    }
    return {
      refundTransactionId: String(row.refund_transaction_id ?? ""),
      originalTransactionId: transactionId,
      userId: String(row.user_id ?? ""),
      amount: Number(row.amount ?? 0),
      newBalance: Number(row.new_balance ?? 0),
    };
  }

  // ── Usage analytics ──────────────────────────────────────────────────

  async spendByUser(start: Date, end: Date): Promise<SpendByUserRow[]> {
    const rows = await this.callproc("spend_by_user", [start.toISOString(), end.toISOString()]);
    return (rows ?? []).map((r) => {
      const row = r as Record<string, unknown>;
      return {
        userId: String(row.user_id ?? ""),
        totalSpend: Number(row.total_spend ?? 0),
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
        totalSpend: Number(row.total_spend ?? 0),
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
        totalSpend: Number(row.total_spend ?? 0),
      };
    });
  }

  async dailySpend(start: Date, end: Date): Promise<DailySpendRow[]> {
    const rows = await this.callproc("daily_spend", [start.toISOString(), end.toISOString()]);
    return (rows ?? []).map((r) => {
      const row = r as Record<string, unknown>;
      return {
        date: String(row.date ?? ""),
        totalSpend: Number(row.total_spend ?? 0),
        transactionCount: Number(row.transaction_count ?? 0),
      };
    });
  }

  // ── Team/shared balance pools ────────────────────────────────────────

  async createTeam(name: string, initialBalance = 0): Promise<CreateTeamResult> {
    const rows = await this.callproc("create_team", [name, initialBalance]);
    const row = (rows?.[0] ?? {}) as Record<string, unknown>;
    return {
      teamId: String(row.team_id ?? ""),
      name: String(row.name ?? name),
    };
  }

  async getTeamBalance(teamId: string): Promise<TeamBalanceResult> {
    const rows = await this.callproc("get_team_balance", [teamId]);
    if (!rows || rows.length === 0) {
      return { teamId, name: "", balance: 0, memberCount: 0 };
    }
    const row = rows[0] as Record<string, unknown>;
    if ("error" in row && row.error) {
      return { teamId, name: "", balance: 0, memberCount: 0 };
    }
    return {
      teamId: String(row.team_id ?? teamId),
      name: String(row.name ?? ""),
      balance: Number(row.balance ?? 0),
      memberCount: Number(row.member_count ?? 0),
    };
  }

  async addTeamMember(
    teamId: string,
    userId: string,
    role = "member",
    spendCap?: number | null,
  ): Promise<AddTeamMemberResult> {
    const rows = await this.callproc("add_team_member", [teamId, userId, role, spendCap ?? null]);
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
        spendCap: (row.spend_cap as number | null) ?? null,
        totalSpent: Number(row.total_spent ?? 0),
      };
    });
  }

  async deductTeam(
    teamId: string,
    userId: string,
    amount: number,
    metadata?: CreditMetadata | null,
  ): Promise<TeamDeductionResult> {
    const rows = await this.callproc("deduct_team", [
      teamId,
      userId,
      amount,
      JSON.stringify(metadata ?? {}),
    ]);
    const row = (rows?.[0] ?? {}) as Record<string, unknown>;
    if ("error" in row && row.error) {
      return {
        transactionId: "",
        teamId,
        userId,
        amount: 0,
        teamBalanceAfter: Number(row.team_balance_after ?? 0),
        error: String(row.error),
      };
    }
    return {
      transactionId: String(row.transaction_id ?? ""),
      teamId: String(row.team_id ?? teamId),
      userId: String(row.user_id ?? userId),
      amount: Number(row.amount ?? -amount),
      teamBalanceAfter: Number(row.team_balance_after ?? 0),
    };
  }

  // ── Credit expiry ────────────────────────────────────────────────────

  async sweepExpiredCredits(dryRun = false): Promise<SweepResult> {
    const rows = await this.callproc("expire_credits", [dryRun]);
    const row = (rows?.[0] ?? {}) as Record<string, unknown>;
    return {
      expiredCount: Number(row.expired_count ?? 0),
      expiredAmount: Number(row.expired_amount ?? 0),
      dryRun,
    };
  }
}
