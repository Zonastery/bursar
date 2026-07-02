import { randomUUID } from "crypto";
import Decimal from "decimal.js";
import { quantizeMoney } from "../expr.js";
import { StoreError } from "../errors.js";
import { resolveAllowanceWindow } from "../allowance.js";
import type { AllowancePeriod } from "../allowance.js";
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
  GetUserPlanResult,
  LeaseResult,
  ListTransactionsOptions,
  ListUsageEventsOptions,
  PaginatedTransactions,
  PlanDefinition,
  PricingConfigData,
  PricingConfigHistoryItem,
  PricingConfigResult,
  RefundResult,
  ReleaseResult,
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
import { CreditStore } from "./credit-store.js";
import type { CreateLeaseOptions, SettleLeaseOptions } from "./credit-store.js";

const ZERO = new Decimal(0);

/** Coerce a presence-or-truthiness feature value per contract §5 (M6). */
function featurePresent(value: unknown): boolean {
  // Identity form: numeric 0 / "" count as present. Matches Python
  // `value is not None and value is not False`. Do NOT use Boolean(value).
  return value !== null && value !== undefined && value !== false;
}

/**
 * Normalise a raw plan object (possibly snake_case from a JSON fixture or raw dict)
 * into a proper PlanDefinition with Decimal fields. Accepts both camelCase and
 * snake_case keys so configs written in either style work without preprocessing.
 */
function normalisePlanDefinition(planKey: string, raw: unknown): PlanDefinition {
  const p = raw as Record<string, unknown>;
  const freeAllowanceRaw = (p["freeAllowance"] ?? p["free_allowance"]) as number | string | undefined;
  const defaultBillingModeRaw = (p["defaultBillingMode"] ?? p["billingMode"] ?? p["default_billing_mode"] ?? p["billing_mode"]) as string | undefined;
  const overdraftFloorRaw = (p["overdraftFloor"] ?? p["overdraft_floor"]) as number | string | null | undefined;
  const rateOverridesRaw = (p["rateOverrides"] ?? p["rate_overrides"]) as Record<string, string> | null | undefined;
  const perOperationRaw = (p["perOperation"] ?? p["per_operation"]) as Record<string, unknown> | null | undefined;
  const maxConcurrentRaw = (p["maxConcurrent"] ?? p["max_concurrent"]) as number | null | undefined;
  const allowancePeriodRaw = (p["allowancePeriod"] ?? p["allowance_period"]) as
    | AllowancePeriod
    | undefined;

  const billingMode = (defaultBillingModeRaw === "overdraft" ? "overdraft" : "strict") as "strict" | "overdraft";
  return {
    id: (p["id"] as string | undefined) ?? planKey,
    name: (p["name"] as string | undefined) ?? planKey,
    freeAllowance: freeAllowanceRaw != null ? new Decimal(freeAllowanceRaw) : ZERO,
    rateOverrides: rateOverridesRaw ?? null,
    features: (p["features"] as Record<string, unknown> | null | undefined) ?? null,
    defaultBillingMode: billingMode,
    overdraftFloor: overdraftFloorRaw != null ? new Decimal(overdraftFloorRaw) : null,
    maxConcurrent: maxConcurrentRaw ?? null,
    perOperation: (perOperationRaw as PlanDefinition["perOperation"]) ?? undefined,
    allowancePeriod: allowancePeriodRaw ?? "calendar_month",
  };
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

/**
 * Internal reservation/lease record used by the lease lifecycle
 * (``createLease``/``settleLease``/``releaseLease``/``renewLease``).
 * ``status`` is driven through ``active → settled | released | expired``.
 * ``billingMode``/``overdraftFloor`` record the resolved admission policy;
 * ``settleTxId`` links to the settling transaction.
 */
interface ReservationRecord {
  id: string;
  userId: string;
  amount: Decimal;
  operationType: string;
  metadata?: Record<string, unknown>;
  expiresAt: Date;
  status: string;
  billingMode: BillingMode;
  overdraftFloor: Decimal | null;
  settleTxId: string | null;
}

/** Default lease TTL (seconds) for the lease lifecycle (interface plan §3). */
const DEFAULT_LEASE_TTL_SECONDS = 600;

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
export class MemoryStore extends CreditStore {
  private balances = new Map<string, Decimal>();
  private lifetime = new Map<string, Decimal>();
  private transactions: TransactionRecord[] = [];
  private reservations = new Map<string, ReservationRecord>();
  private pricingVersion = 0;
  private pricingLabel: string | null = null;
  private pricingHistory: Array<{
    id: string;
    version: number;
    label: string | null;
    active: boolean;
    config: PricingConfigData;
    createdAt: string;
  }> = [];
  private planDefinitions = new Map<string, PlanDefinition>();
  private userPlanMap = new Map<string, string>();
  /** Timestamp each user's CURRENT plan was assigned — anchor for WS9 non-calendar periods. */
  private userPlanAssignedAt = new Map<string, Date>();
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

  /**
   * Billing-period key (YYYY-MM-DD) used as the in-memory usage-window bucket.
   *
   * When an explicit ``periodStart`` is supplied (WS9 — a non-calendar-month
   * allowance window resolved by the manager), it is used directly. Otherwise
   * falls back to the UTC calendar-month start for the current clock, exactly
   * matching pre-WS9 behaviour.
   */
  private billingPeriod(periodStart?: Date | null): string {
    if (periodStart) return periodStart.toISOString().slice(0, 10);
    const now = this.now();
    return new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), 1))
      .toISOString()
      .slice(0, 10);
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
    const periodStart = options?.periodStart ?? null;

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
          allowanceConsumed:
            consumed instanceof Decimal ? consumed : new Decimal(String(consumed ?? 0)),
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
      const billingPeriod = this.billingPeriod(periodStart);
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
      this.incrementUsageWindowSync(userId, planId, consume, periodStart);
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

  // ── Lease lifecycle (atomic admission) ─────────────────────────────

  /** Active, unexpired holds for a user (synchronous — no awaits in callers). */
  private activeLeases(userId: string, operationType?: string): ReservationRecord[] {
    const now = this.now();
    const out: ReservationRecord[] = [];
    for (const r of this.reservations.values()) {
      if (
        r.userId === userId &&
        r.status === "active" &&
        r.expiresAt > now &&
        (operationType === undefined || r.operationType === operationType)
      ) {
        out.push(r);
      }
    }
    return out;
  }

  async createLease(
    userId: string,
    amount: Decimal,
    operationType: string,
    options?: CreateLeaseOptions,
  ): Promise<LeaseResult> {
    const billingMode = options?.billingMode ?? "strict";
    const floor = options?.floor ?? ZERO;
    const maxConcurrent = options?.maxConcurrent ?? null;
    const ttlSeconds = options?.ttlSeconds ?? DEFAULT_LEASE_TTL_SECONDS;
    const model = options?.model ?? null;
    const overdraftFloor = options?.overdraftFloor ?? null;
    const metadata = options?.metadata ?? null;

    // ── critical section (synchronous; no awaits) ──
    if (!amount.isFinite() || amount.lte(0)) {
      return {
        leaseId: "",
        userId,
        amount: ZERO,
        available: ZERO,
        reservedTotal: ZERO,
        billingMode,
        expiresAt: "",
        error: "invalid_amount",
      };
    }

    // Ensure a balance row exists (overdraft admits brand-new users at 0).
    const balance = this.balance(userId);
    if (!this.balances.has(userId)) this.balances.set(userId, balance);

    // (2) Concurrency: count active leases for this operation type.
    if (
      maxConcurrent !== null &&
      this.activeLeases(userId, operationType).length >= maxConcurrent
    ) {
      return {
        leaseId: "",
        userId,
        amount: ZERO,
        available: ZERO,
        reservedTotal: ZERO,
        billingMode,
        expiresAt: "",
        error: "concurrency_limit",
      };
    }

    // (3) Deny spend cap at admission: a blocked user can't even start.
    const userCaps = this.spendCaps.filter(
      (c) => c.userId === userId && (c.model == null || c.model === model),
    );
    for (const cap of userCaps) {
      if (cap.action !== "deny") continue;
      const windowStart = this.capWindowStart(cap.type);
      const spend = this.spendInWindow(userId, windowStart, cap.model);
      if (spend.plus(amount).gt(cap.limit)) {
        return {
          leaseId: "",
          userId,
          amount: ZERO,
          available: ZERO,
          reservedTotal: ZERO,
          billingMode,
          expiresAt: "",
          error: "cap_reached",
        };
      }
    }

    // (4) available = balance − Σ active holds; reject if floor breached.
    let reservedTotal = ZERO;
    for (const r of this.activeLeases(userId)) reservedTotal = reservedTotal.plus(r.amount);
    const available = balance.minus(reservedTotal);
    if (available.minus(amount).lt(floor)) {
      return {
        leaseId: "",
        userId,
        amount: ZERO,
        available,
        reservedTotal,
        billingMode,
        expiresAt: "",
        error: "insufficient_credits",
      };
    }

    // (5) Insert the active lease.
    const lid = randomUUID();
    const expiresAt = new Date(this.now().getTime() + ttlSeconds * 1000);
    this.reservations.set(lid, {
      id: lid,
      userId,
      amount,
      operationType,
      metadata: metadata ? this.cleanMetadata(metadata) : undefined,
      expiresAt,
      status: "active",
      billingMode,
      overdraftFloor,
      settleTxId: null,
    });

    return {
      leaseId: lid,
      userId,
      amount,
      available: available.minus(amount),
      reservedTotal: reservedTotal.plus(amount),
      billingMode,
      expiresAt: expiresAt.toISOString(),
    };
  }

  /** Build an idempotent-replay `DeductionResult` from a ledger row (synchronous). */
  private replayDeduction(
    tx: TransactionRecord,
    userId: string,
    balance: Decimal,
  ): DeductionResult {
    const consumed = tx.metadata?.["allowanceConsumed"];
    return {
      transactionId: tx.id,
      userId,
      amount: tx.amount.abs(),
      allowanceConsumed:
        consumed instanceof Decimal ? consumed : new Decimal(String(consumed ?? 0)),
      balanceAfter: balance,
      idempotent: true,
      capWarning: null,
    };
  }

  /**
   * Validate a lease for settle. Returns a short-circuit result, or `null` to
   * proceed (synchronous):
   * - missing / other-user / released → ``lease_not_found``
   * - already settled → idempotent replay of the original charge
   * - TTL elapsed → mark ``expired`` and return ``lease_expired``
   */
  private settleLeaseState(
    lease: ReservationRecord | undefined,
    userId: string,
    balance: Decimal,
  ): DeductionResult | null {
    const now = this.now();
    if (!lease || lease.userId !== userId || lease.status === "released") {
      return {
        transactionId: "",
        userId,
        amount: ZERO,
        allowanceConsumed: ZERO,
        balanceAfter: balance,
        idempotent: false,
        capWarning: null,
        error: "lease_not_found",
      };
    }
    if (lease.status === "settled") {
      if (lease.settleTxId) {
        const tx = this.transactions.find((t) => t.id === lease.settleTxId);
        if (tx) return this.replayDeduction(tx, userId, balance);
      }
      return {
        transactionId: "",
        userId,
        amount: ZERO,
        allowanceConsumed: ZERO,
        balanceAfter: balance,
        idempotent: true,
        capWarning: null,
      };
    }
    if (lease.status === "expired" || lease.expiresAt <= now) {
      lease.status = "expired";
      return {
        transactionId: "",
        userId,
        amount: ZERO,
        allowanceConsumed: ZERO,
        balanceAfter: balance,
        idempotent: false,
        capWarning: null,
        error: "lease_expired",
      };
    }
    return null;
  }

  async settleLease(
    userId: string,
    leaseId: string,
    amount: Decimal,
    options?: SettleLeaseOptions,
  ): Promise<DeductionResult> {
    const idempotencyKey = options?.idempotencyKey ?? null;
    const model = options?.model ?? null;
    const metadata = options?.metadata ?? null;
    const periodStart = options?.periodStart ?? null;

    // ── critical section (synchronous; no awaits) ──
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

    const balance = this.balance(userId);

    // Idempotency replay (user-scoped).
    if (idempotencyKey) {
      const existing = this.transactions.find(
        (t) => t.userId === userId && t.metadata?.["idempotencyKey"] === idempotencyKey,
      );
      if (existing) return this.replayDeduction(existing, userId, balance);
    }

    const lease = this.reservations.get(leaseId);
    const precheck = this.settleLeaseState(lease, userId, balance);
    if (precheck !== null) return precheck;
    // settleLeaseState returns early on a missing lease, so `lease` is defined.
    const activeLease = lease as ReservationRecord;

    // Active & unexpired → settle. De-clamped: charge the ACTUAL cost (D5), never
    // clamp to the lease hold.

    // Zero-cost: release the lease without charging (resolves M3).
    if (amount.eq(0)) {
      activeLease.status = "settled";
      return {
        transactionId: "",
        userId,
        amount: ZERO,
        allowanceConsumed: ZERO,
        balanceAfter: balance,
        idempotent: false,
        capWarning: null,
      };
    }

    // Allowance consume on the actual cost.
    let consume = ZERO;
    const planId = this.userPlanMap.get(userId);
    const planDef = planId ? this.planDefinitions.get(planId) : undefined;
    if (planId && planDef) {
      const billingPeriod = this.billingPeriod(periodStart);
      let used = ZERO;
      for (const w of this.usageWindows) {
        if (w.userId === userId && w.planId === planId && w.billingPeriod === billingPeriod) {
          used = used.plus(w.usage);
        }
      }
      const remaining = Decimal.max(ZERO, planDef.freeAllowance.minus(used));
      consume = Decimal.min(remaining, amount);
    }
    // Floor enforcement (C1): clamp net so balance stays ≥ floor.
    // The floor is derived from the lease's persisted billingMode and
    // overdraftFloor; options.minBalance is the engine's strict-mode floor.
    const settleFloor: Decimal =
      activeLease.billingMode === "strict"
        ? (options?.minBalance ?? ZERO)
        : (activeLease.overdraftFloor ?? ZERO);
    const maxDebit = Decimal.max(ZERO, balance.minus(settleFloor));
    let net = amount.minus(consume);
    if (net.gt(maxDebit)) {
      net = maxDebit;
      // Re-clamp consume so it never exceeds amount - net.
      if (net.lt(amount)) {
        consume = Decimal.min(consume, amount.minus(net));
      }
    }

    // Spend cap is ADVISORY at settle (work is done): record the strongest
    // breaching action, never block (interface plan §7). 'deny' surfaces as a
    // non-blocking signal the manager re-emits as credits.cap_reached.
    let capWarning: string | null = null;
    const userCaps = this.spendCaps
      .filter((c) => c.userId === userId && (c.model == null || c.model === model))
      .sort((a, b) => (a.action === "deny" ? 0 : 1) - (b.action === "deny" ? 0 : 1));
    for (const cap of userCaps) {
      const windowStart = this.capWindowStart(cap.type);
      const spend = this.spendInWindow(userId, windowStart, cap.model);
      if (
        spend.plus(net).gt(cap.limit) &&
        (capWarning === null || (capWarning !== "deny" && cap.action === "deny"))
      ) {
        capWarning = cap.action;
      }
    }

    if (consume.gt(0) && planId) {
      this.incrementUsageWindowSync(userId, planId, consume, periodStart);
    }

    this.balances.set(userId, balance.minus(net));

    const txMeta = metadata ? this.cleanMetadata(metadata) : {};
    if (model != null) txMeta["model"] = model;
    if (idempotencyKey) txMeta["idempotencyKey"] = idempotencyKey;
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

    activeLease.status = "settled";
    activeLease.settleTxId = txId;

    return {
      transactionId: txId,
      userId,
      amount: net,
      allowanceConsumed: consume,
      balanceAfter: balance.minus(net),
      idempotent: false,
      capWarning,
    };
  }

  async releaseLease(userId: string, leaseId: string): Promise<ReleaseResult> {
    const lease = this.reservations.get(leaseId);
    if (!lease || lease.userId !== userId) {
      return { leaseId, userId, released: false, reason: "not_found" };
    }
    if (lease.status === "settled") {
      return { leaseId, userId, released: false, reason: "already_settled" };
    }
    if (lease.status === "released") {
      return { leaseId, userId, released: false, reason: "already_released" };
    }
    lease.status = "released";
    return { leaseId, userId, released: true, reason: "released" };
  }

  async renewLease(userId: string, leaseId: string, ttlSeconds: number): Promise<LeaseResult> {
    const now = this.now();
    const lease = this.reservations.get(leaseId);
    if (
      !lease ||
      lease.userId !== userId ||
      lease.status === "released" ||
      lease.status === "settled"
    ) {
      return {
        leaseId,
        userId,
        amount: ZERO,
        available: ZERO,
        reservedTotal: ZERO,
        billingMode: "strict",
        expiresAt: "",
        error: "lease_not_found",
      };
    }
    if (lease.status === "expired" || lease.expiresAt <= now) {
      lease.status = "expired";
      return {
        leaseId,
        userId,
        amount: ZERO,
        available: ZERO,
        reservedTotal: ZERO,
        billingMode: lease.billingMode,
        expiresAt: "",
        error: "lease_expired",
      };
    }

    lease.expiresAt = new Date(now.getTime() + ttlSeconds * 1000);
    let reservedTotal = ZERO;
    for (const r of this.activeLeases(userId)) reservedTotal = reservedTotal.plus(r.amount);
    const balance = this.balance(userId);
    return {
      leaseId,
      userId,
      amount: lease.amount,
      available: balance.minus(reservedTotal),
      reservedTotal,
      billingMode: lease.billingMode,
      expiresAt: lease.expiresAt.toISOString(),
    };
  }

  async getAvailable(userId: string): Promise<AvailableResult> {
    const balance = this.balance(userId);
    let reserved = ZERO;
    for (const r of this.activeLeases(userId)) reserved = reserved.plus(r.amount);
    return { userId, balance, reserved, available: balance.minus(reserved) };
  }

  async getActivePricing(): Promise<PricingConfigResult | null> {
    // LOW hygiene fix: return the stored record id (stable), not a fresh randomUUID()
    // every call. A fresh id breaks any caching keyed on config id.
    const active = [...this.pricingHistory].reverse().find((h) => h.active);
    if (!active) return null;
    return { id: active.id, config: active.config, version: active.version };
  }

  async setActivePricing(config: PricingConfigData, label?: string | null): Promise<string> {
    // Deactivate all previous versions, then insert a new active entry.
    for (const h of this.pricingHistory) h.active = false;
    this.pricingVersion += 1;
    this.pricingLabel = label ?? null;
    const id = randomUUID();
    this.pricingHistory.push({
      id,
      version: this.pricingVersion,
      label: this.pricingLabel,
      active: true,
      config,
      createdAt: new Date().toISOString(),
    });
    // H1 fix: key planDefinitions by the config dict key (plan_key), not plan.id.
    // Python MemoryStore and SQL both resolve setUserPlan("u1","pro") by looking up
    // the dict key "pro"; JS must match or setUserPlan resolves to null/planless.
    if ("plans" in config && config.plans) {
      for (const [planKey, planData] of Object.entries(config.plans)) {
        this.planDefinitions.set(planKey, normalisePlanDefinition(planKey, planData));
      }
    }
    return id;
  }

  // H8: pricing history / activation — parity with Python base.py:293-312.

  async getPricingHistory(): Promise<PricingConfigHistoryItem[]> {
    return this.pricingHistory
      .slice()
      .reverse()
      .map((h) => ({
        id: h.id,
        version: h.version,
        label: h.label,
        active: h.active,
        createdAt: h.createdAt,
      }));
  }

  async getPricingConfig(version: number): Promise<PricingConfigResult | null> {
    const entry = this.pricingHistory.find((h) => h.version === version);
    if (!entry) return null;
    return { id: entry.id, config: entry.config, version: entry.version };
  }

  async activatePricing(version: number): Promise<string> {
    const entry = this.pricingHistory.find((h) => h.version === version);
    if (!entry) throw new Error(`pricing version ${version} not found`);
    for (const h of this.pricingHistory) h.active = false;
    entry.active = true;
    this.pricingVersion = entry.version;
    if ("plans" in entry.config && entry.config.plans) {
      for (const [planKey, planData] of Object.entries(entry.config.plans)) {
        this.planDefinitions.set(planKey, normalisePlanDefinition(planKey, planData));
      }
    }
    return entry.id;
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
      defaultBillingMode: planDef?.defaultBillingMode ?? "strict",
      perOperation: (planDef?.perOperation as GetUserPlanResult["perOperation"]) ?? {},
      maxConcurrent: planDef?.maxConcurrent ?? null,
      overdraftFloor: planDef?.overdraftFloor ?? null,
      allowancePeriod: planDef?.allowancePeriod ?? "calendar_month",
      planAssignedAt: planId ? (this.userPlanAssignedAt.get(userId) ?? null) : null,
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
    // WS9: record the assignment time as the anchor for non-calendar-month
    // allowance periods. Uses the SAME injectable clock as everything else so
    // tests can control it deterministically.
    this.userPlanAssignedAt.set(userId, this.now());
    return { userId, planId };
  }

  async checkAllowance(userId: string, periodStart?: Date | null): Promise<AllowanceResult> {
    // periodStart is unused here: MemoryStore already has direct access to
    // planAssignedAt and its own (injectable, test-controllable) clock, so it
    // self-resolves the window rather than trusting a caller-supplied date that
    // may have been computed against the manager's wall clock instead.
    void periodStart;
    const planId = this.userPlanMap.get(userId);
    if (!planId) {
      return { planId: "", allowanceRemaining: ZERO, periodStart: "", periodEnd: "" };
    }
    const planDef = this.planDefinitions.get(planId);
    if (!planDef) {
      return { planId: "", allowanceRemaining: ZERO, periodStart: "", periodEnd: "" };
    }
    const now = this.now();
    const allowancePeriod: AllowancePeriod = planDef.allowancePeriod ?? "calendar_month";
    const anchor = this.userPlanAssignedAt.get(userId) ?? null;
    const { start, end: exclusiveEnd } = resolveAllowanceWindow(now, allowancePeriod, anchor);
    // Preserve the pre-WS9 display convention: periodEnd is the LAST DAY of the
    // window (inclusive), i.e. one day before the resolver's exclusive end.
    const periodEnd = new Date(exclusiveEnd.getTime() - 86_400_000);
    const billingPeriod = start.toISOString().slice(0, 10);
    let usage = ZERO;
    for (const w of this.usageWindows) {
      if (w.userId === userId && w.planId === planId && w.billingPeriod === billingPeriod) {
        usage = usage.plus(w.usage);
      }
    }
    return {
      planId,
      allowanceRemaining: Decimal.max(planDef.freeAllowance.minus(usage), ZERO),
      periodStart: start.toISOString(),
      periodEnd: periodEnd.toISOString(),
    };
  }

  private incrementUsageWindowSync(
    userId: string,
    planId: string,
    amount: Decimal,
    periodStart?: Date | null,
  ): void {
    const billingPeriod = this.billingPeriod(periodStart);
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
    if ((origTx.type !== "usage" && origTx.type !== "team_usage") || origTx.amount.gte(0)) {
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

  /** Monthly team spend for a specific member: sum of team_usage debits this UTC month (H3). */
  private teamMonthSpent(teamId: string, userId: string): Decimal {
    const now = this.now();
    const windowStart = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), 1));
    let total = ZERO;
    for (const t of this.transactions) {
      if (t.userId !== userId) continue;
      if (t.type !== "team_usage") continue;
      if (t.amount.gte(0)) continue;
      if (t.metadata?.["teamId"] !== teamId) continue;
      if (t.createdAt >= windowStart) total = total.plus(t.amount.abs());
    }
    return total;
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

    // H3 fix: enforce spend cap against the monthly window, not lifetime totalSpent.
    // Python MemoryStore and SQL both use a monthly window (first day of UTC month).
    if (member.spendCap != null) {
      const monthSpent = this.teamMonthSpent(teamId, userId);
      if (monthSpent.plus(amount).gt(member.spendCap)) {
        return {
          transactionId: "",
          teamId,
          userId,
          amount: ZERO,
          teamBalanceAfter: team.balance,
          error: "spend_cap_exceeded",
        };
      }
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
