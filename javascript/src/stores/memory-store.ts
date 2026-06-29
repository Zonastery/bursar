import { randomUUID } from "crypto";
import Decimal from "decimal.js";
import { quantizeMoney } from "../expr.js";
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

const ZERO = new Decimal(0);

/** Coerce a presence-or-truthiness feature value per contract §5 (M6). */
function featurePresent(value: unknown): boolean {
  // Identity form: numeric 0 / "" count as present. Matches Python
  // `value is not None and value is not False`. Do NOT use Boolean(value).
  return value !== null && value !== undefined && value !== false;
}

interface TransactionRecord {
  id: string;
  userId: string;
  amount: Decimal;
  type: string;
  metadata?: Record<string, unknown>;
  referenceType?: string | null;
  referenceId?: string | null;
  expiresAt?: Date | null;
  /** Timestamp at which an expired grant was swept (H4: prevents re-sweep). */
  sweptAt?: Date | null;
  createdAt: Date;
}

interface ReservationRecord {
  id: string;
  userId: string;
  amount: Decimal;
  operationType: string;
  metadata?: Record<string, unknown>;
  expiresAt: Date;
}

/** Default reservation TTL (seconds) — matches the SQL `credit_reservations` default. */
const RESERVATION_TTL_MS = 10 * 60 * 1000;

/**
 * Credit store backed by in-memory maps.
 * Zero dependencies. Useful for unit testing and local development.
 *
 * Money is exact `Decimal` everywhere (contract §1). Because JavaScript is
 * single-threaded, every mutating method performs its read-modify-write
 * **synchronously** (no `await` between reading a balance and writing it back),
 * so a `Promise.all` of concurrent deductions cannot interleave and double-spend
 * (C2). A test-only injectable clock is exposed for deterministic time tests.
 */
export class MemoryStore implements CreditStore {
  private balances = new Map<string, Decimal>();
  private lifetime = new Map<string, Decimal>();
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
    usage: Decimal;
  }> = [];
  private spendCaps: SpendCap[] = [];
  private teams = new Map<
    string,
    { id: string; name: string; balance: Decimal; memberCount: number; createdAt: Date }
  >();
  private teamMembers = new Map<
    string,
    Map<string, { userId: string; role: string; spendCap: Decimal | null; totalSpent: Decimal }>
  >();

  /**
   * Injectable clock for deterministic time-dependent tests. Defaults to the
   * real wall clock. Tests set this to a fixed `Date` to avoid `setTimeout`
   * sleeps (contract §8).
   */
  private clock: () => Date = () => new Date();

  /** Override the clock used for all time comparisons (test-only). */
  setClock(clock: () => Date): void {
    this.clock = clock;
  }

  private now(): Date {
    return this.clock();
  }

  private balance(userId: string): Decimal {
    return this.balances.get(userId) ?? ZERO;
  }

  /** Billing-period key (UTC month start, YYYY-MM-DD) for the current clock. */
  private billingPeriod(): string {
    const now = this.now();
    return new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), 1)).toISOString().slice(0, 10);
  }

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
      balance: this.balance(userId),
      lifetimePurchased: this.lifetime.get(userId) ?? ZERO,
    };
  }

  async addCredits(
    userId: string,
    amount: Decimal,
    type = "adjustment",
    metadata?: CreditMetadata | null,
    expiresAt?: Date | null,
  ): Promise<AddCreditsResult> {
    // L2: reject non-finite amounts always, and non-positive amounts unless this
    // is an explicit `adjustment` (parity with SQL `credits_add`). A negative or
    // zero purchase/grant must never drive the balance below the floor.
    if (!amount.isFinite()) {
      throw new StoreError(`addCredits: amount must be finite, got ${amount.toString()}`);
    }
    if (type !== "adjustment" && amount.lte(0)) {
      throw new StoreError(
        `addCredits: ${type} amount must be > 0, got ${amount.toString()} (use type='adjustment' for negative/zero)`,
      );
    }

    const amt = quantizeMoney(amount);
    const current = this.balance(userId);
    this.balances.set(userId, current.plus(amt));

    const lifetimeAdd = type === "purchase" ? amt : ZERO;
    this.lifetime.set(userId, (this.lifetime.get(userId) ?? ZERO).plus(lifetimeAdd));

    const txId = randomUUID();
    const tx: TransactionRecord = {
      id: txId,
      userId,
      amount: amt,
      type,
      metadata: metadata ? this.cleanMetadata(metadata) : undefined,
      createdAt: this.now(),
      expiresAt: expiresAt ?? null,
    };
    this.transactions.push(tx);

    return {
      transactionId: txId,
      userId,
      amount: amt,
      newBalance: this.balance(userId),
      lifetimePurchased: this.lifetime.get(userId) ?? ZERO,
    };
  }

  private cleanMetadata(metadata: CreditMetadata): Record<string, unknown> {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(metadata)) {
      if (v != null) out[k] = v;
    }
    return out;
  }

  /** Remove expired reservations for a user (keeps the active-reservation sum honest). */
  private pruneReservations(userId: string): void {
    const now = this.now();
    for (const [id, r] of this.reservations) {
      if (r.userId === userId && r.expiresAt <= now) {
        this.reservations.delete(id);
      }
    }
  }

  async reserveCredits(
    userId: string,
    amount: Decimal,
    operationType: string,
    metadata?: CreditMetadata | null,
    minBalance: Decimal = new Decimal(5),
  ): Promise<ReserveResult> {
    // ── critical section (synchronous; no awaits) ──
    if (!amount.isFinite() || amount.lte(0)) {
      return {
        reservationId: "",
        userId,
        amount: ZERO,
        balance: this.balance(userId),
        reservedTotal: ZERO,
        error: "invalid_amount",
      };
    }

    this.pruneReservations(userId);

    const balance = this.balance(userId);
    let reservedTotal = ZERO;
    for (const r of this.reservations.values()) {
      if (r.userId === userId) reservedTotal = reservedTotal.plus(r.amount);
    }

    const available = balance.minus(reservedTotal);
    // Canonical rule (contract §3): REJECT (do not cap) if reserving the full
    // amount would push available below min_balance.
    if (available.minus(amount).lt(minBalance)) {
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
    this.reservations.set(rid, {
      id: rid,
      userId,
      amount,
      operationType,
      metadata: metadata ? this.cleanMetadata(metadata) : undefined,
      expiresAt: new Date(this.now().getTime() + RESERVATION_TTL_MS),
    });
    return {
      reservationId: rid,
      userId,
      amount,
      balance,
      reservedTotal: reservedTotal.plus(amount),
    };
  }

  async deductCredits(
    userId: string,
    reservationId: string,
    amount: Decimal,
    idempotencyKey?: string | null,
    metadata?: CreditMetadata | null,
  ): Promise<DeductionResult> {
    // ── critical section (synchronous; no awaits) ──
    // Idempotency-first (user-scoped).
    if (idempotencyKey) {
      const existing = this.transactions.find(
        (t) => t.userId === userId && t.metadata?.["idempotencyKey"] === idempotencyKey,
      );
      if (existing) {
        return {
          transactionId: existing.id,
          userId,
          amount: existing.amount,
          allowanceConsumed: ZERO,
          balanceAfter: this.balance(userId),
          idempotent: true,
          capWarning: null,
        };
      }
    }

    if (!amount.isFinite() || amount.lte(0)) {
      return {
        transactionId: "",
        userId,
        amount: ZERO,
        allowanceConsumed: ZERO,
        balanceAfter: this.balance(userId),
        idempotent: false,
        capWarning: null,
        error: "invalid_amount",
      };
    }

    // C3: the reservation is the authority on the maximum deductible amount.
    // It must exist, belong to this user, and be unexpired. Clamp amount to it.
    const reservation = this.reservations.get(reservationId);
    if (!reservation || reservation.userId !== userId || reservation.expiresAt <= this.now()) {
      return {
        transactionId: "",
        userId,
        amount: ZERO,
        allowanceConsumed: ZERO,
        balanceAfter: this.balance(userId),
        idempotent: false,
        capWarning: null,
        error: "not_found",
      };
    }

    const deductAmount = Decimal.min(amount, reservation.amount);

    const current = this.balance(userId);
    if (current.lt(deductAmount)) {
      return {
        transactionId: "",
        userId,
        amount: deductAmount.negated(),
        allowanceConsumed: ZERO,
        balanceAfter: current,
        idempotent: false,
        capWarning: null,
        error: "insufficient_credits",
      };
    }

    this.balances.set(userId, current.minus(deductAmount));
    this.reservations.delete(reservationId);

    const txMeta = metadata ? this.cleanMetadata(metadata) : {};
    if (idempotencyKey) txMeta["idempotencyKey"] = idempotencyKey;

    const txId = randomUUID();
    this.transactions.push({
      id: txId,
      userId,
      amount: deductAmount.negated(),
      type: "usage",
      metadata: txMeta,
      createdAt: this.now(),
    });

    return {
      transactionId: txId,
      userId,
      amount: deductAmount.negated(),
      allowanceConsumed: ZERO,
      balanceAfter: current.minus(deductAmount),
      idempotent: false,
      capWarning: null,
    };
  }

  /**
   * Atomic "calculate-then-charge" in a single synchronous critical section
   * (contract §2). Mirrors the SQL `deduct_with_allowance` RPC:
   * idempotency-first → consume allowance → cap on net → balance floor → debit.
   * A `deny` cap or floor breach consumes NO allowance.
   */
  async deductWithAllowance(
    userId: string,
    amount: Decimal,
    options?: DeductWithAllowanceOptions,
  ): Promise<DeductionResult> {
    const idempotencyKey = options?.idempotencyKey ?? null;
    const minBalance = options?.minBalance ?? ZERO;
    const model = options?.model ?? null;
    const metadata = options?.metadata ?? null;

    // ── critical section (synchronous; no awaits) ──

    // Reject non-finite / negative amounts. Zero is a valid no-op charge.
    if (!amount.isFinite() || amount.lt(0)) {
      return {
        transactionId: "",
        userId,
        amount: ZERO,
        allowanceConsumed: ZERO,
        balanceAfter: this.balance(userId),
        idempotent: false,
        capWarning: null,
        error: "invalid_amount",
      };
    }

    // (2) Idempotency (user-scoped): replay the original result.
    if (idempotencyKey) {
      const existing = this.transactions.find(
        (t) => t.userId === userId && t.metadata?.["idempotencyKey"] === idempotencyKey,
      );
      if (existing) {
        const consumed = existing.metadata?.["allowanceConsumed"];
        return {
          transactionId: existing.id,
          userId,
          amount: existing.amount.abs(),
          allowanceConsumed: consumed instanceof Decimal ? consumed : new Decimal(String(consumed ?? 0)),
          balanceAfter: this.balance(userId),
          idempotent: true,
          capWarning: null,
        };
      }
    }

    const gross = quantizeMoney(amount);

    // (3) Allowance: consume as much of the cost as remaining free allowance covers.
    let consume = ZERO;
    const planId = this.userPlanMap.get(userId);
    const planDef = planId ? this.planDefinitions.get(planId) : undefined;
    if (planId && planDef) {
      const billingPeriod = this.billingPeriod();
      let used = ZERO;
      for (const w of this.usageWindows) {
        if (w.userId === userId && w.planId === planId && w.billingPeriod === billingPeriod) {
          used = used.plus(w.usage);
        }
      }
      const remaining = Decimal.max(ZERO, planDef.freeAllowance.minus(used));
      consume = Decimal.min(remaining, gross);
    }

    const net = gross.minus(consume);

    // (4) Spend cap on the NET amount. Deny aborts WITHOUT consuming allowance.
    let capWarning: string | null = null;
    const userCaps = this.spendCaps.filter(
      (c) => c.userId === userId && (c.model == null || c.model === model),
    );
    // Deny caps first (most restrictive), then soft caps.
    const ordered = [...userCaps].sort(
      (a, b) => (a.action === "deny" ? 0 : 1) - (b.action === "deny" ? 0 : 1),
    );
    for (const cap of ordered) {
      const windowStart = this.capWindowStart(cap.type);
      const currentSpend = this.spendInWindow(userId, windowStart, cap.model);
      if (currentSpend.plus(net).gt(cap.limit)) {
        if (cap.action === "deny") {
          // Abort: no allowance consumed, no balance change.
          return {
            transactionId: "",
            userId,
            amount: ZERO,
            allowanceConsumed: ZERO,
            balanceAfter: this.balance(userId),
            idempotent: false,
            capWarning: null,
            error: "cap_reached",
          };
        }
        if (capWarning === null) capWarning = cap.action;
      }
    }

    // (5) Balance floor on the NET amount.
    const current = this.balance(userId);
    if (current.minus(net).lt(minBalance)) {
      return {
        transactionId: "",
        userId,
        amount: ZERO,
        allowanceConsumed: ZERO,
        balanceAfter: current,
        idempotent: false,
        capWarning: null,
        error: "insufficient_credits",
      };
    }

    // (6) Commit: consume allowance, debit balance, insert ledger row.
    if (consume.gt(0) && planId) {
      this.incrementUsageWindowSync(userId, planId, consume);
    }

    this.balances.set(userId, current.minus(net));

    const txMeta = metadata ? this.cleanMetadata(metadata) : {};
    if (idempotencyKey) txMeta["idempotencyKey"] = idempotencyKey;
    if (model != null) txMeta["model"] = model;
    txMeta["allowanceConsumed"] = consume;

    const txId = randomUUID();
    this.transactions.push({
      id: txId,
      userId,
      amount: net.negated(),
      type: "usage",
      metadata: txMeta,
      createdAt: this.now(),
    });

    return {
      transactionId: txId,
      userId,
      amount: net,
      allowanceConsumed: consume,
      balanceAfter: current.minus(net),
      idempotent: false,
      capWarning,
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
      freeAllowance: planDef?.freeAllowance ?? ZERO,
      features: (planDef?.features as Record<string, unknown>) ?? {},
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
      hasFeature: present && featurePresent(value),
    };
  }

  async setUserPlan(userId: string, planId: string): Promise<SetUserPlanResult> {
    this.userPlanMap.set(userId, planId);
    return { userId, planId };
  }

  async checkAllowance(userId: string): Promise<AllowanceResult> {
    const planId = this.userPlanMap.get(userId);
    if (!planId) {
      return { planId: "", allowanceRemaining: ZERO, periodStart: "", periodEnd: "" };
    }
    const planDef = this.planDefinitions.get(planId);
    if (!planDef) {
      return { planId: "", allowanceRemaining: ZERO, periodStart: "", periodEnd: "" };
    }
    const now = this.now();
    const periodStart = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), 1));
    const periodEnd = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + 1, 0));
    const billingPeriod = periodStart.toISOString().slice(0, 10);
    let usage = ZERO;
    for (const w of this.usageWindows) {
      if (w.userId === userId && w.planId === planId && w.billingPeriod === billingPeriod) {
        usage = usage.plus(w.usage);
      }
    }
    return {
      planId,
      allowanceRemaining: Decimal.max(planDef.freeAllowance.minus(usage), ZERO),
      periodStart: periodStart.toISOString(),
      periodEnd: periodEnd.toISOString(),
    };
  }

  private incrementUsageWindowSync(userId: string, planId: string, amount: Decimal): void {
    const billingPeriod = this.billingPeriod();
    const existing = this.usageWindows.find(
      (w) => w.userId === userId && w.planId === planId && w.billingPeriod === billingPeriod,
    );
    if (existing) {
      existing.usage = existing.usage.plus(amount);
    } else {
      this.usageWindows.push({ userId, planId, billingPeriod, usage: amount });
    }
  }

  async incrementUsageWindow(userId: string, planId: string, amount: Decimal): Promise<void> {
    this.incrementUsageWindowSync(userId, planId, amount);
  }

  // ── Refunds ──────────────────────────────────────────────────────────

  async refundCredits(
    transactionId: string,
    amount?: Decimal,
    reason?: string,
    metadata?: CreditMetadata | null,
  ): Promise<RefundResult> {
    // ── critical section (synchronous; no awaits) ──
    const origTx = this.transactions.find((t) => t.id === transactionId);
    if (!origTx) {
      return {
        refundTransactionId: "",
        originalTransactionId: transactionId,
        userId: "",
        amount: ZERO,
        newBalance: ZERO,
        error: "not_found",
      };
    }

    // Only a usage/team_usage debit (negative amount) is refundable. Anything
    // else (purchase/refund/adjustment/bonus) has zero refundable amount, so any
    // refund over-refunds (parity with SQL refund RPC).
    if (
      (origTx.type !== "usage" && origTx.type !== "team_usage") ||
      origTx.amount.gte(0)
    ) {
      return {
        refundTransactionId: "",
        originalTransactionId: transactionId,
        userId: origTx.userId,
        amount: ZERO,
        newBalance: this.balance(origTx.userId),
        error: "over_refund",
      };
    }

    const originalDebit = origTx.amount.abs();

    // Back-compat: an exact duplicate of a prior FULL refund → already_refunded.
    const fullRefundExists = this.transactions.some(
      (t) => t.type === "refund" && t.referenceId === transactionId && t.amount.eq(originalDebit),
    );
    if (fullRefundExists) {
      return {
        refundTransactionId: "",
        originalTransactionId: transactionId,
        userId: origTx.userId,
        amount: ZERO,
        newBalance: this.balance(origTx.userId),
        error: "already_refunded",
      };
    }

    // Sum prior refunds for cumulative-partial over-refund detection.
    let priorRefunded = ZERO;
    for (const t of this.transactions) {
      if (t.type === "refund" && t.referenceId === transactionId) {
        priorRefunded = priorRefunded.plus(t.amount);
      }
    }
    const remaining = originalDebit.minus(priorRefunded);

    const refundAmount = quantizeMoney(amount ?? remaining);

    // Over-refund: non-positive request, or one exceeding what remains.
    if (refundAmount.lte(0) || refundAmount.gt(remaining)) {
      return {
        refundTransactionId: "",
        originalTransactionId: transactionId,
        userId: origTx.userId,
        amount: ZERO,
        newBalance: this.balance(origTx.userId),
        error: "over_refund",
      };
    }

    // Restore balance and append the refund ledger row.
    const current = this.balance(origTx.userId);
    this.balances.set(origTx.userId, current.plus(refundAmount));

    const txMeta = metadata ? this.cleanMetadata(metadata) : {};
    if (reason) txMeta["reason"] = reason;

    const txId = randomUUID();
    this.transactions.push({
      id: txId,
      userId: origTx.userId,
      amount: refundAmount,
      type: "refund",
      referenceType: reason ?? null,
      referenceId: transactionId,
      metadata: txMeta,
      createdAt: this.now(),
    });

    return {
      refundTransactionId: txId,
      originalTransactionId: transactionId,
      userId: origTx.userId,
      amount: refundAmount,
      newBalance: current.plus(refundAmount),
    };
  }

  // ── Credit expiry ─────────────────────────────────────────────────────

  async sweepExpiredCredits(dryRun = false): Promise<SweepResult> {
    // ── critical section (synchronous; no awaits) ──
    const now = this.now();
    const expiredByUser = new Map<string, Decimal>();
    const expiredTxs: TransactionRecord[] = [];

    // Find all expired, not-yet-swept grant transactions.
    for (const tx of this.transactions) {
      if (
        tx.expiresAt &&
        !tx.sweptAt && // H4: never re-sweep a previously swept grant
        (tx.type === "purchase" || tx.type === "adjustment")
      ) {
        if (tx.expiresAt <= now) {
          const current = expiredByUser.get(tx.userId) ?? ZERO;
          expiredByUser.set(tx.userId, current.plus(tx.amount));
          expiredTxs.push(tx);
        }
      }
    }

    let expiredCount = 0;
    let expiredAmount = ZERO;

    for (const [userId, totalExpired] of expiredByUser) {
      const currentBalance = this.balance(userId);
      const toExpire = Decimal.min(totalExpired, currentBalance);

      if (toExpire.gt(0)) {
        expiredCount++;
        expiredAmount = expiredAmount.plus(toExpire);

        if (!dryRun) {
          this.balances.set(userId, currentBalance.minus(toExpire));

          // H4: mark swept grants so a second sweep reports zero.
          for (const et of expiredTxs) {
            if (et.userId === userId) {
              et.sweptAt = now;
              et.expiresAt = null;
            }
          }

          const txId = randomUUID();
          this.transactions.push({
            id: txId,
            userId,
            amount: toExpire.negated(),
            type: "adjustment",
            metadata: { reason: "credit_expired", expiredAmount: toExpire },
            createdAt: now,
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
      (t) => t.type === "usage" && t.amount.lt(0) && t.createdAt >= start && t.createdAt <= end,
    );
  }

  async spendByUser(start: Date, end: Date): Promise<SpendByUserRow[]> {
    const usage = this._usageInWindow(start, end);
    const byUser = new Map<string, { total: Decimal; count: number }>();
    for (const t of usage) {
      const entry = byUser.get(t.userId) ?? { total: ZERO, count: 0 };
      entry.total = entry.total.plus(t.amount.abs());
      entry.count++;
      byUser.set(t.userId, entry);
    }
    return Array.from(byUser.entries())
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([userId, { total, count }]) => ({
        userId,
        totalSpend: total,
        transactionCount: count,
      }));
  }

  async spendByModel(start: Date, end: Date): Promise<SpendByModelRow[]> {
    const usage = this._usageInWindow(start, end);
    const byModel = new Map<string, { total: Decimal; count: number }>();
    for (const t of usage) {
      const model = (t.metadata?.model as string) ?? "unknown";
      const entry = byModel.get(model) ?? { total: ZERO, count: 0 };
      entry.total = entry.total.plus(t.amount.abs());
      entry.count++;
      byModel.set(model, entry);
    }
    return Array.from(byModel.entries())
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([model, { total, count }]) => ({
        model,
        totalSpend: total,
        transactionCount: count,
      }));
  }

  async topUsers(limit: number, start: Date, end: Date): Promise<TopUserRow[]> {
    const byUser = await this.spendByUser(start, end);
    return byUser
      .sort((a, b) => b.totalSpend.comparedTo(a.totalSpend))
      .slice(0, limit)
      .map((r) => ({ userId: r.userId, totalSpend: r.totalSpend }));
  }

  // ── Transaction listing ─────────────────────────────────────────────

  async listUserTransactions(
    userId: string,
    options?: ListTransactionsOptions,
  ): Promise<PaginatedTransactions> {
    const limit = options?.limit ?? 50;
    const offset = options?.offset ?? 0;

    const filtered = this.transactions.filter((t) => {
      if (t.userId !== userId) return false;
      if (options?.types && !options.types.includes(t.type)) return false;
      if (options?.fromDate && t.createdAt < options.fromDate) return false;
      if (options?.toDate && t.createdAt > options.toDate) return false;
      return true;
    });

    // Sort newest first
    filtered.sort((a, b) => b.createdAt.getTime() - a.createdAt.getTime());

    const total = filtered.length;
    const items = filtered.slice(offset, offset + limit);

    return {
      total,
      items: items.map((t) => ({
        id: t.id,
        userId: t.userId,
        amount: t.amount,
        type: t.type,
        referenceType: t.referenceType ?? null,
        referenceId: t.referenceId ?? null,
        metadata: (t.metadata as Record<string, unknown> | null) ?? null,
        createdAt: t.createdAt.toISOString(),
      })),
    };
  }

  async listUsageEvents(
    userId: string,
    options?: ListUsageEventsOptions,
  ): Promise<PaginatedTransactions> {
    let items = this.transactions.filter((t) => t.userId === userId && t.type === "usage");

    if (options?.fromDate) {
      const from = options.fromDate;
      items = items.filter((t) => t.createdAt >= from);
    }
    if (options?.toDate) {
      const to = options.toDate;
      items = items.filter((t) => t.createdAt <= to);
    }

    const total = items.length;
    const offset = options?.offset ?? 0;
    const limit = options?.limit ?? 50;
    const page = items
      .sort((a, b) => b.createdAt.getTime() - a.createdAt.getTime())
      .slice(offset, offset + limit);

    return {
      total,
      items: page.map((t) => ({
        id: t.id,
        userId: t.userId,
        amount: t.amount,
        type: t.type,
        referenceType: t.referenceType ?? null,
        referenceId: t.referenceId ?? null,
        metadata: (t.metadata as Record<string, unknown> | null) ?? null,
        createdAt: t.createdAt.toISOString(),
      })),
    };
  }

  // ── Aggregate stats ──────────────────────────────────────────────────

  async aggregateStats(start: Date, end: Date): Promise<AggregateStats> {
    const usage = this._usageInWindow(start, end);
    if (usage.length === 0) {
      return {
        totalCreditsConsumed: ZERO,
        activeUsers: 0,
        avgDailySpend: ZERO,
        topModel: "",
        topUser: "",
      };
    }
    let total = ZERO;
    for (const t of usage) total = total.plus(t.amount.abs());
    const activeUsers = new Set(usage.map((t) => t.userId)).size;
    const days = new Set(usage.map((t) => t.createdAt.toISOString().slice(0, 10))).size;
    // NUMERIC division (no integer truncation) — quantize to 4dp.
    const avgDailySpend = days > 0 ? quantizeMoney(total.div(days)) : ZERO;
    const byModel = new Map<string, Decimal>();
    const byUser = new Map<string, Decimal>();
    for (const t of usage) {
      const model = (t.metadata?.model as string) ?? "unknown";
      byModel.set(model, (byModel.get(model) ?? ZERO).plus(t.amount.abs()));
      byUser.set(t.userId, (byUser.get(t.userId) ?? ZERO).plus(t.amount.abs()));
    }
    const topModel =
      byModel.size > 0
        ? [...byModel.entries()].reduce((best, curr) => (curr[1].gt(best[1]) ? curr : best))[0]
        : "";
    const topUser =
      byUser.size > 0
        ? [...byUser.entries()].reduce((best, curr) => (curr[1].gt(best[1]) ? curr : best))[0]
        : "";
    return { totalCreditsConsumed: total, activeUsers, avgDailySpend, topModel, topUser };
  }

  // ── Spend caps and rate limiting ─────────────────────────────────────

  /** Configure a spend cap (MemoryStore-only helper for testing). */
  setSpendCap(cap: SpendCap): void {
    this.spendCaps.push(cap);
  }

  private capWindowStart(type: "daily" | "monthly"): Date {
    const now = this.now();
    if (type === "daily") {
      return new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));
    }
    return new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), 1));
  }

  /** Sum spend (positive magnitude) in a window, optionally restricted to a model. */
  private spendInWindow(userId: string, windowStart: Date, capModel?: string | null): Decimal {
    let total = ZERO;
    for (const t of this.transactions) {
      if (t.userId !== userId) continue;
      if (t.type !== "usage" && t.type !== "team_usage") continue;
      if (t.amount.gte(0)) continue;
      if (capModel != null && t.metadata?.model !== capModel) continue;
      if (t.createdAt >= windowStart) total = total.plus(t.amount.abs());
    }
    return total;
  }

  async checkSpendCap(
    userId: string,
    model?: string | null,
    amount?: Decimal,
  ): Promise<CapCheckResult> {
    const amt = amount ?? ZERO;
    const userCaps = this.spendCaps.filter((c) => c.userId === userId);
    if (userCaps.length === 0) {
      return { capped: false, currentSpend: ZERO, limit: ZERO, action: null };
    }

    // Check deny caps first — most restrictive
    for (const cap of userCaps) {
      if (cap.model && cap.model !== model) continue;
      if (cap.action !== "deny") continue;
      const windowStart = this.capWindowStart(cap.type);
      const currentSpend = this.spendInWindow(userId, windowStart, cap.model);
      if (currentSpend.plus(amt).gt(cap.limit)) {
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
      const windowStart = this.capWindowStart(cap.type);
      const currentSpend = this.spendInWindow(userId, windowStart, cap.model);
      if (currentSpend.plus(amt).gt(cap.limit)) {
        return {
          capped: false,
          currentSpend,
          limit: cap.limit,
          action: cap.action,
          model: cap.model,
        };
      }
    }

    return { capped: false, currentSpend: ZERO, limit: ZERO, action: null };
  }

  // ── Team/shared balance pools ────────────────────────────────────────

  async createTeam(name: string, initialBalance: Decimal = ZERO): Promise<CreateTeamResult> {
    const teamId = randomUUID();
    this.teams.set(teamId, {
      id: teamId,
      name,
      balance: initialBalance,
      memberCount: 0,
      createdAt: this.now(),
    });
    this.teamMembers.set(teamId, new Map());
    return { teamId, name };
  }

  async getTeamBalance(teamId: string): Promise<TeamBalanceResult> {
    const team = this.teams.get(teamId);
    if (!team) {
      return { teamId, name: "", balance: ZERO, memberCount: 0 };
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
    spendCap?: Decimal | null,
  ): Promise<AddTeamMemberResult> {
    const members = this.teamMembers.get(teamId);
    if (!members) {
      return { teamId, userId, role: "" };
    }
    members.set(userId, { userId, role, spendCap: spendCap ?? null, totalSpent: ZERO });
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
    amount: Decimal,
    metadata?: CreditMetadata | null,
    idempotencyKey?: string | null,
  ): Promise<TeamDeductionResult> {
    // ── critical section (synchronous; no awaits) ──
    const team = this.teams.get(teamId);
    if (!team) {
      return {
        transactionId: "",
        teamId,
        userId,
        amount: ZERO,
        teamBalanceAfter: ZERO,
        error: "team_not_found",
      };
    }

    // Idempotency-first (user/team-scoped): replay the original team debit.
    if (idempotencyKey) {
      const existing = this.transactions.find(
        (t) =>
          t.type === "team_usage" &&
          t.metadata?.["teamId"] === teamId &&
          t.metadata?.["idempotencyKey"] === idempotencyKey,
      );
      if (existing) {
        return {
          transactionId: existing.id,
          teamId,
          userId: existing.userId,
          amount: existing.amount,
          teamBalanceAfter: team.balance,
        };
      }
    }

    const members = this.teamMembers.get(teamId);
    const member = members?.get(userId);
    if (!member) {
      return {
        transactionId: "",
        teamId,
        userId,
        amount: ZERO,
        teamBalanceAfter: team.balance,
        error: "user_not_in_team",
      };
    }

    if (!amount.isFinite() || amount.lte(0)) {
      return {
        transactionId: "",
        teamId,
        userId,
        amount: ZERO,
        teamBalanceAfter: team.balance,
        error: "invalid_amount",
      };
    }

    // Enforce spend cap
    if (member.spendCap != null && member.totalSpent.plus(amount).gt(member.spendCap)) {
      return {
        transactionId: "",
        teamId,
        userId,
        amount: ZERO,
        teamBalanceAfter: team.balance,
        error: "spend_cap_exceeded",
      };
    }

    if (team.balance.lt(amount)) {
      return {
        transactionId: "",
        teamId,
        userId,
        amount: ZERO,
        teamBalanceAfter: team.balance,
        error: "insufficient_team_balance",
      };
    }

    team.balance = team.balance.minus(amount);
    member.totalSpent = member.totalSpent.plus(amount);

    const txMeta: Record<string, unknown> = {
      ...(metadata ? this.cleanMetadata(metadata) : {}),
      teamId,
    };
    if (idempotencyKey) txMeta["idempotencyKey"] = idempotencyKey;

    const txId = randomUUID();
    this.transactions.push({
      id: txId,
      userId,
      amount: amount.negated(),
      type: "team_usage",
      metadata: txMeta,
      createdAt: this.now(),
    });

    return {
      transactionId: txId,
      teamId,
      userId,
      amount: amount.negated(),
      teamBalanceAfter: team.balance,
    };
  }

  async dailySpend(start: Date, end: Date): Promise<DailySpendRow[]> {
    const usage = this._usageInWindow(start, end);
    const byDay = new Map<string, { total: Decimal; count: number }>();
    for (const t of usage) {
      const date = t.createdAt.toISOString().slice(0, 10);
      const entry = byDay.get(date) ?? { total: ZERO, count: 0 };
      entry.total = entry.total.plus(t.amount.abs());
      entry.count++;
      byDay.set(date, entry);
    }
    return Array.from(byDay.entries())
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([date, { total, count }]) => ({ date, totalSpend: total, transactionCount: count }));
  }
}
