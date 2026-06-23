import { randomUUID } from "crypto";
import type {
  AddCreditsResult,
  AllowanceResult,
  BalanceResult,
  CreditMetadata,
  DeductionResult,
  GetUserPlanResult,
  PlanDefinition,
  PricingConfigData,
  PricingConfigResult,
  ReserveResult,
  SetUserPlanResult,
  SetupResult,
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

  async setup(_databaseUrl?: string | null): Promise<SetupResult> {
    return {
      tablesCreated: [
        "001_credit_tables.sql",
        "002_credit_rpcs.sql",
        "003_pricing_config.sql",
        "004_user_plans.sql",
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
  ): Promise<AddCreditsResult> {
    const current = this.balances.get(userId) ?? 0;
    this.balances.set(userId, current + amount);

    const lifetimeAdd = type === "purchase" ? amount : 0;
    this.lifetime.set(userId, (this.lifetime.get(userId) ?? 0) + lifetimeAdd);

    const txId = randomUUID();
    this.transactions.push({ id: txId, userId, amount, type });

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
    _metadata?: CreditMetadata | null,
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

    const txId = randomUUID();
    this.transactions.push({
      id: txId,
      userId,
      amount: -amount,
      type: "usage",
      metadata: idempotencyKey ? { idempotencyKey } : undefined,
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
}
