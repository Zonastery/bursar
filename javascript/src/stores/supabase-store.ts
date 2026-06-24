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
 * Credit store backed by Supabase RPCs via raw HTTP (fetch).
 *
 * No supabase-js dependency — makes direct POST requests to the Supabase REST API.
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

  private async rpc(fn: string, params: Record<string, unknown>): Promise<Record<string, unknown>> {
    const resp = await fetch(`${this.url}/rest/v1/rpc/${fn}`, {
      method: "POST",
      headers: {
        apikey: this.key,
        authorization: `Bearer ${this.key}`,
        "content-type": "application/json",
      },
      body: JSON.stringify(params),
    });
    if (!resp.ok) {
      const text = await resp.text();
      throw new Error(`Supabase RPC ${fn} failed (${resp.status}): ${text}`);
    }
    return resp.json() as Promise<Record<string, unknown>>;
  }

  private async rpcAll(
    fn: string,
    params: Record<string, unknown>,
  ): Promise<Record<string, unknown>[]> {
    const resp = await fetch(`${this.url}/rest/v1/rpc/${fn}`, {
      method: "POST",
      headers: {
        apikey: this.key,
        authorization: `Bearer ${this.key}`,
        "content-type": "application/json",
      },
      body: JSON.stringify(params),
    });
    if (!resp.ok) {
      const text = await resp.text();
      throw new Error(`Supabase RPC ${fn} failed (${resp.status}): ${text}`);
    }
    const data = await resp.json();
    if (data === null || data === undefined) return [];
    if (!Array.isArray(data)) return [data];
    return data.filter((r: unknown) => r != null);
  }

  async setup(_databaseUrl?: string | null): Promise<SetupResult> {
    throw new Error(
      "HttpxSupabaseStore.setup() requires a database_url — use PostgresStore.setup() instead",
    );
  }

  async getBalance(userId: string): Promise<BalanceResult> {
    const row = await this.rpc("get_credits_balance", { p_user_id: userId });
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
    const row = await this.rpc("credits_add", {
      p_user_id: userId,
      p_amount: amount,
      p_type: type,
      p_metadata: meta,
    });
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
    const row = await this.rpc("reserve_credits", {
      p_user_id: userId,
      p_amount: amount,
      p_operation_type: operationType,
      p_metadata: metadata ?? {},
      p_min_balance: minBalance,
    });

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
      reservationId: String(row.reservation_id),
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

    const row = await this.rpc("deduct_credits", {
      p_user_id: userId,
      p_reservation_id: reservationId,
      p_amount: amount,
      p_metadata: meta,
    });

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
      transactionId: String(row.id),
      userId: String(row.user_id ?? userId),
      amount: Number(row.amount ?? -amount),
      balanceAfter: Number(row.new_balance ?? 0),
      idempotent: Boolean(row.idempotent),
    };
  }

  async getActivePricing(): Promise<PricingConfigResult | null> {
    const row = await this.rpc("get_active_pricing_config", {});
    if (!row || Object.keys(row).length === 0) return null;
    return row as unknown as PricingConfigResult;
  }

  async setActivePricing(config: PricingConfigData, label?: string | null): Promise<string> {
    const row = await this.rpc("set_active_pricing_config", {
      p_config: config,
      p_label: label ?? null,
    });
    return String(row.id ?? "");
  }

  // ── Plan management ────────────────────────────────────────────────

  async getUserPlan(userId: string): Promise<GetUserPlanResult> {
    const row = await this.rpc("get_user_plan", { p_user_id: userId });
    if (!row || Object.keys(row).length === 0) {
      return { userId, planId: null, planName: null, freeAllowance: 0 };
    }
    return {
      userId: String(row.user_id ?? userId),
      planId: (row.plan_id as string) ?? null,
      planName: (row.plan_name as string) ?? null,
      freeAllowance: Number(row.free_allowance ?? 0),
    };
  }

  async setUserPlan(userId: string, planId: string): Promise<SetUserPlanResult> {
    const row = await this.rpc("set_user_plan", {
      p_user_id: userId,
      p_plan_id: planId,
    });
    return {
      userId: String(row.user_id ?? userId),
      planId: String(row.plan_id ?? planId),
    };
  }

  async checkAllowance(userId: string): Promise<AllowanceResult> {
    const row = await this.rpc("check_plan_allowance", { p_user_id: userId });
    if (!row || Object.keys(row).length === 0) {
      return { planId: "", allowanceRemaining: 0, periodStart: "", periodEnd: "" };
    }
    return {
      planId: String(row.plan_id ?? ""),
      allowanceRemaining: Number(row.allowance_remaining ?? 0),
      periodStart: String(row.period_start ?? ""),
      periodEnd: String(row.period_end ?? ""),
    };
  }

  async incrementUsageWindow(userId: string, planId: string, amount: number): Promise<void> {
    await this.rpc("increment_usage_window", {
      p_user_id: userId,
      p_plan_id: planId,
      p_amount: amount,
    });
  }

  // ── Spend caps and rate limiting ──────────────────────────────────────

  async checkSpendCap(
    userId: string,
    model?: string | null,
    amount?: number,
  ): Promise<CapCheckResult> {
    const row = await this.rpc("check_spend_cap", {
      p_user_id: userId,
      p_model: model ?? null,
      p_amount: amount ?? 0,
    });
    if (!row || Object.keys(row).length === 0) {
      return { capped: false, currentSpend: 0, limit: 0, action: null };
    }
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
    const row = await this.rpc("refund_credits", {
      p_transaction_id: transactionId,
      p_amount: amount ?? null,
      p_reason: reason ?? null,
      p_metadata: metadata ?? {},
    });
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
    const rows = await this.rpcAll("spend_by_user", {
      p_start: start.toISOString(),
      p_end: end.toISOString(),
    });
    return rows.map((row) => ({
      userId: String(row.user_id ?? ""),
      totalSpend: Number(row.total_spend ?? 0),
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
      totalSpend: Number(row.total_spend ?? 0),
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
      totalSpend: Number(row.total_spend ?? 0),
    }));
  }

  async dailySpend(start: Date, end: Date): Promise<DailySpendRow[]> {
    const rows = await this.rpcAll("daily_spend", {
      p_start: start.toISOString(),
      p_end: end.toISOString(),
    });
    return rows.map((row) => ({
      date: String(row.date ?? ""),
      totalSpend: Number(row.total_spend ?? 0),
      transactionCount: Number(row.transaction_count ?? 0),
    }));
  }

  // ── Team/shared balance pools ────────────────────────────────────────

  async createTeam(name: string, initialBalance = 0): Promise<CreateTeamResult> {
    const row = await this.rpc("create_team", { p_name: name, p_initial_balance: initialBalance });
    return {
      teamId: String(row.team_id ?? ""),
      name: String(row.name ?? name),
    };
  }

  async getTeamBalance(teamId: string): Promise<TeamBalanceResult> {
    const row = await this.rpc("get_team_balance", { p_team_id: teamId });
    if (!row || Object.keys(row).length === 0 || ("error" in row && row.error)) {
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
    const row = await this.rpc("add_team_member", {
      p_team_id: teamId,
      p_user_id: userId,
      p_role: role,
      p_spend_cap: spendCap ?? null,
    });
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
      spendCap: (row.spend_cap as number | null) ?? null,
      totalSpent: Number(row.total_spent ?? 0),
    }));
  }

  async deductTeam(
    teamId: string,
    userId: string,
    amount: number,
    metadata?: CreditMetadata | null,
  ): Promise<TeamDeductionResult> {
    const row = await this.rpc("deduct_team", {
      p_team_id: teamId,
      p_user_id: userId,
      p_amount: amount,
      p_metadata: metadata ?? {},
    });
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
    const row = await this.rpc("expire_credits", {
      p_dry_run: dryRun,
    });
    return {
      expiredCount: Number(row.expired_count ?? 0),
      expiredAmount: Number(row.expired_amount ?? 0),
      dryRun,
    };
  }
}
