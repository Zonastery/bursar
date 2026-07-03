import Decimal from "decimal.js";
import {
  CapReachedError,
  ConcurrencyLimitError,
  ConfigError,
  FeatureLimitReachedError,
  FeatureNotEntitledError,
  InsufficientCreditsError,
  LeaseExpiredError,
  LeaseNotFoundError,
  PricingNotLoadedError,
  RefundError,
} from "./errors.js";
import type { PricingEngine } from "./engine.js";
import { PricingEngine as PricingEngineClass } from "./engine.js";
import type {
  AddCreditsResult,
  AggregateStats,
  AllowanceResult,
  AvailableResult,
  BalanceResult,
  BillingMode,
  CanAffordResult,
  CheckFeatureResult,
  CreditMetadata,
  DailySpendRow,
  DeductionResult,
  DeductWithAllowanceOptions,
  FeatureLimit,
  FeatureLimitResult,
  GetUserPlanResult,
  LeaseResult,
  OperationPolicy,
  PricingConfigData,
  RefundResult,
  ReleaseResult,
  SetupResult,
  SpendByModelRow,
  SpendByUserRow,
  SweepResult,
  TeamDeductionResult,
  TierBalancesResult,
  TopUserRow,
  UserTransactionRow,
} from "./types.js";
import type {
  ListTransactionsOptions,
  ListUsageEventsOptions,
  PaginatedTransactions,
} from "./types.js";
import type { CreditStore } from "./stores/credit-store.js";
import type { CreditEvent, CreditEventEmitter, CreditEventType } from "./stores/events.js";
import type { UsageMetrics } from "./metrics.js";
import { resolveAllowanceWindow, resolveCalendarWindow } from "./allowance.js";

/**
 * Default `low_balance` threshold multiplier (contract §6 / M18). The event
 * fires when a deduction crosses ``minBalance * LOW_BALANCE_MULTIPLIER`` from
 * above. Override via the ``lowBalance.thresholds`` constructor option to set
 * an absolute threshold (or multiple levels) instead.
 */
const LOW_BALANCE_MULTIPLIER = 2;

/**
 * Default lease TTL (seconds) for ``reserve``/``runBilled`` (interface plan §3).
 * Long batch/agentic jobs call {@link CreditManager.renew} before this elapses.
 */
const DEFAULT_LEASE_TTL_SECONDS = 600;

/**
 * Built-in financial-safety presets (interface plan §2). ``strict_prepaid``
 * keeps the floor ``>= 0`` (structural zero debt); ``overdraft`` permits a
 * negative floor and bills the full actual cost at settle.
 */
const POLICY_PRESETS = new Set<PolicyPreset>(["strict_prepaid", "overdraft"]);

/** A financial-safety constructor preset (interface plan §2). */
export type PolicyPreset = "strict_prepaid" | "overdraft";

/** Coerce a `Decimal | number` money input into a `Decimal`. */
function toDecimal(value: Decimal | number): Decimal {
  return value instanceof Decimal ? value : new Decimal(value);
}

/** A cost input: either usage metrics (priced via the engine) or a raw amount. */
type MetricsOrAmount = UsageMetrics | Decimal | number;

/** True when `value` is a raw money amount rather than a `UsageMetrics` object. */
function isAmount(value: MetricsOrAmount): value is Decimal | number {
  return value instanceof Decimal || typeof value === "number";
}

/**
 * Multi-level ``credits.low_balance`` configuration (WS7 — collapses the
 * former ``lowBalanceThreshold`` / ``lowBalanceThresholds`` / ``onLowBalance``
 * trio into one option). Each threshold is edge-triggered once per descent and
 * re-arms after a top-up (interface plan §6). When ``lowBalance`` is omitted
 * entirely, the manager falls back to ``[minBalance * LOW_BALANCE_MULTIPLIER]``,
 * resolved lazily against the loaded engine.
 */
export interface LowBalanceConfig {
  thresholds?: (Decimal | number)[] | null;
  onTrigger?: ((event: CreditEvent) => void | Promise<void>) | null;
}

/** Optional behavioural knobs for the manager. */
export interface CreditManagerOptions {
  /**
   * Financial-safety preset for planless users (interface plan §2). Defaults to
   * ``"strict_prepaid"``. Per-plan / per-call policy layers on top of this.
   */
  policy?: PolicyPreset;
  /** Negative balance floor for the ``overdraft`` preset (interface plan §1). */
  overdraftFloor?: Decimal | number | null;
  /** Default ``maxConcurrent`` lease bound applied by the preset. */
  maxConcurrent?: number | null;
  /**
   * ``credits.low_balance`` thresholds and handler (interface plan §6). When
   * omitted, the manager uses ``[engine.minBalance * 2]`` (documented default,
   * M18), resolved lazily. The event is **edge-triggered**: each configured
   * level fires only on the deduction that crosses it from above, never
   * repeatedly while already below it, and re-arms on top-up.
   */
  lowBalance?: LowBalanceConfig | null;
  /** Default lease TTL (seconds) for ``reserve``/``runBilled`` (default 600). */
  defaultTtlSeconds?: number;
  /**
   * Enable lazy-on-read credit expiry (default ``false``, unchanged behaviour).
   *
   * Allowance windows and lease TTLs are already lazy-on-read (checked via
   * `expiresAt`/window filters at read time); credit expiry otherwise requires
   * an explicit ``sweepExpiredCredits`` call (e.g. from a cron job). When
   * ``true``, the manager transparently sweeps a user's own expired credits
   * (scoped to that user — never a global sweep) before ``getBalance``,
   * ``getCreditTiers``, ``deduct``, ``deductFixed``, ``deductTeam``,
   * ``reserve``, and ``settle`` so a caller never needs to run a cron job.
   */
  lazyExpiry?: boolean;
}

/** Options for {@link CreditManager.reserve}. */
export interface ReserveOptions {
  operationType?: string;
  billingMode?: BillingMode | null;
  requiredFeature?: string | null;
  ttl?: number | null;
  metadata?: CreditMetadata | null;
  /** Named feature to enforce/tag a per-feature invocation-count limit for (independent of `requiredFeature`). */
  feature?: string | null;
}

/** Options for {@link CreditManager.settle}. */
export interface SettleOptions {
  idempotencyKey?: string | null;
  metadata?: CreditMetadata | null;
  /**
   * The same feature name supplied to `reserve` — re-supplied at settle time
   * for accurate per-feature invocation-count accounting, exactly like `model`
   * is re-supplied at settle for per-model spend-cap accuracy.
   */
  feature?: string | null;
}

/** Options for {@link CreditManager.canAfford}. */
export interface CanAffordOptions {
  requiredFeature?: string | null;
  billingMode?: BillingMode | null;
  operationType?: string;
}

/** Options for {@link CreditManager.grantSubscriptionCycle}. */
export interface GrantSubscriptionCycleOptions {
  /** Target credit tier for the granted cycle. Defaults to ``"subscription"``. */
  tier?: string;
  /** Explicit expiry for the granted cycle. Mutually exclusive with ``ttlDays``. */
  expiresAt?: Date;
  /** TTL in days from now for the granted cycle. Mutually exclusive with ``expiresAt``. */
  ttlDays?: number;
  /**
   * Expire any remaining balance in ``tier`` before granting the new cycle
   * (default ``true``) — the usual "use it or lose it" subscription semantics.
   * When ``false``, the new cycle's credits are added on top of any leftover
   * balance.
   */
  replacePrior?: boolean;
  /** Optional plan to assign after granting (e.g. resolved from a webhook's price/plan id). */
  planKey?: string;
  /**
   * Replay-safe idempotency key — safe to call again with the same key on
   * webhook redelivery (e.g. Stripe's at-least-once delivery guarantee).
   */
  idempotencyKey?: string;
  metadata?: CreditMetadata | null;
}

/** Options for {@link CreditManager.runBilled}. */
export interface RunBilledOptions<T> {
  estimate: MetricsOrAmount;
  doWork: () => Promise<{ result: T; actual: MetricsOrAmount }>;
  operationType?: string;
  billingMode?: BillingMode | null;
  requiredFeature?: string | null;
  idempotencyKey?: string | null;
  ttl?: number | null;
  /** Named feature to enforce/tag a per-feature invocation-count limit for (independent of `requiredFeature`). */
  feature?: string | null;
}

/**
 * Orchestrates credit operations.
 *
 * The deduction path is a single atomic, idempotency-keyed store call
 * (``deductWithAllowance``) that consumes free allowance, enforces spend caps,
 * applies the balance floor and debits the net amount in one transaction
 * (contract §2). The manager is a thin layer that calculates the cost, maps
 * the store's typed ``error`` codes to exceptions, and emits lifecycle events
 * **only after** the operation has succeeded (contract §6).
 *
 * Optionally accepts a ``CreditEventEmitter`` to emit lifecycle events
 * (deducted, deduct_failed, added, refunded, refund_failed, expired,
 * cap_reached, cap_warning, low_balance).
 */
export class CreditManager {
  private store: CreditStore;
  private engine: PricingEngine | null = null;
  private emitter: CreditEventEmitter | null = null;
  // Financial-safety policy (interface plan §1/§2): `policy` is the preset default
  // used for planless users; per-plan / per-call policy layers on top.
  private policy: PolicyPreset;
  private overdraftFloor: Decimal | null;
  private defaultMaxConcurrent: number | null;
  private defaultTtl: number;
  // Multi-level low_balance thresholds (interface plan §6), sorted high→low.
  // Populated from `options.lowBalance.thresholds` (WS7); `null` means
  // "unconfigured" — falls back to `[minBalance * LOW_BALANCE_MULTIPLIER]` lazily.
  private lowBalanceThresholds: Decimal[] | null;
  private onLowBalance: ((event: CreditEvent) => void | Promise<void>) | null;
  // Lazy-on-read credit expiry (closes the gap with allowance windows/lease
  // TTLs, which are already lazy-on-read). Default `false` — unchanged
  // behaviour; explicit `sweepExpiredCredits`/cron remains required.
  private lazyExpiry: boolean;
  // Edge-trigger state: per-user set of thresholds currently breached ("below"),
  // keyed by `.toString()`. A level re-arms only after the balance climbs back
  // above it (a top-up).
  private lbBelow = new Map<string, Set<string>>();

  constructor(
    store: CreditStore,
    engine?: PricingEngine | null,
    emitter?: CreditEventEmitter | null,
    options?: CreditManagerOptions | null,
  ) {
    const policy = options?.policy ?? "strict_prepaid";
    if (!POLICY_PRESETS.has(policy)) {
      throw new ConfigError(
        `unknown policy preset '${policy}'; expected one of ${[...POLICY_PRESETS].sort().join(", ")}`,
      );
    }
    this.store = store;
    if (engine) this.engine = engine;
    if (emitter) this.emitter = emitter;
    this.policy = policy;
    this.overdraftFloor =
      options?.overdraftFloor != null ? toDecimal(options.overdraftFloor) : null;
    this.defaultMaxConcurrent = options?.maxConcurrent ?? null;
    this.defaultTtl = options?.defaultTtlSeconds ?? DEFAULT_LEASE_TTL_SECONDS;
    this.lowBalanceThresholds = options?.lowBalance?.thresholds?.length
      ? options.lowBalance.thresholds.map(toDecimal).sort((a, b) => b.comparedTo(a))
      : null;
    this.onLowBalance = options?.lowBalance?.onTrigger ?? null;
    this.lazyExpiry = options?.lazyExpiry ?? false;
  }

  /** Emit a credit lifecycle event. No-op if no emitter is configured. */
  private emit(type: CreditEventType, userId: string, data?: Record<string, unknown>): void {
    this.emitter?.emit({ type, timestamp: new Date(), userId, data });
  }

  /** The configured min-balance floor as a `Decimal` (defaults to 0). */
  private minBalanceDecimal(): Decimal {
    return new Decimal(this.engine?.minBalance ?? 0);
  }

  /**
   * Resolve the single `low_balance` threshold used by the ``deduct`` fast path:
   * the lowest configured level when ``lowBalance.thresholds`` is set, otherwise
   * ``minBalance * LOW_BALANCE_MULTIPLIER`` (documented default).
   */
  private resolveLowBalanceThreshold(): Decimal {
    if (this.lowBalanceThresholds) {
      // `lowBalanceThresholds` is sorted high→low; the last element is lowest.
      return this.lowBalanceThresholds[this.lowBalanceThresholds.length - 1];
    }
    return this.minBalanceDecimal().times(LOW_BALANCE_MULTIPLIER);
  }

  /** Run bundled SQL migrations through the store. */
  async setup(): Promise<SetupResult> {
    return await this.store.setup();
  }

  /** Load pricing from a PricingConfigData or raw dict and sync it. */
  async publishPricingFromDict(data: PricingConfigData | Record<string, unknown>): Promise<void> {
    const raw = data as Record<string, unknown>;
    this.engine = PricingEngineClass.fromDict(raw);
    await this.store.setActivePricing(data as PricingConfigData);
  }

  /** Load the active pricing config from the store. */
  async loadPricingFromStore(): Promise<void> {
    const active = await this.store.getActivePricing();
    if (!active) throw new PricingNotLoadedError("no active pricing config in store");

    const { models, tools, search, cache, fixed, minBalance, plans } = active.config;
    const engineDict: Record<string, unknown> = {
      models,
      tools: tools ?? { _default: "tool_calls * 0" },
      search: search ?? null,
      cache: cache ?? null,
      fixed: fixed ?? {},
      minBalance: minBalance ?? 0,
      ...(plans ? { plans } : {}),
    };

    this.engine = PricingEngineClass.fromDict(engineDict);
  }

  /**
   * Publish new pricing and update the engine in one call.
   *
   * H10: the store write is now **awaited** (was a fire-and-forget `void`), so
   * a persistence failure surfaces to the caller instead of becoming an
   * unhandled promise rejection.
   */
  async publishPricing(config: PricingConfigData, label?: string | null): Promise<void> {
    const { models, tools, search, cache, fixed, minBalance, plans } = config;
    const raw: Record<string, unknown> = {
      models,
      tools: tools ?? { _default: "tool_calls * 0" },
      search: search ?? null,
      cache: cache ?? null,
      fixed: fixed ?? {},
      minBalance: minBalance ?? 0,
      ...(plans ? { plans } : {}),
    };
    this.engine = PricingEngineClass.fromDict(raw);
    await this.store.setActivePricing(config, label);
  }

  /** The current PricingEngine, or null if not loaded. */
  get pricingEngine(): PricingEngine | null {
    return this.engine;
  }

  /** Fetch a user's current plan (including feature entitlements). */
  async getUserPlan(userId: string): Promise<GetUserPlanResult> {
    return this.store.getUserPlan(userId);
  }

  /**
   * Set a user's subscription plan and emit ``credits.plan_changed``.
   *
   * The store call is awaited so a persistence failure surfaces to the caller.
   * The event is emitted only after the store write succeeds (contract §6).
   */
  async setUserPlan(userId: string, planKey: string): Promise<void> {
    await this.store.setUserPlan(userId, planKey);
    this.emit("credits.plan_changed", userId, {
      userId,
      planKey,
      timestamp: new Date().toISOString(),
    });
  }

  /**
   * Unset a user's subscription plan. Clears `plan_id` and `plan_assigned_at`
   * so the allowance period is effectively paused. Call {@link setUserPlan} to
   * re-assign and re-anchor the allowance window.
   */
  async unsetUserPlan(userId: string): Promise<void> {
    await this.store.unsetUserPlan(userId);
    this.emit("credits.plan_changed", userId, {
      userId,
      planKey: null,
      timestamp: new Date().toISOString(),
    });
  }

  /**
   * Check whether a user's plan has a specific feature entitlement.
   *
   * Passthrough to the store, which distinguishes *presence* from *truthiness*
   * (numeric `0` / `""` count as present; only `null`/`undefined`/`false`/absent
   * read as missing — contract §5 / M6).
   */
  async checkFeature(userId: string, feature: string): Promise<CheckFeatureResult> {
    return this.store.checkFeature(userId, feature);
  }

  /**
   * Advisory, non-locking read of a per-feature invocation-count limit (UI only).
   *
   * Resolves the `FeatureLimit` (if any) from the user's plan and the
   * calendar-aligned window, then delegates counting to the store. Returns
   * `limited: false` (zeroed fields, `action: null`) when no `FeatureLimit`
   * is configured for `feature` on the user's plan — never used for
   * admission control; that is exclusively the atomic check-and-increment
   * inside `deduct`/`reserve`.
   */
  async checkFeatureLimit(userId: string, feature: string): Promise<FeatureLimitResult> {
    const { limit, periodStart, periodEnd } = await this.resolveFeatureLimit(userId, feature);
    if (limit == null || periodStart == null || periodEnd == null) {
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
    const result = await this.store.checkFeatureLimit(
      userId,
      feature,
      limit.maxCalls,
      periodStart,
      periodEnd,
    );
    // The store only counts; the manager (which owns plan lookup) overrides
    // `limited`/`action` from the resolved FeatureLimit (mirrors how
    // checkAllowance overrides periodEnd after the store call).
    return { ...result, limited: true, action: limit.action };
  }

  /**
   * Sweep a single user's own expired credits when ``options.lazyExpiry`` is
   * enabled — no-op otherwise. Mirrors the fact that allowance windows and
   * lease TTLs are already lazy-on-read; this closes the same gap for the
   * stored aggregate expiry balance without requiring a cron job.
   */
  private async maybeLazyExpire(userId: string): Promise<void> {
    if (!this.lazyExpiry) return;
    await this.runSweep(false, userId);
  }

  /** Get a user's current credit balance. */
  async getBalance(userId: string): Promise<BalanceResult> {
    await this.maybeLazyExpire(userId);
    return await this.store.getBalance(userId);
  }

  /** Fetch a single credit transaction by ID. Returns null when not found. */
  async getTransaction(userId: string, transactionId: string): Promise<UserTransactionRow | null> {
    return await this.store.getTransaction(userId, transactionId);
  }

  /** Add credits to a user's account. */
  async addCredits(
    userId: string,
    amount: Decimal | number,
    options?: {
      type?: string;
      metadata?: CreditMetadata | null;
      expiresAt?: Date | null;
      /** Target credit tier (credit tiers); omitted resolves to the config's default tier. */
      tier?: string | null;
      /** Replay-safe idempotency key (parity with `deduct`/`settle`/`refund`). */
      idempotencyKey?: string | null;
    },
  ): Promise<AddCreditsResult> {
    const type = options?.type ?? "adjustment";
    const result = await this.store.addCredits(
      userId,
      toDecimal(amount),
      type,
      options?.metadata,
      options?.expiresAt,
      options?.tier,
      options?.idempotencyKey,
    );
    this.emit("credits.added", userId, {
      transactionId: result.transactionId,
      amount: result.amount,
      newBalance: result.newBalance,
      type,
      idempotent: result.idempotent ?? false,
    });
    // Re-arm multi-level low_balance: any level the topped-up balance is now back
    // above can fire again on the next descent (interface plan §6).
    if (this.lowBalanceThresholds) {
      const below = this.lbBelow.get(userId) ?? new Set<string>();
      this.lbBelow.set(userId, below);
      for (const t of this.lowBalanceThresholds) {
        if (result.newBalance.gt(t)) below.delete(t.toString());
      }
    }
    return result;
  }

  /**
   * Grant one billing-cycle's worth of credits — idempotent-safe for a
   * payment-provider webhook handler (Stripe, etc. — bursar stays
   * provider-agnostic) to call even on webhook redelivery.
   *
   * 1. At most one of ``expiresAt``/``ttlDays`` may be given (throws
   *    ``ConfigError`` otherwise).
   * 2. When ``ttlDays`` is given, ``expiresAt = now + ttlDays`` days.
   * 3. When ``replacePrior`` (default ``true``), any remaining balance in
   *    ``tier`` is expired immediately via a direct ``store.addCredits``
   *    negative adjustment — naturally idempotent (a replay finds the tier
   *    already at zero and skips the call).
   * 4. The new cycle is granted via a direct ``store.addCredits`` call
   *    (bypassing {@link addCredits} so only ``credits.cycle_renewed`` fires,
   *    not a duplicate ``credits.added``), threading ``idempotencyKey`` so a
   *    redelivered webhook replays the prior grant instead of double-crediting.
   * 5. When ``planKey`` is given, assigns it via {@link setUserPlan}.
   * 6. Emits ``credits.cycle_renewed`` and returns the grant result.
   */
  async grantSubscriptionCycle(
    userId: string,
    amount: Decimal | number,
    options?: GrantSubscriptionCycleOptions,
  ): Promise<AddCreditsResult> {
    if (options?.expiresAt != null && options?.ttlDays != null) {
      throw new ConfigError(
        "grantSubscriptionCycle: specify at most one of 'expiresAt' or 'ttlDays', not both",
      );
    }
    const tier = options?.tier ?? "subscription";
    const replacePrior = options?.replacePrior ?? true;
    const expiresAt: Date | undefined =
      options?.ttlDays != null
        ? new Date(Date.now() + options.ttlDays * 86_400_000)
        : options?.expiresAt;
    const amountDec = toDecimal(amount);

    // Snapshot the tier's leftover balance and lifetimePurchased BEFORE granting.
    // A redelivered webhook must be a full no-op -- including skipping the
    // replace-prior wipe below -- not just avoiding a double-grant. AddCreditsResult
    // carries no reliable cross-store "was this a replay" flag (Postgres/Supabase
    // never populate `idempotent`), so a genuine new grant is detected after the
    // fact by checking whether lifetimePurchased actually moved by `amountDec`; an
    // idempotent replay leaves it unchanged.
    let priorLeftover = new Decimal(0);
    let preLifetimePurchased = new Decimal(0);
    if (replacePrior) {
      const tiersBefore = await this.getCreditTiers(userId);
      const current = tiersBefore.tiers.find((t) => t.tierKey === tier);
      if (current) priorLeftover = current.balance;
      preLifetimePurchased = (await this.getBalance(userId)).lifetimePurchased;
    }

    let result = await this.store.addCredits(
      userId,
      amountDec,
      "purchase",
      options?.metadata,
      expiresAt,
      tier,
      options?.idempotencyKey,
    );

    const isFreshGrant = result.lifetimePurchased.minus(preLifetimePurchased).eq(amountDec);
    if (replacePrior && isFreshGrant && priorLeftover.gt(0)) {
      await this.store.addCredits(
        userId,
        priorLeftover.negated(),
        "adjustment",
        { reason: "cycle_replaced" },
        undefined,
        tier,
      );
      // Reflect the post-replace balance so the returned result is accurate
      // (the grant call above only knows the pre-replace balance).
      result = { ...result, newBalance: (await this.getBalance(userId)).balance };
    }

    if (options?.planKey) {
      await this.setUserPlan(userId, options.planKey);
    }

    this.emit("credits.cycle_renewed", userId, {
      transactionId: result.transactionId,
      amount: amountDec,
      newBalance: result.newBalance,
      tier,
      planKey: options?.planKey ?? null,
      idempotencyKey: options?.idempotencyKey ?? null,
    });

    return result;
  }

  // ── Lease lifecycle: atomic admission (interface plan §3/§4) ────────

  /** The default {@link OperationPolicy} from the constructor preset (§2). */
  private presetPolicy(): OperationPolicy {
    if (this.policy === "overdraft") {
      return {
        billingMode: "overdraft",
        maxConcurrent: this.defaultMaxConcurrent,
        overdraftFloor: this.overdraftFloor ?? new Decimal(0),
      };
    }
    return {
      billingMode: "strict",
      maxConcurrent: this.defaultMaxConcurrent,
      overdraftFloor: null,
    };
  }

  /**
   * Resolve the effective policy: explicit arg → per-op → plan → preset (§1).
   *
   * A planless user (``planId`` is null) always gets the constructor preset, never
   * silently unlimited (resolves M1). A user *with* a plan gets the plan default,
   * then any ``perOperation`` override, then the explicit per-call ``billingMode``.
   */
  private async resolvePolicy(
    userId: string,
    operationType: string,
    billingModeOverride?: BillingMode | null,
  ): Promise<OperationPolicy> {
    let policy = this.presetPolicy();

    let plan: GetUserPlanResult | null;
    try {
      plan = await this.store.getUserPlan(userId);
    } catch {
      // A store outage shouldn't crash admission — fall back to the preset.
      plan = null;
    }

    if (plan && plan.planId) {
      policy = {
        billingMode: plan.defaultBillingMode ?? "strict",
        maxConcurrent: plan.maxConcurrent != null ? plan.maxConcurrent : policy.maxConcurrent,
        overdraftFloor: plan.overdraftFloor != null ? plan.overdraftFloor : policy.overdraftFloor,
      };
      const op = plan.perOperation?.[operationType];
      if (op) {
        policy = {
          billingMode: op.billingMode,
          maxConcurrent: op.maxConcurrent != null ? op.maxConcurrent : policy.maxConcurrent,
          overdraftFloor: op.overdraftFloor != null ? op.overdraftFloor : policy.overdraftFloor,
        };
      }
    }

    if (billingModeOverride != null) {
      policy = { ...policy, billingMode: billingModeOverride };
    }
    return policy;
  }

  /** Admission floor for a policy: ``overdraftFloor`` (≤0) or ``minBalance`` (≥0). */
  private resolveFloor(policy: OperationPolicy): Decimal {
    if (policy.billingMode === "overdraft") {
      return policy.overdraftFloor ?? new Decimal(0);
    }
    return this.minBalanceDecimal();
  }

  /**
   * Resolve the free-allowance period start for a user (WS9c).
   *
   * Fast path: a ``calendar_month`` plan (the default, and the vast majority of
   * users) needs no computation — returns ``null`` and the store falls back to
   * its own calendar-month window, exactly matching pre-WS9 behaviour. Only
   * ``rolling_30d``/``anniversary`` plans pay the cost of resolving a window,
   * anchored at the plan's ``planAssignedAt``.
   */
  private async resolveAllowancePeriodStart(userId: string): Promise<Date | null> {
    let plan: GetUserPlanResult | null;
    try {
      plan = await this.store.getUserPlan(userId);
    } catch {
      return null;
    }
    if (!plan || !plan.allowancePeriod || plan.allowancePeriod === "calendar_month") {
      return null;
    }
    const { start } = resolveAllowanceWindow(
      new Date(),
      plan.allowancePeriod,
      plan.planAssignedAt ?? null,
    );
    return start;
  }

  /**
   * Resolve the `FeatureLimit` (if any) configured for `feature` on the
   * user's plan, plus the calendar-aligned `[start, end)` window it applies
   * to (via {@link resolveCalendarWindow}).
   *
   * Returns `{ limit: null, periodStart: null, periodEnd: null }` when
   * `feature` is omitted or no limit is configured for it — the store then
   * skips enforcement entirely (mirrors `resolveAllowancePeriodStart`).
   */
  private async resolveFeatureLimit(
    userId: string,
    feature?: string | null,
  ): Promise<{ limit: FeatureLimit | null; periodStart: Date | null; periodEnd: Date | null }> {
    if (feature == null) return { limit: null, periodStart: null, periodEnd: null };
    let plan: GetUserPlanResult | null;
    try {
      plan = await this.store.getUserPlan(userId);
    } catch {
      return { limit: null, periodStart: null, periodEnd: null };
    }
    const limit = plan?.featureLimits?.[feature] ?? null;
    if (limit == null) return { limit: null, periodStart: null, periodEnd: null };
    const { start, end } = resolveCalendarWindow(new Date(), limit.period);
    return { limit, periodStart: start, periodEnd: end };
  }

  /**
   * Compute the credit cost and model from metrics, or pass a raw amount through.
   *
   * For {@link UsageMetrics} the cost is ``engine.calculate(...).total`` (exact
   * `Decimal`, no truncation); a raw amount is used as-is with no model.
   */
  private costOf(metricsOrAmount: MetricsOrAmount): { amount: Decimal; model: string | null } {
    if (isAmount(metricsOrAmount)) {
      return { amount: toDecimal(metricsOrAmount), model: null };
    }
    if (!this.engine) {
      throw new PricingNotLoadedError(
        "pricing not loaded: call loadPricingFromStore or publishPricing first",
      );
    }
    const breakdown = this.engine.calculate(metricsOrAmount);
    return { amount: breakdown.total, model: metricsOrAmount.model ?? null };
  }

  /** Map a store business code to the coherent typed exception (M2). */
  private raiseLeaseError(error: string, userId: string, amount: Decimal): never {
    switch (error) {
      case "concurrency_limit":
        throw new ConcurrencyLimitError(`Concurrency limit reached. user=${userId}`);
      case "cap_reached":
        throw new CapReachedError(`Spend cap exceeded. user=${userId}, requested=${amount}`);
      case "feature_limit_reached":
        throw new FeatureLimitReachedError(`Feature limit reached. user=${userId}`);
      case "feature_not_entitled":
        throw new FeatureNotEntitledError(`Feature not entitled. user=${userId}`);
      case "insufficient_credits":
        throw new InsufficientCreditsError(
          `Insufficient credits. user=${userId}, requested=${amount}`,
        );
      case "lease_expired":
        throw new LeaseExpiredError(`Lease expired. user=${userId}`);
      case "lease_not_found":
      case "not_found":
        throw new LeaseNotFoundError(`Lease not found. user=${userId}`);
      case "invalid_amount":
        throw new RangeError(`Invalid amount: ${amount}`);
      default:
        throw new InsufficientCreditsError(`Operation failed: ${error}. user=${userId}`);
    }
  }

  /**
   * Atomically acquire a lease — the only admission control (D4).
   *
   * Resolves the effective policy, enforces ``requiredFeature``, sizes the hold
   * from ``metricsOrAmount`` (worst-case in strict, estimate in overdraft — the
   * caller chooses what to pass), and calls the store's atomic ``createLease``. On
   * any business failure throws the coherent typed exception; on success emits
   * ``credits.reserved`` and returns the {@link LeaseResult}.
   */
  async reserve(
    userId: string,
    metricsOrAmount: MetricsOrAmount,
    options?: ReserveOptions,
  ): Promise<LeaseResult> {
    await this.maybeLazyExpire(userId);
    const operationType = options?.operationType ?? "usage";
    const requiredFeature = options?.requiredFeature ?? null;

    if (requiredFeature != null) {
      const check = await this.store.checkFeature(userId, requiredFeature);
      if (!check.hasFeature) {
        throw new FeatureNotEntitledError(
          `Feature '${requiredFeature}' not entitled. user=${userId}`,
        );
      }
    }

    const policy = await this.resolvePolicy(userId, operationType, options?.billingMode);
    const floor = this.resolveFloor(policy);
    const { amount, model } = this.costOf(metricsOrAmount);
    const ttlSeconds = options?.ttl != null ? options.ttl : this.defaultTtl;
    const periodStart = await this.resolveAllowancePeriodStart(userId);
    const feature = options?.feature ?? null;
    const { limit: featureLimit, periodStart: featurePeriodStart } = await this.resolveFeatureLimit(
      userId,
      feature,
    );

    const result = await this.store.createLease(userId, amount, operationType, {
      billingMode: policy.billingMode,
      floor,
      maxConcurrent: policy.maxConcurrent,
      ttlSeconds,
      model,
      overdraftFloor: policy.overdraftFloor,
      metadata: options?.metadata,
      periodStart,
      feature,
      featureLimit,
      featurePeriodStart,
    });

    if (result.error) {
      this.emit("credits.deduct_failed", userId, {
        error: result.error,
        amount,
        stage: "reserve",
        operationType,
      });
      this.raiseLeaseError(result.error, userId, amount);
    }

    this.emit("credits.reserved", userId, {
      leaseId: result.leaseId,
      amount: result.amount,
      available: result.available,
      billingMode: result.billingMode,
      operationType,
      expiresAt: result.expiresAt,
    });
    return result;
  }

  /**
   * Charge the ACTUAL cost against a lease and finalize it (D5).
   *
   * De-clamped: bills the full actual cost even if it exceeds the lease hold
   * (overdraft). Never blocks on floor/cap at settle — a cap breach surfaces as a
   * non-blocking ``credits.cap_warning``/``credits.cap_reached`` signal. Emits
   * ``credits.deducted``, then multi-level ``credits.low_balance`` and a
   * ``credits.overdraft`` signal if the balance went negative.
   */
  async settle(
    userId: string,
    leaseId: string,
    metricsOrAmount: MetricsOrAmount,
    options?: SettleOptions,
  ): Promise<DeductionResult> {
    await this.maybeLazyExpire(userId);
    const idempotencyKey = options?.idempotencyKey ?? null;
    const { amount, model } = this.costOf(metricsOrAmount);

    // Build transaction metadata: caller fields first, system fields last (M7).
    const txMeta: Record<string, unknown> = {};
    if (isAmount(metricsOrAmount)) {
      if (options?.metadata) {
        for (const [k, v] of Object.entries(options.metadata)) {
          if (v != null) txMeta[k] = v;
        }
      }
      if (idempotencyKey) txMeta["idempotencyKey"] = idempotencyKey;
    } else {
      if (options?.metadata) {
        for (const [k, v] of Object.entries(options.metadata)) {
          if (v != null) txMeta[k] = v;
        }
      }
      txMeta["inputTokens"] = metricsOrAmount.inputTokens ?? 0;
      txMeta["outputTokens"] = metricsOrAmount.outputTokens ?? 0;
      txMeta["model"] = metricsOrAmount.model ?? "unknown";
      txMeta["breakdownTotal"] = amount.toString();
      if (metricsOrAmount.fixedJob) txMeta["fixedJob"] = metricsOrAmount.fixedJob;
      if (idempotencyKey) txMeta["idempotencyKey"] = idempotencyKey;
    }

    const periodStart = await this.resolveAllowancePeriodStart(userId);
    // The caller re-supplies the same `feature` at settle as at reserve time,
    // exactly like `model` is re-supplied for per-model spend-cap accuracy.
    const feature = options?.feature ?? null;
    const { limit: featureLimit, periodStart: featurePeriodStart } = await this.resolveFeatureLimit(
      userId,
      feature,
    );

    const result = await this.store.settleLease(userId, leaseId, amount, {
      idempotencyKey,
      minBalance: this.engine ? new Decimal(this.engine.minBalance) : new Decimal(0),
      model,
      metadata: txMeta as CreditMetadata,
      periodStart,
      feature,
      featureLimit,
      featurePeriodStart,
    });

    if (result.error) {
      this.emit("credits.deduct_failed", userId, {
        error: result.error,
        amount,
        stage: "settle",
        leaseId,
      });
      if (result.error === "lease_expired") {
        this.emit("credits.lease_expired", userId, { leaseId });
      }
      this.raiseLeaseError(result.error, userId, amount);
    }

    this.emit("credits.deducted", userId, {
      transactionId: result.transactionId,
      amount: result.amount,
      allowanceConsumed: result.allowanceConsumed,
      balanceAfter: result.balanceAfter,
      model,
      leaseId,
      idempotent: result.idempotent,
    });

    // Cap signal: 'deny' breaching at settle is non-blocking (work is done) and
    // re-emitted as cap_reached; warn/notify as cap_warning (interface plan §7).
    if (result.capWarning === "deny") {
      this.emit("credits.cap_reached", userId, {
        amount: result.amount,
        model,
        blocking: false,
      });
    } else if (result.capWarning === "warn" || result.capWarning === "notify") {
      this.emit("credits.cap_warning", userId, {
        balanceAfter: result.balanceAfter,
        amount: result.amount,
        model,
        action: result.capWarning,
      });
    }

    // Feature-limit signal: mirrors the cap signal above ("prefer deny" was
    // already applied by the store when choosing featureLimitWarning).
    if (result.featureLimitWarning === "deny") {
      this.emit("credits.feature_limit_reached", userId, {
        feature,
        amount: result.amount,
        blocking: false,
      });
    } else if (result.featureLimitWarning === "warn" || result.featureLimitWarning === "notify") {
      this.emit("credits.feature_limit_warning", userId, {
        feature,
        action: result.featureLimitWarning,
        amount: result.amount,
      });
    }

    await this.postChargeSignals(userId, result);
    return result;
  }

  /** Release a lease without charging (work failed/aborted) — idempotent (H1). */
  async release(userId: string, leaseId: string): Promise<ReleaseResult> {
    const result = await this.store.releaseLease(userId, leaseId);
    if (result.released) {
      this.emit("credits.reservation_released", userId, {
        leaseId,
        reason: result.reason,
      });
    }
    return result;
  }

  /** Extend a lease's TTL for long batch/agentic jobs (B4). */
  async renew(userId: string, leaseId: string, ttl?: number | null): Promise<LeaseResult> {
    const ttlSeconds = ttl != null ? ttl : this.defaultTtl;
    const result = await this.store.renewLease(userId, leaseId, ttlSeconds);
    if (result.error) {
      if (result.error === "lease_expired") {
        this.emit("credits.lease_expired", userId, { leaseId });
      }
      this.raiseLeaseError(result.error, userId, new Decimal(0));
    }
    return result;
  }

  /**
   * Advisory affordability check — UI only, non-locking, may be stale (D4/H3).
   *
   * Never use this as an admission gate; only ``reserve`` is authoritative.
   */
  async canAfford(
    userId: string,
    metricsOrAmount: MetricsOrAmount,
    options?: CanAffordOptions,
  ): Promise<CanAffordResult> {
    await this.maybeLazyExpire(userId);
    const operationType = options?.operationType ?? "usage";
    const requiredFeature = options?.requiredFeature ?? null;
    const { amount: worstCase } = this.costOf(metricsOrAmount);
    const avail = await this.store.getAvailable(userId);
    const policy = await this.resolvePolicy(userId, operationType, options?.billingMode);
    const floor = this.resolveFloor(policy);

    let affordable = true;
    let reason: string | null = null;
    if (requiredFeature != null) {
      const check = await this.store.checkFeature(userId, requiredFeature);
      if (!check.hasFeature) {
        affordable = false;
        reason = "feature_not_entitled";
      }
    }
    if (affordable && avail.available.minus(worstCase).lt(floor)) {
      affordable = false;
      reason = "insufficient_credits";
    }

    return { affordable, spendable: avail.available, worstCase, reason };
  }

  /** Advisory ``available = balance − Σ active holds`` read (UI only, D4/H3). */
  async getAvailable(userId: string): Promise<AvailableResult> {
    await this.maybeLazyExpire(userId);
    return await this.store.getAvailable(userId);
  }

  /** Get a user's per-tier credit balances (credit tiers). Thin pass-through, no event emission. */
  async getCreditTiers(userId: string): Promise<TierBalancesResult> {
    await this.maybeLazyExpire(userId);
    return await this.store.getCreditTiers(userId);
  }

  /**
   * Get remaining free allowance for the current billing period.
   *
   * Convenience wrapper that routes through the manager so callers never need
   * to reach past it into the raw store. Returns a zero-allowance result for
   * planless users (no exception).
   *
   * For ``rolling_30d``/``anniversary`` plans (WS9), resolves and threads
   * ``periodStart`` so the reported window matches the one actually used by
   * ``deduct``/``settle``/``reserve`` — a ``calendar_month`` plan (the default)
   * takes the fast path unchanged.
   */
  async checkAllowance(userId: string): Promise<AllowanceResult> {
    const plan = await this.store.getUserPlan(userId);
    if (!plan.planId || !plan.allowancePeriod || plan.allowancePeriod === "calendar_month") {
      return await this.store.checkAllowance(userId);
    }
    const { start, end } = resolveAllowanceWindow(
      new Date(),
      plan.allowancePeriod,
      plan.planAssignedAt ?? null,
    );
    const result = await this.store.checkAllowance(userId, start);
    // The store/SQL layer only knows a generic calendar-month periodEnd (see
    // 004_plans.sql / 009_deduct_and_leases.sql); override it here with the
    // authoritative window this manager resolved.
    const periodEnd = new Date(end.getTime() - 86_400_000);
    return {
      ...result,
      periodStart: start.toISOString(),
      periodEnd: periodEnd.toISOString(),
    };
  }

  /**
   * One-call shortcut wiring reserve → doWork → settle (interface plan §4).
   *
   * ``doWork`` runs the operation and returns ``{ result, actual }`` where
   * ``actual`` is the real usage metrics (or amount) to settle. On any exception
   * from ``doWork`` the lease is released and the error re-raised. For long jobs
   * ``doWork`` may call {@link renew}. A crash between reserve and settle is
   * covered by the lease TTL (and the store's reaper).
   */
  async runBilled<T>(
    userId: string,
    options: RunBilledOptions<T>,
  ): Promise<{ result: T; deduction: DeductionResult }> {
    const lease = await this.reserve(userId, options.estimate, {
      operationType: options.operationType,
      billingMode: options.billingMode,
      requiredFeature: options.requiredFeature,
      ttl: options.ttl,
      feature: options.feature,
    });

    let workResult: T;
    let actual: MetricsOrAmount;
    try {
      ({ result: workResult, actual } = await options.doWork());
    } catch (err) {
      await this.release(userId, lease.leaseId);
      throw err;
    }

    const deduction = await this.settle(userId, lease.leaseId, actual, {
      idempotencyKey: options.idempotencyKey,
      feature: options.feature,
    });
    return { result: workResult, deduction };
  }

  // ── Low-balance / overdraft signals (interface plan §6) ─────────────

  /** Emit overdraft + multi-level low_balance after a balance-decreasing op. */
  private async postChargeSignals(userId: string, result: DeductionResult): Promise<void> {
    if (result.balanceAfter.lt(0)) {
      this.emit("credits.overdraft", userId, {
        balance: result.balanceAfter,
        amount: result.amount,
      });
    }
    if (result.idempotent) return;
    const balanceAfter = result.balanceAfter;
    const balanceBefore = balanceAfter.plus(result.amount);
    await this.emitLowBalance(userId, balanceBefore, balanceAfter);
  }

  /** Edge-triggered low_balance: multi-level if configured, else single (§6). */
  private async emitLowBalance(
    userId: string,
    balanceBefore: Decimal,
    balanceAfter: Decimal,
  ): Promise<void> {
    if (this.lowBalanceThresholds) {
      const below = this.lbBelow.get(userId) ?? new Set<string>();
      this.lbBelow.set(userId, below);
      const newlyCrossed: Decimal[] = [];
      for (const t of this.lowBalanceThresholds) {
        // high → low
        if (balanceAfter.lte(t)) {
          if (!below.has(t.toString())) {
            below.add(t.toString());
            newlyCrossed.push(t);
          }
        } else {
          below.delete(t.toString());
        }
      }
      const fireLevel =
        newlyCrossed.length > 0 ? newlyCrossed.reduce((min, t) => (t.lt(min) ? t : min)) : null;
      if (fireLevel !== null) {
        await this.fireLowBalance(userId, balanceAfter, fireLevel);
      }
      return;
    }

    const threshold = this.resolveLowBalanceThreshold();
    if (balanceBefore.gt(threshold) && balanceAfter.lte(threshold)) {
      await this.fireLowBalance(userId, balanceAfter, threshold);
    }
  }

  /** Emit ``credits.low_balance`` and invoke the non-blocking ``onLowBalance``. */
  private async fireLowBalance(
    userId: string,
    balance: Decimal,
    threshold: Decimal,
  ): Promise<void> {
    const data = { balance, threshold };
    this.emit("credits.low_balance", userId, data);
    if (this.onLowBalance != null) {
      const event: CreditEvent = {
        type: "credits.low_balance",
        timestamp: new Date(),
        userId,
        data,
      };
      try {
        // Never block/break the op on a handler failure (§6/H4).
        await this.onLowBalance(event);
      } catch (err) {
        console.error(`[CreditManager] onLowBalance handler failed for user ${userId}:`, err);
      }
    }
  }

  /**
   * Full deduction flow as one atomic store call (contract §2).
   *
   * 1. ``breakdown = engine.calculate(metrics)``; ``cost = breakdown.total``
   *    (exact `Decimal`, **no truncation**).
   * 2. If ``cost <= 0`` short-circuit with a zero-amount result.
   * 3. Otherwise ``store.deductWithAllowance`` consumes allowance, enforces caps,
   *    applies the balance floor and debits — idempotency-keyed end-to-end.
   *
   * On a store ``error`` a ``credits.deduct_failed`` event is emitted and a
   * typed exception is thrown (``insufficient_credits`` → InsufficientCreditsError,
   * ``cap_reached`` → CapReachedError). No success event is emitted on error.
   */
  async deduct(
    userId: string,
    metrics: UsageMetrics,
    idempotencyKey?: string | null,
    metadata?: CreditMetadata | null,
    /** Named feature to enforce/tag a per-feature invocation-count limit for. */
    feature?: string | null,
  ): Promise<DeductionResult> {
    await this.maybeLazyExpire(userId);
    if (!this.engine)
      throw new PricingNotLoadedError(
        "pricing not loaded: call loadPricingFromStore or publishPricing first",
      );

    // 1) Calculate cost — exact Decimal, never truncated (H1).
    const breakdown = this.engine.calculate(metrics);
    const cost = breakdown.total;

    // 2) Zero-amount short-circuit (no balance touch, no store round-trip).
    if (cost.lte(0)) {
      const balance = await this.store.getBalance(userId);
      const result: DeductionResult = {
        transactionId: "",
        userId,
        amount: new Decimal(0),
        allowanceConsumed: new Decimal(0),
        balanceAfter: balance.balance,
        idempotent: false,
        capWarning: null,
        featureLimitWarning: null,
      };
      this.emit("credits.deducted", userId, {
        amount: new Decimal(0),
        balanceAfter: balance.balance,
        planCovered: false,
      });
      return result;
    }

    // Build transaction metadata: caller fields FIRST, system fields LAST so the
    // system fields win (contract §5 / M7).
    const meta: Record<string, unknown> = {};
    if (metadata) {
      for (const [k, v] of Object.entries(metadata)) {
        if (v != null) meta[k] = v;
      }
    }
    meta["inputTokens"] = metrics.inputTokens ?? 0;
    meta["outputTokens"] = metrics.outputTokens ?? 0;
    meta["model"] = metrics.model ?? "unknown";
    meta["breakdownTotal"] = breakdown.total.toString();
    if (metrics.fixedJob) meta["fixedJob"] = metrics.fixedJob;
    if (idempotencyKey) meta["idempotencyKey"] = idempotencyKey;

    const periodStart = await this.resolveAllowancePeriodStart(userId);
    const { limit: featureLimit, periodStart: featurePeriodStart } = await this.resolveFeatureLimit(
      userId,
      feature,
    );

    const options: DeductWithAllowanceOptions = {
      idempotencyKey: idempotencyKey ?? null,
      minBalance: this.minBalanceDecimal(),
      model: metrics.model ?? null,
      metadata: meta as CreditMetadata,
      periodStart,
      feature: feature ?? null,
      featureLimit,
      featurePeriodStart,
    };

    // 3) Atomic charge.
    const result = await this.store.deductWithAllowance(userId, cost, options);

    if (result.error) {
      this.emit("credits.deduct_failed", userId, {
        error: result.error,
        amount: cost,
        model: metrics.model ?? null,
      });
      if (result.error === "cap_reached") {
        throw new CapReachedError(`Spend cap exceeded for user ${userId} (requested ${cost})`);
      }
      if (result.error === "feature_limit_reached") {
        this.emit("credits.feature_limit_reached", userId, {
          feature,
          amount: cost,
          model: metrics.model ?? null,
        });
        throw new FeatureLimitReachedError(
          `Feature limit exceeded for '${feature}'. user=${userId}`,
        );
      }
      // insufficient_credits, invalid_amount, and any other business error.
      throw new InsufficientCreditsError(
        `Credit deduction failed: ${result.error}. user=${userId}, requested=${cost}`,
      );
    }

    // Success — emit deducted, then any cap warning, then edge-triggered low-balance.
    this.emit("credits.deducted", userId, {
      transactionId: result.transactionId,
      amount: result.amount,
      allowanceConsumed: result.allowanceConsumed,
      balanceAfter: result.balanceAfter,
      model: metrics.model ?? null,
      idempotent: result.idempotent,
    });

    if (result.capWarning) {
      this.emit("credits.cap_warning", userId, {
        action: result.capWarning,
        amount: result.amount,
        model: metrics.model ?? null,
      });
    }

    // Non-blocking feature-limit signal surfaced by the store (parallels
    // capWarning above). A 'deny' feature limit never reaches here — it
    // aborts in the error path — so only warn/notify can appear here.
    if (result.featureLimitWarning === "warn" || result.featureLimitWarning === "notify") {
      this.emit("credits.feature_limit_warning", userId, {
        feature,
        balanceAfter: result.balanceAfter,
        amount: result.amount,
        model: metrics.model ?? null,
        action: result.featureLimitWarning,
      });
    }

    // low_balance is EDGE-triggered (M18): only fire when THIS deduction crossed
    // the threshold. A replayed (idempotent) result did not move the balance, so
    // it never crosses. balanceBefore = balanceAfter + amount charged.
    if (!result.idempotent) {
      const threshold = this.resolveLowBalanceThreshold();
      const balanceBefore = result.balanceAfter.plus(result.amount);
      if (result.balanceAfter.lte(threshold) && balanceBefore.gt(threshold)) {
        this.emit("credits.low_balance", userId, {
          balance: result.balanceAfter,
          threshold,
          minBalance: this.minBalanceDecimal(),
        });
      }
    }

    return result;
  }

  /**
   * Refund a previous credit deduction.
   *
   * H3: the store's ``error`` is checked **before** emitting. A successful refund
   * emits ``credits.refunded``; a failed/duplicate/over-refund emits
   * ``credits.refund_failed`` and throws a typed ``RefundError`` (no success
   * event is ever emitted for a failed refund).
   */
  async refundCredits(
    transactionId: string,
    amount?: Decimal | number,
    reason?: string,
    metadata?: CreditMetadata | null,
  ): Promise<RefundResult> {
    const refundAmount = amount != null ? toDecimal(amount) : undefined;
    const result = await this.store.refundCredits(transactionId, refundAmount, reason, metadata);

    if (result.error) {
      this.emit("credits.refund_failed", result.userId, {
        transactionId,
        error: result.error,
        reason: reason ?? null,
      });
      throw new RefundError(`Refund failed: ${result.error}. transaction=${transactionId}`);
    }

    this.emit("credits.refunded", result.userId, {
      transactionId,
      refundTransactionId: result.refundTransactionId,
      amount: result.amount,
      newBalance: result.newBalance,
      reason: reason ?? null,
    });
    return result;
  }

  /**
   * Deduct from a team's shared balance pool.
   *
   * Calculates the cost via the pricing engine (exact `Decimal`, no truncation),
   * then debits the team balance. Threads an optional ``idempotencyKey`` through
   * to the store so retried team charges are not double-counted (H12).
   */
  async deductTeam(
    teamId: string,
    userId: string,
    metrics: UsageMetrics,
    idempotencyKey?: string | null,
    metadata?: CreditMetadata | null,
  ): Promise<TeamDeductionResult> {
    // Lazy expiry is scoped to the individual member's credits, not the team's
    // shared pool — there's no per-team expiry concept.
    await this.maybeLazyExpire(userId);
    if (!this.engine)
      throw new PricingNotLoadedError(
        "pricing not loaded: call loadPricingFromStore or publishPricing first",
      );

    const breakdown = this.engine.calculate(metrics);
    const cost = breakdown.total;

    if (cost.lte(0)) {
      const teamBal = await this.store.getTeamBalance(teamId);
      return {
        transactionId: "",
        teamId,
        userId,
        amount: new Decimal(0),
        teamBalanceAfter: teamBal.balance,
      };
    }

    const result = await this.store.deductTeam(teamId, userId, cost, metadata, idempotencyKey);
    // H2 fix: surface store errors — emit credits.deduct_failed and throw,
    // mirroring Python manager.py:1069-1082. Previously returned a silent
    // success-shaped object with an .error field, so failed charges looked OK.
    if (result.error) {
      this.emit("credits.deduct_failed", userId, {
        error: result.error,
        amount: cost,
        teamId,
        deductType: "team",
      });
      throw new InsufficientCreditsError(
        `Team deduction failed: ${result.error}. Team=${teamId}, user=${userId}, requested=${cost}`,
      );
    }
    this.emit("credits.deducted", userId, {
      transactionId: result.transactionId,
      amount: result.amount,
      teamBalanceAfter: result.teamBalanceAfter,
      teamId,
      deductType: "team",
    });
    return result;
  }

  /**
   * Shortcut for fixed-cost batch jobs.
   *
   * L1: an unknown/typo'd ``jobName`` is rejected (throws) instead of silently
   * charging 0 credits — ``engine.getFixedCost(jobName) === null`` means the job
   * is not configured.
   */
  async deductFixed(
    userId: string,
    jobName: string,
    idempotencyKey?: string | null,
    metadata?: CreditMetadata | null,
    feature?: string | null,
  ): Promise<DeductionResult> {
    await this.maybeLazyExpire(userId);
    if (!this.engine)
      throw new PricingNotLoadedError(
        "pricing not loaded: call loadPricingFromStore or publishPricing first",
      );
    if (this.engine.getFixedCost(jobName) === null) {
      throw new ConfigError(
        `unknown fixed job '${jobName}': not configured in pricing 'fixed' section`,
      );
    }
    return await this.deduct(userId, { fixedJob: jobName }, idempotencyKey, metadata, feature);
  }

  /**
   * Sweep expired credits — global when ``userId`` is omitted, scoped to a
   * single user when given (used by ``maybeLazyExpire``). Shared by the public
   * ``sweepExpiredCredits`` (always global) and the lazy-on-read trigger.
   */
  private async runSweep(dryRun: boolean, userId?: string): Promise<SweepResult> {
    const result = await this.store.sweepExpiredCredits(dryRun, userId);
    if (!dryRun && result.expiredCount > 0) {
      this.emit("credits.expired", userId ?? "system", {
        expiredCount: result.expiredCount,
        expiredAmount: result.expiredAmount,
      });
    }
    return result;
  }

  /**
   * Sweep expired credits from all users' balances.
   *
   * When ``dryRun`` is true, reports what would be expired without modifying
   * any balances. Unaffected by ``options.lazyExpiry`` — this always runs a
   * global sweep; lazy expiry only scopes *automatic* per-user sweeps.
   */
  async sweepExpiredCredits(dryRun = false): Promise<SweepResult> {
    return await this.runSweep(dryRun, undefined);
  }

  // ── Usage analytics ──────────────────────────────────────────────────

  /** Aggregate statistics across all users in a time window. */
  async aggregateStats(start: Date, end: Date): Promise<AggregateStats> {
    return await this.store.aggregateStats(start, end);
  }

  /** Aggregate spend by user in a time window. */
  async spendByUser(start: Date, end: Date): Promise<SpendByUserRow[]> {
    return await this.store.spendByUser(start, end);
  }

  /** Aggregate spend by model in a time window. */
  async spendByModel(start: Date, end: Date): Promise<SpendByModelRow[]> {
    return await this.store.spendByModel(start, end);
  }

  /** List all user credit transactions with pagination. */
  async listUserTransactions(
    userId: string,
    options?: ListTransactionsOptions,
  ): Promise<PaginatedTransactions> {
    return await this.store.listUserTransactions(userId, options);
  }

  async listUsageEvents(
    userId: string,
    options?: ListUsageEventsOptions,
  ): Promise<PaginatedTransactions> {
    return await this.store.listUsageEvents(userId, options);
  }

  /** Top users by spend in a time window with limit. */
  async topUsers(limit: number, start: Date, end: Date): Promise<TopUserRow[]> {
    return await this.store.topUsers(limit, start, end);
  }

  /** Daily spend aggregation in a time window. */
  async dailySpend(start: Date, end: Date): Promise<DailySpendRow[]> {
    return await this.store.dailySpend(start, end);
  }
}
