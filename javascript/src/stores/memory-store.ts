import { randomUUID } from "crypto";
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
  PlanDefinition,
  PricingConfigData,
  PricingConfigResult,
  RefundResult,
  ReserveResult,
  SetUserPlanResult,
  SetupResult,
  SpendByModelRow,
  SpendByUserRow,
  SpendCap,
  SweepResult,
  TeamBalanceResult,
  TeamDeductionResult,
  TeamMember,
  TopUserRow,
} from "../types.js";
import type { CreditStore } from "./credit-store.js";

interface TransactionRecord {
  id: string;
  userId: string;
  amount: number;
  type: string;
  metadata?: Record<string, unknown>;
  referenceType?: string | null;
  referenceId?: string | null;
  expiresAt?: string | null;
  createdAt: string;
}

interface ReservationRecord {
  id: string;
  userId: string;
  amount: number;
  operationType: string;
  metadata?: Record<string, unknown>;
}

/**
 * Credit store backed by in-memory dicts.
 * Zero dependencies. Useful for unit testing and local development.
 */
export class MemoryStore implements CreditStore {
  private balances = new Map<string, number>();
  private lifetime = new Map<string, number>();
  private transactions: TransactionRecord[] = [];
  private reservations = new Map<string, ReservationRecord>();
  private pricingConfig: PricingConfigData | null = null;
  private pricingVersion = 0;
  private planDefinitions = new Map<string, PlanDefinition>();
  private userPlanMap = new Map<string, string>();
  private usageWindows: Array<{
    userId: string;
    planId: string;
    billingPeriod: string;
    usage: number;
  }> = [];
  private spendCaps: SpendCap[] = [];
  private teams = new Map<
    string,
    { id: string; name: string; balance: number; memberCount: number; createdAt: string }
  >();
  private teamMembers = new Map<
    string,
    Map<string, { userId: string; role: string; spendCap: number | null; totalSpent: number }>
  >();

  async setup(_databaseUrl?: string | null): Promise<SetupResult> {
    return {
      tablesCreated: [
        "001_credit_tables.sql",
        "002_credit_rpcs.sql",
        "003_pricing_config.sql",
        "004_user_plans.sql",
        "005_credit_refunds.sql",
        "006_credit_expiry.sql",
        "007_usage_analytics.sql",
        "008_team_balances.sql",
        "009_spend_caps.sql",
      ],
      rpcsCreated: [],
      errors: [],
      success: true,
    };
  }

  async getBalance(userId: string): Promise<BalanceResult> {
    return {
      userId,
      balance: this.balances.get(userId) ?? 0,
      lifetimePurchased: this.lifetime.get(userId) ?? 0,
    };
  }

  async addCredits(
    userId: string,
    amount: number,
    type = "adjustment",
    _metadata?: CreditMetadata | null,
    expiresAt?: Date | null,
  ): Promise<AddCreditsResult> {
    const current = this.balances.get(userId) ?? 0;
    this.balances.set(userId, current + amount);

    const lifetimeAdd = type === "purchase" ? amount : 0;
    this.lifetime.set(userId, (this.lifetime.get(userId) ?? 0) + lifetimeAdd);

    const txId = randomUUID();
    const tx: TransactionRecord = {
      id: txId,
      userId,
      amount,
      type,
      createdAt: new Date().toISOString(),
    };
    if (expiresAt) {
      tx.expiresAt = expiresAt instanceof Date ? expiresAt.toISOString() : String(expiresAt);
    }
    this.transactions.push(tx);

    return {
      transactionId: txId,
      userId,
      amount,
      newBalance: current + amount,
      lifetimePurchased: this.lifetime.get(userId) ?? 0,
    };
  }

  async reserveCredits(
    userId: string,
    amount: number,
    operationType: string,
    _metadata?: CreditMetadata | null,
    minBalance = 5,
  ): Promise<ReserveResult> {
    const balance = this.balances.get(userId) ?? 0;

    let reservedTotal = 0;
    for (const r of this.reservations.values()) {
      if (r.userId === userId) reservedTotal += r.amount;
    }

    const available = balance - reservedTotal;
    if (available - amount < minBalance) {
      return {
        reservationId: "",
        userId,
        amount,
        balance,
        reservedTotal,
        error: "insufficient_credits",
      };
    }

    const rid = randomUUID();
    this.reservations.set(rid, { id: rid, userId, amount, operationType });
    return { reservationId: rid, userId, amount, balance, reservedTotal: reservedTotal + amount };
  }

  async deductCredits(
    userId: string,
    reservationId: string,
    amount: number,
    idempotencyKey?: string | null,
    metadata?: CreditMetadata | null,
  ): Promise<DeductionResult> {
    if (idempotencyKey) {
      const existing = this.transactions.find(
        (t) => t.metadata && t.metadata["idempotencyKey"] === idempotencyKey,
      );
      if (existing) {
        return {
          transactionId: existing.id,
          userId: existing.userId,
          amount: existing.amount,
          balanceAfter: this.balances.get(userId) ?? 0,
          idempotent: true,
        };
      }
    }

    const current = this.balances.get(userId) ?? 0;
    if (current < amount) {
      return {
        transactionId: "",
        userId,
        amount,
        balanceAfter: current,
        idempotent: false,
        error: "insufficient_credits",
      };
    }

    this.balances.set(userId, current - amount);
    this.reservations.delete(reservationId);

    const txMeta: Record<string, unknown> = {};
    if (metadata) {
      for (const [k, v] of Object.entries(metadata)) {
        if (v != null) txMeta[k] = v;
      }
    }
    if (idempotencyKey) txMeta["idempotencyKey"] = idempotencyKey;

    const txId = randomUUID();
    this.transactions.push({
      id: txId,
      userId,
      amount: -amount,
      type: "usage",
      metadata: txMeta,
      createdAt: new Date().toISOString(),
    });

    return {
      transactionId: txId,
      userId,
      amount: -amount,
      balanceAfter: current - amount,
      idempotent: false,
    };
  }

  async getActivePricing(): Promise<PricingConfigResult | null> {
    if (!this.pricingConfig) return null;
    return {
      id: randomUUID(),
      config: this.pricingConfig,
      version: this.pricingVersion,
    };
  }

  async setActivePricing(config: PricingConfigData, _label?: string | null): Promise<string> {
    this.pricingConfig = config;
    this.pricingVersion += 1;
    // Extract plan definitions from v2 config
    if ("plans" in config && config.plans) {
      for (const planData of Object.values(config.plans)) {
        const plan = planData as PlanDefinition;
        this.planDefinitions.set(plan.id, plan);
      }
    }
    return randomUUID();
  }

  // ── Plan management ────────────────────────────────────────────────

  async getUserPlan(userId: string): Promise<GetUserPlanResult> {
    const planId = this.userPlanMap.get(userId) ?? null;
    const planDef = planId ? this.planDefinitions.get(planId) : null;
    return {
      userId,
      planId,
      planName: planDef?.name ?? null,
      freeAllowance: planDef?.freeAllowance ?? 0,
    };
  }

  async setUserPlan(userId: string, planId: string): Promise<SetUserPlanResult> {
    this.userPlanMap.set(userId, planId);
    return { userId, planId };
  }

  async checkAllowance(userId: string): Promise<AllowanceResult> {
    const planId = this.userPlanMap.get(userId);
    if (!planId) {
      return { planId: "", allowanceRemaining: 0, periodStart: "", periodEnd: "" };
    }
    const planDef = this.planDefinitions.get(planId);
    if (!planDef) {
      return { planId: "", allowanceRemaining: 0, periodStart: "", periodEnd: "" };
    }
    const now = new Date();
    const periodStart = new Date(now.getFullYear(), now.getMonth(), 1);
    const periodEnd = new Date(now.getFullYear(), now.getMonth() + 1, 0);
    const billingPeriod = periodStart.toISOString().slice(0, 10);
    const usage = this.usageWindows
      .filter(
        (w) => w.userId === userId && w.planId === planId && w.billingPeriod === billingPeriod,
      )
      .reduce((sum, w) => sum + w.usage, 0);
    return {
      planId,
      allowanceRemaining: Math.max(planDef.freeAllowance - usage, 0),
      periodStart: periodStart.toISOString(),
      periodEnd: periodEnd.toISOString(),
    };
  }

  async incrementUsageWindow(userId: string, planId: string, amount: number): Promise<void> {
    const now = new Date();
    const billingPeriod = new Date(now.getFullYear(), now.getMonth(), 1).toISOString().slice(0, 10);
    const existing = this.usageWindows.find(
      (w) => w.userId === userId && w.planId === planId && w.billingPeriod === billingPeriod,
    );
    if (existing) {
      existing.usage += amount;
    } else {
      this.usageWindows.push({ userId, planId, billingPeriod, usage: amount });
    }
  }

  // ── Refunds ──────────────────────────────────────────────────────────

  async refundCredits(
    transactionId: string,
    amount?: number,
    reason?: string,
    _metadata?: CreditMetadata | null,
  ): Promise<RefundResult> {
    // Find original transaction
    const origTx = this.transactions.find((t) => t.id === transactionId);
    if (!origTx) {
      return {
        refundTransactionId: "",
        originalTransactionId: transactionId,
        userId: "",
        amount: 0,
        newBalance: 0,
        error: "transaction_not_found",
      };
    }

    // Check for duplicate refund
    const isRefunded = this.transactions.some(
      (t) => t.type === "refund" && t.referenceId === transactionId,
    );
    if (isRefunded) {
      return {
        refundTransactionId: "",
        originalTransactionId: transactionId,
        userId: origTx.userId,
        amount: 0,
        newBalance: this.balances.get(origTx.userId) ?? 0,
        error: "already_refunded",
      };
    }

    const refundAmount = amount ?? Math.abs(origTx.amount);
    const maxRefund = Math.abs(origTx.amount);
    const actualRefund = Math.min(refundAmount, maxRefund);

    // Restore balance
    const current = this.balances.get(origTx.userId) ?? 0;
    this.balances.set(origTx.userId, current + actualRefund);

    const txId = randomUUID();
    this.transactions.push({
      id: txId,
      userId: origTx.userId,
      amount: actualRefund,
      type: "refund",
      referenceType: reason ?? null,
      referenceId: transactionId as string,
      metadata: reason ? { reason } : undefined,
      createdAt: new Date().toISOString(),
    });

    return {
      refundTransactionId: txId,
      originalTransactionId: transactionId,
      userId: origTx.userId,
      amount: actualRefund,
      newBalance: current + actualRefund,
    };
  }

  // ── Credit expiry ─────────────────────────────────────────────────────

  async sweepExpiredCredits(dryRun = false): Promise<SweepResult> {
    const now = new Date();
    const expiredByUser = new Map<string, number>();
    const expiredTxs: TransactionRecord[] = [];

    // Find all expired grant transactions
    for (const tx of this.transactions) {
      if (tx.expiresAt && (tx.type === "purchase" || tx.type === "adjustment")) {
        if (new Date(tx.expiresAt) <= now) {
          const current = expiredByUser.get(tx.userId) ?? 0;
          expiredByUser.set(tx.userId, current + tx.amount);
          expiredTxs.push(tx);
        }
      }
    }

    let expiredCount = 0;
    let expiredAmount = 0;

    for (const [userId, totalExpired] of expiredByUser) {
      const currentBalance = this.balances.get(userId) ?? 0;
      const toExpire = Math.min(totalExpired, currentBalance);

      if (toExpire > 0) {
        expiredCount++;
        expiredAmount += toExpire;

        if (!dryRun) {
          this.balances.set(userId, currentBalance - toExpire);

          // Null out expiresAt on swept grants to prevent re-sweeping
          for (const et of expiredTxs) {
            if (et.userId === userId) {
              et.expiresAt = null;
            }
          }

          const txId = randomUUID();
          this.transactions.push({
            id: txId,
            userId,
            amount: -toExpire,
            type: "adjustment",
            metadata: { reason: "credit_expired", expiredAmount: toExpire },
            createdAt: new Date().toISOString(),
          });
        }
      }
    }

    return { expiredCount, expiredAmount, dryRun };
  }

  // ── Usage analytics ──────────────────────────────────────────────────

  /** Filter transactions to usage records in the time window. */
  private _usageInWindow(start: Date, end: Date): TransactionRecord[] {
    return this.transactions.filter(
      (t) =>
        t.type === "usage" &&
        t.amount < 0 &&
        new Date(t.createdAt) >= start &&
        new Date(t.createdAt) <= end,
    );
  }

  async spendByUser(start: Date, end: Date): Promise<SpendByUserRow[]> {
    const usage = this._usageInWindow(start, end);
    const byUser = new Map<string, { total: number; count: number }>();
    for (const t of usage) {
      const entry = byUser.get(t.userId) ?? { total: 0, count: 0 };
      entry.total += Math.abs(t.amount);
      entry.count++;
      byUser.set(t.userId, entry);
    }
    return Array.from(byUser.entries()).map(([userId, { total, count }]) => ({
      userId,
      totalSpend: total,
      transactionCount: count,
    }));
  }

  async spendByModel(start: Date, end: Date): Promise<SpendByModelRow[]> {
    const usage = this._usageInWindow(start, end);
    const byModel = new Map<string, { total: number; count: number }>();
    for (const t of usage) {
      const model = (t.metadata?.model as string) ?? "unknown";
      const entry = byModel.get(model) ?? { total: 0, count: 0 };
      entry.total += Math.abs(t.amount);
      entry.count++;
      byModel.set(model, entry);
    }
    return Array.from(byModel.entries()).map(([model, { total, count }]) => ({
      model,
      totalSpend: total,
      transactionCount: count,
    }));
  }

  async topUsers(limit: number, start: Date, end: Date): Promise<TopUserRow[]> {
    const byUser = await this.spendByUser(start, end);
    return byUser.sort((a, b) => b.totalSpend - a.totalSpend).slice(0, limit);
  }

  // ── Spend caps and rate limiting ─────────────────────────────────────

  /** Configure a spend cap (MemoryStore-only helper for testing). */
  setSpendCap(cap: SpendCap): void {
    this.spendCaps.push(cap);
  }

  async checkSpendCap(
    userId: string,
    model?: string | null,
    amount?: number,
  ): Promise<CapCheckResult> {
    const userCaps = this.spendCaps.filter((c) => c.userId === userId);
    if (userCaps.length === 0) {
      return { capped: false, currentSpend: 0, limit: 0, action: null };
    }

    const now = new Date();
    const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const monthStart = new Date(now.getFullYear(), now.getMonth(), 1);

    // Helper: compute spend in a time window
    const spendInWindow = (windowStart: Date, capModel?: string | null): number => {
      return this.transactions
        .filter((t) => {
          if (t.userId !== userId) return false;
          if (t.type !== "usage" && t.type !== "team_usage") return false;
          if (t.amount >= 0) return false;
          if (capModel != null && t.metadata?.model !== capModel) return false;
          return new Date(t.createdAt) >= windowStart;
        })
        .reduce((sum, t) => sum + Math.abs(t.amount), 0);
    };

    // Check deny caps first — most restrictive
    for (const cap of userCaps) {
      if (cap.model && cap.model !== model) continue;
      if (cap.action !== "deny") continue;
      const windowStart = cap.type === "daily" ? todayStart : monthStart;
      const currentSpend = spendInWindow(windowStart, cap.model);
      if (currentSpend + (amount ?? 0) > cap.limit) {
        return {
          capped: true,
          currentSpend,
          limit: cap.limit,
          action: "deny",
          model: cap.model,
        };
      }
    }

    // Check warn/notify caps
    for (const cap of userCaps) {
      if (cap.model && cap.model !== model) continue;
      if (cap.action === "deny") continue;
      const windowStart = cap.type === "daily" ? todayStart : monthStart;
      const currentSpend = spendInWindow(windowStart, cap.model);
      if (currentSpend + (amount ?? 0) > cap.limit) {
        return {
          capped: false,
          currentSpend,
          limit: cap.limit,
          action: cap.action,
          model: cap.model,
        };
      }
    }

    return { capped: false, currentSpend: 0, limit: 0, action: null };
  }

  // ── Team/shared balance pools ────────────────────────────────────────

  async createTeam(name: string, initialBalance = 0): Promise<CreateTeamResult> {
    const teamId = randomUUID();
    this.teams.set(teamId, {
      id: teamId,
      name,
      balance: initialBalance,
      memberCount: 0,
      createdAt: new Date().toISOString(),
    });
    this.teamMembers.set(teamId, new Map());
    return { teamId, name };
  }

  async getTeamBalance(teamId: string): Promise<TeamBalanceResult> {
    const team = this.teams.get(teamId);
    if (!team) {
      return { teamId, name: "", balance: 0, memberCount: 0 };
    }
    return {
      teamId: team.id,
      name: team.name,
      balance: team.balance,
      memberCount: team.memberCount,
    };
  }

  async addTeamMember(
    teamId: string,
    userId: string,
    role = "member",
    spendCap?: number | null,
  ): Promise<AddTeamMemberResult> {
    const members = this.teamMembers.get(teamId);
    if (!members) {
      return { teamId, userId, role: "" };
    }
    members.set(userId, { userId, role, spendCap: spendCap ?? null, totalSpent: 0 });
    const team = this.teams.get(teamId);
    if (team) {
      team.memberCount = members.size;
    }
    return { teamId, userId, role };
  }

  async getTeamMembers(teamId: string): Promise<TeamMember[]> {
    const members = this.teamMembers.get(teamId);
    if (!members) return [];
    return Array.from(members.values());
  }

  async deductTeam(
    teamId: string,
    userId: string,
    amount: number,
    metadata?: CreditMetadata | null,
  ): Promise<TeamDeductionResult> {
    const team = this.teams.get(teamId);
    if (!team) {
      return {
        transactionId: "",
        teamId,
        userId,
        amount: 0,
        teamBalanceAfter: 0,
        error: "team_not_found",
      };
    }
    const members = this.teamMembers.get(teamId);
    const member = members?.get(userId);
    if (!member) {
      return {
        transactionId: "",
        teamId,
        userId,
        amount: 0,
        teamBalanceAfter: team.balance,
        error: "user_not_in_team",
      };
    }

    // Enforce spend cap
    if (member.spendCap != null && member.totalSpent + amount > member.spendCap) {
      return {
        transactionId: "",
        teamId,
        userId,
        amount: 0,
        teamBalanceAfter: team.balance,
        error: "spend_cap_exceeded",
      };
    }

    if (team.balance < amount) {
      return {
        transactionId: "",
        teamId,
        userId,
        amount: 0,
        teamBalanceAfter: team.balance,
        error: "insufficient_team_balance",
      };
    }

    team.balance -= amount;
    member.totalSpent += amount;

    const txId = randomUUID();
    this.transactions.push({
      id: txId,
      userId,
      amount: -amount,
      type: "team_usage",
      metadata: { ...((metadata as Record<string, unknown>) ?? {}), teamId },
      createdAt: new Date().toISOString(),
    });

    return { transactionId: txId, teamId, userId, amount: -amount, teamBalanceAfter: team.balance };
  }

  async dailySpend(start: Date, end: Date): Promise<DailySpendRow[]> {
    const usage = this._usageInWindow(start, end);
    const byDay = new Map<string, { total: number; count: number }>();
    for (const t of usage) {
      const date = new Date(t.createdAt).toISOString().slice(0, 10);
      const entry = byDay.get(date) ?? { total: 0, count: 0 };
      entry.total += Math.abs(t.amount);
      entry.count++;
      byDay.set(date, entry);
    }
    return Array.from(byDay.entries())
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([date, { total, count }]) => ({ date, totalSpend: total, transactionCount: count }));
  }
}
