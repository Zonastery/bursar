import { randomUUID } from "crypto";
import Decimal from "decimal.js";
import { quantizeMoney } from "../expr.js";
import { StoreError } from "../errors.js";
import { resolveAllowanceWindow, resolveCalendarWindow } from "../allowance.js";
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
  FeatureLimit,
  FeatureLimitResult,
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
  TierBalance,
  TierBalancesResult,
  TierDefinition,
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
  const freeAllowanceRaw = (p["freeAllowance"] ?? p["free_allowance"]) as
    | number
    | string
    | undefined;
  const defaultBillingModeRaw = (p["defaultBillingMode"] ??
    p["billingMode"] ??
    p["default_billing_mode"] ??
    p["billing_mode"]) as string | undefined;
  const overdraftFloorRaw = (p["overdraftFloor"] ?? p["overdraft_floor"]) as
    | number
    | string
    | null
    | undefined;
  const rateOverridesRaw = (p["rateOverrides"] ?? p["rate_overrides"]) as
    | Record<string, string>
    | null
    | undefined;
  const perOperationRaw = (p["perOperation"] ?? p["per_operation"]) as
    | Record<string, unknown>
    | null
    | undefined;
  const maxConcurrentRaw = (p["maxConcurrent"] ?? p["max_concurrent"]) as number | null | undefined;
  const allowancePeriodRaw = (p["allowancePeriod"] ?? p["allowance_period"]) as
    | AllowancePeriod
    | undefined;
  const featureLimitsRaw = (p["featureLimits"] ?? p["feature_limits"]) as
    | Record<string, unknown>
    | null
    | undefined;

  const billingMode = (defaultBillingModeRaw === "overdraft" ? "overdraft" : "strict") as
    | "strict"
    | "overdraft";
  return {
    id: (p["id"] as string | undefined) ?? planKey,
    name: (p["name"] as string | undefined) ?? planKey,
    freeAllowance: freeAllowanceRaw != null ? new Decimal(freeAllowanceRaw) : ZERO,
    rateOverrides: rateOverridesRaw ?? null,
    features: (p["features"] as Record<string, unknown> | null | undefined) ?? null,
    featureLimits: normaliseFeatureLimitsMap(featureLimitsRaw),
    defaultBillingMode: billingMode,
    overdraftFloor: overdraftFloorRaw != null ? new Decimal(overdraftFloorRaw) : null,
    maxConcurrent: maxConcurrentRaw ?? null,
    perOperation: (perOperationRaw as PlanDefinition["perOperation"]) ?? undefined,
    allowancePeriod: allowancePeriodRaw ?? "calendar_month",
  };
}

/**
 * Normalise a raw `featureLimits`/`feature_limits` map (possibly snake_case,
 * from a JSON fixture or raw dict) into typed `FeatureLimit` records. Mirrors
 * `normalisePlanDefinition`'s camelCase/snake_case tolerance.
 */
function normaliseFeatureLimitsMap(
  raw: Record<string, unknown> | null | undefined,
): Record<string, FeatureLimit> | null {
  if (!raw) return null;
  const out: Record<string, FeatureLimit> = {};
  for (const [featureKey, rawLimit] of Object.entries(raw)) {
    const l = (rawLimit ?? {}) as Record<string, unknown>;
    const maxCallsRaw = (l["maxCalls"] ?? l["max_calls"]) as number | string | undefined;
    out[featureKey] = {
      maxCalls: maxCallsRaw != null ? Number(maxCallsRaw) : 0,
      period: (l["period"] as FeatureLimit["period"] | undefined) ?? "monthly",
      action: (l["action"] as FeatureLimit["action"] | undefined) ?? "deny",
    };
  }
  return out;
}

/**
 * Normalise a raw tier definition object (possibly snake_case from a JSON
 * fixture or raw dict) into a proper `TierDefinition`. Accepts both camelCase
 * and snake_case keys, mirroring `normalisePlanDefinition` (credit tiers).
 */
function normaliseTierDefinition(tierKey: string, raw: unknown): TierDefinition {
  const t = raw as Record<string, unknown>;
  const defaultTtlDaysRaw = (t["defaultTtlDays"] ?? t["default_ttl_days"]) as
    | number
    | string
    | null
    | undefined;
  const allowOverdraftRaw = (t["allowOverdraft"] ?? t["allow_overdraft"]) as boolean | undefined;
  const isDefaultRaw = (t["isDefault"] ?? t["is_default"]) as boolean | undefined;
  return {
    name: (t["name"] as string | undefined) ?? tierKey,
    priority: Number(t["priority"] ?? 0),
    expires: Boolean(t["expires"] ?? false),
    defaultTtlDays: defaultTtlDaysRaw != null ? Number(defaultTtlDaysRaw) : null,
    allowOverdraft: Boolean(allowOverdraftRaw ?? false),
    isDefault: Boolean(isDefaultRaw ?? false),
  };
}

/** One entry in the tier-priority walk order (deduct/settle/refund — credit tiers). */
interface TierOrderEntry {
  tierKey: string;
  priority: number;
  allowOverdraft: boolean;
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
  // Credit tiers: definitions keyed by tier key (config key); balances keyed by
  // a composite `${userId}:${tierKey}` string — JS Maps compare object/tuple
  // keys by reference, so a plain string key is required (repo CLAUDE.md).
  private tierDefinitions = new Map<string, TierDefinition>();
  private tierBalances = new Map<string, Decimal>();
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

  // ── Credit tiers: per-(user, tier) balance map ──────────────────────

  private tierBalanceKey(userId: string, tierKey: string): string {
    return `${userId}:${tierKey}`;
  }

  private getTierBalance(userId: string, tierKey: string): Decimal {
    return this.tierBalances.get(this.tierBalanceKey(userId, tierKey)) ?? ZERO;
  }

  private adjustTierBalance(userId: string, tierKey: string, delta: Decimal): void {
    const key = this.tierBalanceKey(userId, tierKey);
    this.tierBalances.set(key, (this.tierBalances.get(key) ?? ZERO).plus(delta));
  }

  /**
   * The tier-priority walk order (plan §4): configured tiers ascending by
   * priority (ties broken by tier key ascending), or the synthetic
   * `[{"default", priority: 0, allowOverdraft: true}]` when no tiers are
   * configured — PLUS any tier keys not in that list, appended last (the
   * config-drift safety net). `extraKeys`, when supplied, is the exact set of
   * "orphaned" keys to consider (e.g. a debit's own tier_breakdown keys, for
   * refund restoration); when omitted, orphaned keys are discovered from any
   * nonzero balance the user currently holds under an unconfigured tier key.
   */
  private tierWalkOrder(userId: string, extraKeys?: Iterable<string>): TierOrderEntry[] {
    const configured: TierOrderEntry[] = [...this.tierDefinitions.entries()]
      .map(([tierKey, def]) => ({
        tierKey,
        priority: def.priority,
        allowOverdraft: def.allowOverdraft ?? false,
      }))
      .sort((a, b) => a.priority - b.priority || a.tierKey.localeCompare(b.tierKey));

    const base: TierOrderEntry[] =
      configured.length > 0
        ? configured
        : [{ tierKey: "default", priority: 0, allowOverdraft: true }];

    const known = new Set(base.map((t) => t.tierKey));
    const orphaned = new Set<string>();
    if (extraKeys) {
      for (const k of extraKeys) if (!known.has(k)) orphaned.add(k);
    } else {
      const prefix = `${userId}:`;
      for (const [key, bal] of this.tierBalances) {
        if (!key.startsWith(prefix)) continue;
        const tierKey = key.slice(prefix.length);
        if (!known.has(tierKey) && !bal.isZero()) orphaned.add(tierKey);
      }
    }

    return [
      ...base,
      ...[...orphaned]
        .sort()
        .map((tierKey) => ({ tierKey, priority: Number.MAX_SAFE_INTEGER, allowOverdraft: false })),
    ];
  }

  /**
   * Resolve the `addCredits` target tier (plan §3):
   *  - no tiers configured ⇒ `tier` must be omitted/null/`"default"`.
   *  - tiers configured + explicit `tier` ⇒ must be a known key.
   *  - tiers configured + omitted `tier` ⇒ resolve via the `isDefault: true`
   *    tier, else throw (deliberately strict — never silently misroute money).
   */
  private resolveAddCreditsTier(tier?: string | null): string {
    if (this.tierDefinitions.size === 0) {
      if (tier != null && tier !== "default") {
        throw new StoreError(`tier_not_found: ${tier}`);
      }
      return "default";
    }
    if (tier != null) {
      if (!this.tierDefinitions.has(tier)) {
        throw new StoreError(`tier_not_found: ${tier}`);
      }
      return tier;
    }
    for (const [key, def] of this.tierDefinitions) {
      if (def.isDefault) return key;
    }
    throw new StoreError("tier_required");
  }

  /**
   * Reconcile a caller-supplied `expiresAt` against the resolved tier's
   * `expires` flag (plan §3):
   *  - no tiers configured ⇒ unchanged existing behaviour (no restriction).
   *  - non-expiring tier + explicit `expiresAt` ⇒ throw.
   *  - expiring tier + explicit `expiresAt` ⇒ validate `> now`, use it.
   *  - expiring tier + omitted `expiresAt` ⇒ use `defaultTtlDays` if set,
   *    else throw.
   */
  private resolveAddCreditsExpiry(tierKey: string, expiresAt?: Date | null): Date | null {
    if (this.tierDefinitions.size === 0) {
      return expiresAt ?? null;
    }
    const def = this.tierDefinitions.get(tierKey);
    if (!def) return expiresAt ?? null;
    if (!def.expires) {
      if (expiresAt != null) {
        throw new StoreError(`tier_does_not_expire: ${tierKey}`);
      }
      return null;
    }
    if (expiresAt != null) {
      if (expiresAt <= this.now()) {
        throw new StoreError(
          `invalid_expires_at: ${expiresAt.toISOString()} must be in the future`,
        );
      }
      return expiresAt;
    }
    if (def.defaultTtlDays != null) {
      return new Date(this.now().getTime() + def.defaultTtlDays * 86_400_000);
    }
    throw new StoreError("expires_at_required");
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
        "001_core_schema.sql",
        "002_credit_rpcs.sql",
        "003_pricing_config.sql",
        "004_plans.sql",
        "005_spend_caps.sql",
        "006_refunds_and_expiry.sql",
        "007_analytics.sql",
        "008_teams.sql",
        "009_deduct_and_leases.sql",
        "010_credit_tiers.sql",
        // NOTE: the contract doc for this feature named this migration
        // "011_feature_limits.sql", but "011_lazy_expiry.sql" already occupies
        // that slot in python/src/ducto/sql/ (a numbering collision between two
        // parallel work tracks) — using the next free number here instead.
        "012_feature_limits.sql",
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
    tier?: string | null,
    idempotencyKey?: string | null,
  ): Promise<AddCreditsResult> {
    // Idempotency (user-scoped): replay the original result, mirroring the
    // idiom used by deductWithAllowance/settleLease/refundCredits — scan for an
    // existing transaction for this user whose metadata.idempotencyKey matches.
    if (idempotencyKey) {
      const existing = this.transactions.find(
        (t) => t.userId === userId && t.metadata?.["idempotencyKey"] === idempotencyKey,
      );
      if (existing) {
        return {
          transactionId: existing.id,
          userId,
          amount: existing.amount,
          newBalance: this.balance(userId),
          lifetimePurchased: this.lifetime.get(userId) ?? ZERO,
          tier: (existing.metadata?.["tier"] as string | undefined) ?? "default",
          idempotent: true,
        };
      }
    }

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

    // Credit tiers: resolve the target tier and reconcile expiresAt against it.
    const resolvedTier = this.resolveAddCreditsTier(tier);
    const resolvedExpiresAt = this.resolveAddCreditsExpiry(resolvedTier, expiresAt);

    const amt = quantizeMoney(amount);
    const current = this.balance(userId);
    this.balances.set(userId, current.plus(amt));
    this.adjustTierBalance(userId, resolvedTier, amt);

    const lifetimeAdd = type === "purchase" ? amt : ZERO;
    this.lifetime.set(userId, (this.lifetime.get(userId) ?? ZERO).plus(lifetimeAdd));

    const txId = randomUUID();
    const txMeta = metadata ? this.cleanMetadata(metadata) : {};
    txMeta["tier"] = resolvedTier;
    if (idempotencyKey) txMeta["idempotencyKey"] = idempotencyKey;
    const tx: TransactionRecord = {
      id: txId,
      userId,
      amount: amt,
      type,
      metadata: txMeta,
      createdAt: this.now(),
      expiresAt: resolvedExpiresAt,
    };
    this.transactions.push(tx);

    return {
      transactionId: txId,
      userId,
      amount: amt,
      newBalance: this.balance(userId),
      lifetimePurchased: this.lifetime.get(userId) ?? ZERO,
      tier: resolvedTier,
      idempotent: false,
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
   * Tier-priority walk (plan §4), shared by `deductWithAllowance` and
   * `settleLease`: drains `net` from tier balances in priority order (exact
   * greedy Decimal walk, never a proportional split). If `net` exceeds the
   * sum of configured-tier balances (only reachable under a negative floor /
   * overdraft — the floor check already guarantees coverage otherwise), the
   * remainder routes to the `allowOverdraft` tier, else the max-priority
   * tier, else `"default"`. Returns a breakdown whose values sum exactly to
   * `net`.
   */
  private walkTiers(userId: string, net: Decimal): Record<string, Decimal> {
    const order = this.tierWalkOrder(userId);
    const breakdown: Record<string, Decimal> = {};
    let remaining = net;

    for (const t of order) {
      if (remaining.lte(0)) break;
      const bal = this.getTierBalance(userId, t.tierKey);
      const take = Decimal.min(bal, remaining);
      if (take.gt(0)) {
        breakdown[t.tierKey] = (breakdown[t.tierKey] ?? ZERO).plus(take);
        this.adjustTierBalance(userId, t.tierKey, take.negated());
        remaining = remaining.minus(take);
      }
    }

    if (remaining.gt(0)) {
      const overdraftTier = order.find((t) => t.allowOverdraft);
      const sink = overdraftTier
        ? overdraftTier.tierKey
        : order.length > 0
          ? order.reduce((best, t) => (t.priority > best.priority ? t : best)).tierKey
          : "default";
      breakdown[sink] = (breakdown[sink] ?? ZERO).plus(remaining);
      this.adjustTierBalance(userId, sink, remaining.negated());
    }

    return breakdown;
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
    const feature = options?.feature ?? null;
    const featureLimit = options?.featureLimit ?? null;
    const featurePeriodStart = options?.featurePeriodStart ?? null;

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
        featureLimitWarning: null,
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
          featureLimitWarning: null,
          // Idempotent replay echoes the ORIGINAL tier breakdown — never recomputed.
          tierBreakdown:
            (existing.metadata?.["tierBreakdown"] as Record<string, Decimal> | undefined) ?? null,
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
            featureLimitWarning: null,
            error: "cap_reached",
          };
        }
        if (capWarning === null) capWarning = cap.action;
      }
    }

    // (4b) Feature limit: ledger-derived count of prior committed `usage`
    // transactions tagged metadata.feature === feature within
    // [featurePeriodStart, featurePeriodEnd). Skipped entirely when no
    // feature/limit was resolved by the caller (manager).
    let featureLimitWarning: string | null = null;
    if (feature != null && featureLimit != null && featurePeriodStart != null) {
      const featurePeriodEnd = resolveCalendarWindow(featurePeriodStart, featureLimit.period).end;
      const count = this.featureUsageCount(userId, feature, featurePeriodStart, featurePeriodEnd);
      if (count >= featureLimit.maxCalls) {
        if (featureLimit.action === "deny") {
          return {
            transactionId: "",
            userId,
            amount: ZERO,
            allowanceConsumed: ZERO,
            balanceAfter: this.balance(userId),
            idempotent: false,
            capWarning: null,
            featureLimitWarning: null,
            error: "feature_limit_reached",
          };
        }
        featureLimitWarning = featureLimit.action;
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
        featureLimitWarning: null,
        error: "insufficient_credits",
      };
    }

    // (6) Commit: consume allowance, debit balance, insert ledger row.
    if (consume.gt(0) && planId) {
      this.incrementUsageWindowSync(userId, planId, consume, periodStart);
    }

    this.balances.set(userId, current.minus(net));
    const tierBreakdown = this.walkTiers(userId, net);

    const txMeta = metadata ? this.cleanMetadata(metadata) : {};
    if (idempotencyKey) txMeta["idempotencyKey"] = idempotencyKey;
    if (model != null) txMeta["model"] = model;
    // Tag metadata.feature whenever `feature` is given, regardless of whether a
    // limit is currently configured — this is what makes the ledger-derived
    // count accurate once a limit is enabled later.
    if (feature != null) txMeta["feature"] = feature;
    txMeta["allowanceConsumed"] = consume;
    txMeta["tierBreakdown"] = tierBreakdown;

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
      featureLimitWarning,
      tierBreakdown,
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
    const feature = options?.feature ?? null;
    const featureLimit = options?.featureLimit ?? null;
    const featurePeriodStart = options?.featurePeriodStart ?? null;

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

    // (3b) Deny-only feature limit at admission — same ledger-derived count as
    // deductWithAllowance/settleLease, but only ever enforces 'deny' (warn/notify
    // are not checked here: nothing has been charged yet, so there is nothing to
    // warn about). Skipped when no feature/limit was resolved by the caller.
    if (
      feature != null &&
      featureLimit != null &&
      featureLimit.action === "deny" &&
      featurePeriodStart != null
    ) {
      const featurePeriodEnd = resolveCalendarWindow(featurePeriodStart, featureLimit.period).end;
      const count = this.featureUsageCount(userId, feature, featurePeriodStart, featurePeriodEnd);
      if (count >= featureLimit.maxCalls) {
        return {
          leaseId: "",
          userId,
          amount: ZERO,
          available: ZERO,
          reservedTotal: ZERO,
          billingMode,
          expiresAt: "",
          error: "feature_limit_reached",
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
      featureLimitWarning: null,
      // Idempotent replay echoes the ORIGINAL tier breakdown — never recomputed.
      tierBreakdown:
        (tx.metadata?.["tierBreakdown"] as Record<string, Decimal> | undefined) ?? null,
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
        featureLimitWarning: null,
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
        featureLimitWarning: null,
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
        featureLimitWarning: null,
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
    const feature = options?.feature ?? null;
    const featureLimit = options?.featureLimit ?? null;
    const featurePeriodStart = options?.featurePeriodStart ?? null;

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
        featureLimitWarning: null,
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
        featureLimitWarning: null,
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

    // Feature limit is ADVISORY at settle (work is done, never blocks): a
    // breach — even a configured 'deny' — only sets featureLimitWarning
    // ("prefer deny", mirroring capWarning's resolution above).
    let featureLimitWarning: string | null = null;
    if (feature != null && featureLimit != null && featurePeriodStart != null) {
      const featurePeriodEnd = resolveCalendarWindow(featurePeriodStart, featureLimit.period).end;
      const count = this.featureUsageCount(userId, feature, featurePeriodStart, featurePeriodEnd);
      if (count >= featureLimit.maxCalls) {
        featureLimitWarning = featureLimit.action;
      }
    }

    if (consume.gt(0) && planId) {
      this.incrementUsageWindowSync(userId, planId, consume, periodStart);
    }

    this.balances.set(userId, balance.minus(net));
    const tierBreakdown = this.walkTiers(userId, net);

    const txMeta = metadata ? this.cleanMetadata(metadata) : {};
    if (model != null) txMeta["model"] = model;
    if (idempotencyKey) txMeta["idempotencyKey"] = idempotencyKey;
    // Tag metadata.feature whenever `feature` is given — this is what makes
    // this call countable by future checks (settle_lease is the ONLY place a
    // leased operation's usage transaction is inserted).
    if (feature != null) txMeta["feature"] = feature;
    txMeta["allowanceConsumed"] = consume;
    txMeta["tierBreakdown"] = tierBreakdown;

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
      featureLimitWarning,
      tierBreakdown,
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
    if ("tiers" in config && config.tiers) {
      for (const [tierKey, tierData] of Object.entries(config.tiers)) {
        this.tierDefinitions.set(tierKey, normaliseTierDefinition(tierKey, tierData));
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
    if ("tiers" in entry.config && entry.config.tiers) {
      for (const [tierKey, tierData] of Object.entries(entry.config.tiers)) {
        this.tierDefinitions.set(tierKey, normaliseTierDefinition(tierKey, tierData));
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
      featureLimits: planDef?.featureLimits ?? {},
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

    // Credit tiers: LIFO restoration (plan §5). The original debit's per-tier
    // breakdown drives allocation; legacy debits without a stored breakdown
    // (pre-tiers) fall back to the whole amount landing in "default".
    const originalBreakdown = (origTx.metadata?.["tierBreakdown"] as
      | Record<string, Decimal>
      | undefined) ?? { default: originalDebit };

    // tierRemaining[t] = original[t] - Σ(all PRIOR refunds' own breakdown[t]) —
    // derived fresh each time so repeated partial refunds compose correctly
    // without a separate running counter.
    const priorBreakdowns: Record<string, Decimal>[] = [];
    for (const t of this.transactions) {
      if (t.type === "refund" && t.referenceId === transactionId) {
        const b = t.metadata?.["tierBreakdown"] as Record<string, Decimal> | undefined;
        if (b) priorBreakdowns.push(b);
      }
    }
    const tierRemaining: Record<string, Decimal> = {};
    for (const [t, orig] of Object.entries(originalBreakdown)) {
      let consumed = ZERO;
      for (const b of priorBreakdowns) consumed = consumed.plus(b[t] ?? ZERO);
      tierRemaining[t] = orig.minus(consumed);
    }

    // Walk tiers in REVERSE priority order (highest-priority-number/last-drained
    // tier first), restoring only the tiers actually present in the original
    // breakdown.
    let toAllocate = refundAmount;
    const newBreakdown: Record<string, Decimal> = {};
    const reverseOrder = [
      ...this.tierWalkOrder(origTx.userId, Object.keys(originalBreakdown)),
    ].reverse();
    for (const t of reverseOrder) {
      if (toAllocate.lte(0)) break;
      const give = Decimal.min(tierRemaining[t.tierKey] ?? ZERO, toAllocate);
      if (give.gt(0)) {
        newBreakdown[t.tierKey] = give;
        toAllocate = toAllocate.minus(give);
        this.adjustTierBalance(origTx.userId, t.tierKey, give);
      }
    }

    const txMeta = metadata ? this.cleanMetadata(metadata) : {};
    if (reason) txMeta["reason"] = reason;
    txMeta["tierBreakdown"] = newBreakdown;

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
      tierBreakdown: newBreakdown,
    };
  }

  // ── Credit expiry ─────────────────────────────────────────────────────

  async sweepExpiredCredits(dryRun = false, userId?: string): Promise<SweepResult> {
    // ── critical section (synchronous; no awaits) ──
    const now = this.now();
    // Credit tiers: re-scoped from per-userId to per-(userId, tierKey), reading
    // `metadata.tier` off each grant (legacy records without it default to
    // "default"). Grouping key: `${userId} ${tierKey}`.
    const expiredByUserTier = new Map<string, Decimal>();
    const expiredTxsByUserTier = new Map<string, TransactionRecord[]>();

    // Find all expired, not-yet-swept grant transactions. When `userId` is
    // given (lazy-on-read expiry), restrict the scan to that user only —
    // omitted preserves the original global-sweep behaviour exactly.
    for (const tx of this.transactions) {
      if (userId !== undefined && tx.userId !== userId) continue;
      if (
        tx.expiresAt &&
        !tx.sweptAt && // H4: never re-sweep a previously swept grant
        (tx.type === "purchase" || tx.type === "adjustment")
      ) {
        if (tx.expiresAt <= now) {
          const tierKey = (tx.metadata?.["tier"] as string | undefined) ?? "default";
          const key = `${tx.userId} ${tierKey}`;
          expiredByUserTier.set(key, (expiredByUserTier.get(key) ?? ZERO).plus(tx.amount));
          const list = expiredTxsByUserTier.get(key) ?? [];
          list.push(tx);
          expiredTxsByUserTier.set(key, list);
        }
      }
    }

    let expiredCount = 0;
    let expiredAmount = ZERO;
    const expiredByTier: Record<string, Decimal> = {};

    for (const [key, totalExpired] of expiredByUserTier) {
      const sep = key.indexOf(" ");
      const userId = key.slice(0, sep);
      const tierKey = key.slice(sep + 1);
      // Clamp per-TIER balance (not aggregate) — the existing LEAST(...) clamp,
      // just re-scoped.
      const currentTierBalance = this.getTierBalance(userId, tierKey);
      const toExpire = Decimal.min(totalExpired, currentTierBalance);

      if (toExpire.gt(0)) {
        expiredCount++;
        expiredAmount = expiredAmount.plus(toExpire);
        expiredByTier[tierKey] = (expiredByTier[tierKey] ?? ZERO).plus(toExpire);

        if (!dryRun) {
          this.adjustTierBalance(userId, tierKey, toExpire.negated());
          this.balances.set(userId, this.balance(userId).minus(toExpire));

          // H4: mark swept grants so a second sweep reports zero.
          const txs = expiredTxsByUserTier.get(key) ?? [];
          for (const et of txs) {
            et.sweptAt = now;
            et.expiresAt = null;
          }

          const txId = randomUUID();
          this.transactions.push({
            id: txId,
            userId,
            amount: toExpire.negated(),
            type: "adjustment",
            metadata: { reason: "credit_expired", expiredAmount: toExpire, tier: tierKey },
            createdAt: now,
          });
        }
      }
    }

    return {
      expiredCount,
      expiredAmount,
      dryRun,
      expiredByTier: Object.keys(expiredByTier).length > 0 ? expiredByTier : null,
    };
  }

  // ── Credit tiers ─────────────────────────────────────────────────────

  async getCreditTiers(userId: string): Promise<TierBalancesResult> {
    if (this.tierDefinitions.size === 0) {
      // No tiers configured: synthesize a single "default" entry from the
      // aggregate balance so the API shape is uniform either way.
      return {
        userId,
        tiers: [
          {
            tierKey: "default",
            name: "default",
            priority: 0,
            expires: false,
            balance: this.balance(userId),
          },
        ],
        totalBalance: this.balance(userId),
      };
    }

    const tiers: TierBalance[] = [...this.tierDefinitions.entries()]
      .map(([tierKey, def]) => ({
        tierKey,
        name: def.name,
        priority: def.priority,
        expires: def.expires,
        balance: this.getTierBalance(userId, tierKey),
      }))
      .sort((a, b) => a.priority - b.priority || a.tierKey.localeCompare(b.tierKey));

    return { userId, tiers, totalBalance: this.balance(userId) };
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

  /**
   * Ledger-derived count of committed `usage` transactions tagged
   * `metadata.feature === feature` in `[periodStart, periodEnd)` — the
   * counting primitive shared by `deductWithAllowance`/`createLease`/
   * `settleLease`'s feature-limit enforcement and `checkFeatureLimit`.
   * Mirrors `spendInWindow`, replacing "sum of amount" with "count of rows".
   *
   * Deliberately does NOT filter on `amount < 0` (unlike `spendInWindow`,
   * which only cares about actual dollars spent): a call fully covered by
   * free allowance nets to `amount === 0` but is still one invocation of the
   * feature and must still count.
   *
   * `release_lease` never inserts a `usage` row (nothing to count) and
   * `refund_credits` never deletes the original row (the count is
   * unaffected) — both intentional, matching the SQL/Python design.
   */
  private featureUsageCount(
    userId: string,
    feature: string,
    periodStart: Date,
    periodEnd: Date,
  ): number {
    let count = 0;
    for (const t of this.transactions) {
      if (t.userId !== userId) continue;
      if (t.type !== "usage") continue;
      if ((t.metadata ?? {})["feature"] !== feature) continue;
      if (t.createdAt >= periodStart && t.createdAt < periodEnd) count += 1;
    }
    return count;
  }

  async checkFeatureLimit(
    userId: string,
    feature: string,
    maxCalls: number,
    periodStart: Date,
    periodEnd: Date,
  ): Promise<FeatureLimitResult> {
    const used = this.featureUsageCount(userId, feature, periodStart, periodEnd);
    return {
      userId,
      feature,
      limited: true,
      limit: maxCalls,
      used,
      remaining: Math.max(maxCalls - used, 0),
      periodStart: periodStart.toISOString(),
      periodEnd: periodEnd.toISOString(),
      // The manager overwrites `action` with the resolved FeatureLimit.action.
      action: null,
    };
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
