import Decimal from "decimal.js";
import { ConfigError } from "./errors.js";
import { validateExpression } from "./expr.js";
import type { AllowancePeriod, FeatureLimitPeriod } from "./allowance.js";
import type {
  BillingMode,
  FeatureLimit,
  OperationPolicy,
  PlanDefinition,
  BucketDefinition,
} from "./types.js";

/** Valid `allowancePeriod` values (WS9b). */
const ALLOWANCE_PERIODS: ReadonlySet<string> = new Set([
  "calendar_month",
  "rolling_30d",
  "anniversary",
]);

/** Valid `FeatureLimit.period` cadences (mirrors Python `resolve_calendar_window`). */
const FEATURE_LIMIT_PERIODS: ReadonlySet<string> = new Set([
  "daily",
  "weekly",
  "monthly",
  "yearly",
]);

/** Valid `FeatureLimit.onExceed` values (mirrors `SpendCap.action`). */
const FEATURE_LIMIT_ACTIONS: ReadonlySet<string> = new Set(["deny", "warn", "notify"]);

/**
 * Canonical metric-variable set — MUST mirror `PricingEngine.buildVariables`
 * exactly. Expressions may only reference these names (or allowed functions).
 * Passed into `validateExpression` so typos like `inputtokens` fail at
 * config-load, not at first runtime use (M5).
 */
export const KNOWN_VARIABLES: ReadonlySet<string> = new Set([
  "input_tokens",
  "output_tokens",
  "cache_read_tokens",
  "cache_write_tokens",
  "tool_calls",
  "search_queries",
  "search_results",
  "web_search_calls",
  "code_exec_calls",
]);

/** Metering configuration section. */
export interface MeteringConfig {
  models: Record<string, string>;
  tools: Record<string, string>;
  search?: string | null;
  cacheDiscount?: string | null;
  flatJobs: Record<string, Decimal>;
}

/** Ledger configuration section. */
export interface LedgerConfig {
  minBalance: Decimal;
  signupGrant?: number | null;
  buckets?: Record<string, BucketDefinition> | null;
}

/** Billing configuration section. */
export interface BillingSection {
  currency?: string;
  subscriptions?: Record<string, unknown>;
  topups?: Record<string, unknown>;
}

/** Internal validated pricing configuration. */
export interface PricingConfig {
  version: number;
  metering: MeteringConfig;
  ledger: LedgerConfig;
  plans?: Record<string, PlanDefinition> | null;
  billing?: BillingSection;
}

/** Known top-level config keys, checked after snake→camel normalisation. */
const TOP_LEVEL_KEYS: ReadonlySet<string> = new Set([
  "version",
  "metering",
  "ledger",
  "plans",
  "billing",
]);

/** Known metering-section keys. */
const METERING_KEYS: ReadonlySet<string> = new Set([
  "models",
  "tools",
  "search",
  "cacheDiscount",
  "flatJobs",
]);

/** Known ledger-section keys. */
const LEDGER_KEYS: ReadonlySet<string> = new Set(["minBalance", "signupGrant", "buckets"]);

/** Known plan-definition keys (new nested schema). */
const PLAN_KEYS: ReadonlySet<string> = new Set([
  "label",
  "allowance",
  "safety",
  "rateOverrides",
  "entitlements",
]);

/** Known `FeatureLimit` keys. */
const FEATURE_LIMIT_KEYS: ReadonlySet<string> = new Set([
  "maxCalls",
  "period",
  "onExceed",
  "value",
]);

/** Known `OperationPolicy` keys (entries of a plan's `safety.perOperation` map). */
const OPERATION_POLICY_KEYS: ReadonlySet<string> = new Set([
  "billingMode",
  "maxConcurrent",
  "overdraftFloor",
]);

/** Known bucket-definition keys. */
const BUCKET_KEYS: ReadonlySet<string> = new Set([
  "label",
  "priority",
  "expires",
  "ttlDays",
  "allowOverdraft",
  "isDefaultBucket",
]);

/** Known allowance-section keys. */
const ALLOWANCE_KEYS: ReadonlySet<string> = new Set(["amount", "period"]);

/** Known safety-section keys. */
const SAFETY_KEYS: ReadonlySet<string> = new Set([
  "billingMode",
  "perOperation",
  "maxConcurrent",
  "overdraftFloor",
]);

/** Reject any key not in `known` — catches typos that would otherwise silently fall back to a default. */
function assertKnownKeys(
  obj: Record<string, unknown>,
  known: ReadonlySet<string>,
  context: string,
): void {
  for (const key of Object.keys(obj)) {
    if (!known.has(key)) {
      throw new ConfigError(`unknown config key in ${context}: ${key}`);
    }
  }
}

/** Variable set for validating `tools` expressions: base set + `calls` (WS2). */
const TOOLS_VARIABLES: ReadonlySet<string> = new Set([...KNOWN_VARIABLES, "calls"]);

function validateExpressions(raw: PricingConfig): void {
  for (const [key, expr] of Object.entries(raw.metering.models)) {
    try {
      validateExpression(expr, KNOWN_VARIABLES);
    } catch (e) {
      throw new ConfigError(
        `invalid expression in metering.models.${key}: ${(e as Error).message}`,
      );
    }
  }
  for (const [key, expr] of Object.entries(raw.metering.tools)) {
    try {
      validateExpression(expr, TOOLS_VARIABLES);
    } catch (e) {
      throw new ConfigError(`invalid expression in metering.tools.${key}: ${(e as Error).message}`);
    }
  }
  if (raw.metering.search) {
    try {
      validateExpression(raw.metering.search, KNOWN_VARIABLES);
    } catch (e) {
      throw new ConfigError(`invalid expression in metering.search: ${(e as Error).message}`);
    }
  }
  if (raw.metering.cacheDiscount) {
    try {
      validateExpression(raw.metering.cacheDiscount, KNOWN_VARIABLES);
    } catch (e) {
      throw new ConfigError(
        `invalid expression in metering.cacheDiscount: ${(e as Error).message}`,
      );
    }
  }
}

/**
 * H6 fix: normalise snake_case keys to camelCase before consumption.
 *
 * The documented config format is snake_case (min_balance, free_allowance,
 * rate_overrides, billing_mode, overdraft_floor). JS previously only read
 * camelCase, silently dropping these fields and falling back to defaults.
 */
function normaliseKeys(data: Record<string, unknown>): Record<string, unknown> {
  const keyMap: Record<string, string> = {
    min_balance: "minBalance",
    signup_grant: "signupGrant",
    rate_overrides: "rateOverrides",
    billing_mode: "billingMode",
    overdraft_floor: "overdraftFloor",
    per_operation: "perOperation",
    max_concurrent: "maxConcurrent",
    ttl_days: "ttlDays",
    allow_overdraft: "allowOverdraft",
    max_calls: "maxCalls",
    on_exceed: "onExceed",
    cache_discount: "cacheDiscount",
    flat_jobs: "flatJobs",
    interval_count: "intervalCount",
    deposit_to: "depositTo",
    credits_per_unit: "creditsPerUnit",
    min_amount_minor: "minAmountMinor",
    max_amount_minor: "maxAmountMinor",
    tax_behavior: "taxBehavior",
    product_id: "productId",
    price_id: "priceId",
    variant_id: "variantId",
    lookup_key: "lookupKey",
    replace_prior: "replacePrior",
    allowance: "allowance",
    safety: "safety",
    entitlements: "entitlements",
    label: "label",
    metering: "metering",
    ledger: "ledger",
    billing: "billing",
  };
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(data)) {
    const mapped = keyMap[k] ?? k;
    out[mapped] = v;
  }
  return out;
}

/** Recursively normalise a plan definition object (new nested schema). */
function normalisePlan(p: Record<string, unknown>): Record<string, unknown> {
  const out = normaliseKeys(p);
  // Normalise nested allowance section
  if (out.allowance != null && typeof out.allowance === "object" && !Array.isArray(out.allowance)) {
    out.allowance = normaliseKeys(out.allowance as Record<string, unknown>);
  }
  // Normalise nested safety section
  if (out.safety != null && typeof out.safety === "object" && !Array.isArray(out.safety)) {
    out.safety = normaliseKeys(out.safety as Record<string, unknown>);
    // Normalise perOperation inside safety
    const safety = out.safety as Record<string, unknown>;
    if (safety.perOperation != null && typeof safety.perOperation === "object") {
      const perOp = safety.perOperation as Record<string, unknown>;
      for (const [opKey, opVal] of Object.entries(perOp)) {
        perOp[opKey] = normaliseKeys(opVal as Record<string, unknown>);
      }
    }
  }
  return out;
}

/** Recursively normalise a bucket definition object (credit buckets). */
function normaliseBucket(t: Record<string, unknown>): Record<string, unknown> {
  return normaliseKeys(t);
}

/**
 * Normalise + validate a plan's `entitlements` map (per-feature
 * invocation-count limits). Each entry's own keys are snake/camel-normalised
 * since, unlike top-level plan fields, these nested objects are not otherwise
 * touched by `normaliseKeys`.
 */
function normaliseEntitlements(
  planKey: string,
  raw: Record<string, unknown>,
): Record<
  string,
  {
    value?: unknown;
    maxCalls: number;
    period: FeatureLimitPeriod;
    onExceed: "deny" | "warn" | "notify";
  }
> {
  const out: Record<string, FeatureLimit> = {};
  for (const [featureKey, rawLimit] of Object.entries(raw)) {
    const limit = normaliseKeys((rawLimit ?? {}) as Record<string, unknown>);
    assertKnownKeys(limit, FEATURE_LIMIT_KEYS, `plans.${planKey}.entitlements.${featureKey}`);
    const maxCalls = Number(limit.maxCalls ?? 0);
    const period = (limit.period as string | undefined) ?? "monthly";
    const onExceed = (limit.onExceed as string | undefined) ?? "deny";
    if (!Number.isFinite(maxCalls) || maxCalls < 0) {
      throw new ConfigError(
        `invalid entitlements in plans.${planKey}.${featureKey}: maxCalls must be >= 0, got ${String(limit.maxCalls)}`,
      );
    }
    if (!FEATURE_LIMIT_PERIODS.has(period)) {
      throw new ConfigError(
        `invalid entitlements in plans.${planKey}.${featureKey}: unknown period '${period}' ` +
          `(expected one of ${[...FEATURE_LIMIT_PERIODS].sort().join(", ")})`,
      );
    }
    if (!FEATURE_LIMIT_ACTIONS.has(onExceed)) {
      throw new ConfigError(
        `invalid entitlements in plans.${planKey}.${featureKey}: unknown onExceed '${onExceed}' ` +
          `(expected one of ${[...FEATURE_LIMIT_ACTIONS].sort().join(", ")})`,
      );
    }
    out[featureKey] = {
      maxCalls,
      period: period as FeatureLimitPeriod,
      onExceed: onExceed as FeatureLimit["onExceed"],
      value: limit.value,
    };
  }
  return out;
}

/**
 * Normalise + validate a plan's `safety.perOperation` map (per-operation
 * financial-safety policy overrides, mirrors Python's `OperationPolicy`).
 */
function normalisePerOperation(
  planKey: string,
  raw: Record<string, unknown>,
): Record<string, OperationPolicy> {
  const out: Record<string, OperationPolicy> = {};
  for (const [opType, rawPolicy] of Object.entries(raw)) {
    const policy = normaliseKeys((rawPolicy ?? {}) as Record<string, unknown>);
    assertKnownKeys(
      policy,
      OPERATION_POLICY_KEYS,
      `plans.${planKey}.safety.perOperation.${opType}`,
    );
    const billingMode = (policy.billingMode as string | undefined) ?? "strict";
    if (billingMode !== "strict" && billingMode !== "overdraft") {
      throw new ConfigError(
        `invalid billingMode in plans.${planKey}.safety.perOperation.${opType}: '${billingMode}' ` +
          `(expected 'strict' or 'overdraft')`,
      );
    }
    out[opType] = {
      billingMode: billingMode as BillingMode,
      maxConcurrent: (policy.maxConcurrent as number | null) ?? null,
      overdraftFloor:
        policy.overdraftFloor != null
          ? new Decimal(policy.overdraftFloor as number | string)
          : null,
    };
  }
  return out;
}

/** Load and validate a pricing config from a raw dictionary. */
export function loadConfigFromDict(data: Record<string, unknown>): PricingConfig {
  // H6: normalise top-level keys from snake_case to camelCase first.
  const d = normaliseKeys(data);
  assertKnownKeys(d, TOP_LEVEL_KEYS, "config");

  // Only `1` is a valid version (mirrors Python's `Literal[1]`).
  const version = (d.version as number | undefined) ?? 1;
  if (version !== 1) {
    throw new ConfigError(`version must be 1, got ${JSON.stringify(d.version)}`);
  }

  // ── Parse `metering` section ──

  const rawMetering = (d.metering ?? {}) as Record<string, unknown>;
  const meteringNormalised = normaliseKeys(rawMetering);
  assertKnownKeys(meteringNormalised, METERING_KEYS, "metering");

  if (meteringNormalised.models == null)
    throw new ConfigError("missing required section: metering.models");
  if (
    typeof meteringNormalised.models !== "object" ||
    Object.keys(meteringNormalised.models as object).length === 0
  ) {
    throw new ConfigError("metering.models must be a non-empty dict");
  }

  const metering: MeteringConfig = {
    models: meteringNormalised.models as Record<string, string>,
    tools: (meteringNormalised.tools as Record<string, string> | undefined) ?? { "*": "calls * 0" },
    search: (meteringNormalised.search as string | null | undefined) ?? null,
    cacheDiscount: (meteringNormalised.cacheDiscount as string | null | undefined) ?? null,
    flatJobs: Object.fromEntries(
      Object.entries(
        (meteringNormalised.flatJobs as Record<string, number | string> | undefined) ?? {},
      ).map(([job, cost]) => [job, new Decimal(cost)]),
    ),
  };

  // Validate flatJobs >= 0
  for (const [job, cost] of Object.entries(metering.flatJobs)) {
    if (cost.isNegative()) {
      throw new ConfigError(`metering.flatJobs.${job} must be >= 0, got ${cost.toString()}`);
    }
  }

  // ── Parse `ledger` section ──

  const rawLedger = (d.ledger ?? {}) as Record<string, unknown>;
  const ledgerNormalised = normaliseKeys(rawLedger);
  assertKnownKeys(ledgerNormalised, LEDGER_KEYS, "ledger");

  const minBalance = new Decimal((ledgerNormalised.minBalance as number | string | undefined) ?? 0);
  if (minBalance.isNegative()) throw new ConfigError("ledger.minBalance must be >= 0");

  const signupGrant = ledgerNormalised.signupGrant as number | undefined;
  if (signupGrant != null && signupGrant < 0) {
    throw new ConfigError(`ledger.signupGrant must be >= 0, got ${signupGrant}`);
  }

  const ledger: LedgerConfig = {
    minBalance,
    signupGrant: signupGrant ?? 50,
    buckets: undefined, // populated below
  };

  // ── Parse `billing` section ──

  const billing: BillingSection | undefined =
    d.billing != null ? { currency: "USD", ...(d.billing as Record<string, unknown>) } : undefined;

  // ── Parse `plans` section ──

  const rawPlans = d.plans as Record<string, Record<string, unknown>> | undefined;
  const plans = rawPlans
    ? Object.fromEntries(Object.entries(rawPlans).map(([k, v]) => [k, normalisePlan(v)]))
    : undefined;

  if (plans) {
    for (const [planKey, plan] of Object.entries(plans)) {
      assertKnownKeys(plan, PLAN_KEYS, `plans.${planKey}`);
      // A plan definition must carry a `label` (mirrors Python's `config.py`).
      if (plan.label == null) {
        throw new ConfigError(
          `plan definition is missing required 'label' field: plans.${planKey}`,
        );
      }
      // Validate allowance section
      if (plan.allowance != null) {
        if (typeof plan.allowance !== "object" || Array.isArray(plan.allowance)) {
          throw new ConfigError(`plans.${planKey}.allowance must be a dict`);
        }
        assertKnownKeys(
          plan.allowance as Record<string, unknown>,
          ALLOWANCE_KEYS,
          `plans.${planKey}.allowance`,
        );
        const allowance = plan.allowance as Record<string, unknown>;
        if (allowance.period != null && !ALLOWANCE_PERIODS.has(allowance.period as string)) {
          throw new ConfigError(
            `invalid allowance period in plans.${planKey}: ${String(allowance.period)} ` +
              `(expected one of ${[...ALLOWANCE_PERIODS].sort().join(", ")})`,
          );
        }
      }
      // Validate safety section
      if (plan.safety != null) {
        if (typeof plan.safety !== "object" || Array.isArray(plan.safety)) {
          throw new ConfigError(`plans.${planKey}.safety must be a dict`);
        }
        assertKnownKeys(
          plan.safety as Record<string, unknown>,
          SAFETY_KEYS,
          `plans.${planKey}.safety`,
        );
        const safety = plan.safety as Record<string, unknown>;
        if (
          safety.billingMode != null &&
          safety.billingMode !== "strict" &&
          safety.billingMode !== "overdraft"
        ) {
          throw new ConfigError(
            `invalid billingMode in plans.${planKey}.safety: '${String(safety.billingMode)}' ` +
              `(expected 'strict' or 'overdraft')`,
          );
        }
      }
      // Validate rate overrides
      const overrides = plan.rateOverrides as Record<string, string> | undefined;
      if (overrides) {
        for (const [modelKey, expr] of Object.entries(overrides)) {
          try {
            validateExpression(expr, KNOWN_VARIABLES);
          } catch (e) {
            throw new ConfigError(
              `invalid expression in plans.${planKey}.rateOverrides.${modelKey}: ${(e as Error).message}`,
            );
          }
        }
      }
      // Normalise entitlements
      if (plan.entitlements != null) {
        if (typeof plan.entitlements !== "object" || Array.isArray(plan.entitlements)) {
          throw new ConfigError(`plans.${planKey}.entitlements must be a dict`);
        }
        plan.entitlements = normaliseEntitlements(
          planKey,
          plan.entitlements as Record<string, unknown>,
        );
      }
    }
    const planLabels = Object.values(plans).map((p) => p.label as string);
    if (new Set(planLabels).size !== planLabels.length) {
      throw new ConfigError("duplicate plan labels in pricing config");
    }
  }

  // ── Parse `ledger.buckets` (was `tiers`) ──

  if (ledgerNormalised.buckets !== undefined && ledgerNormalised.buckets !== null) {
    if (typeof ledgerNormalised.buckets !== "object" || Array.isArray(ledgerNormalised.buckets)) {
      throw new ConfigError("ledger.buckets must be a dict of bucket definitions");
    }
    if (Object.keys(ledgerNormalised.buckets as object).length === 0) {
      throw new ConfigError(
        "ledger.buckets must not be an empty object; omit the `buckets` key entirely for no buckets",
      );
    }
  }
  const rawBuckets = ledgerNormalised.buckets as
    Record<string, Record<string, unknown>> | undefined;
  const buckets = rawBuckets
    ? Object.fromEntries(Object.entries(rawBuckets).map(([k, v]) => [k, normaliseBucket(v)]))
    : undefined;

  if (buckets) {
    let overdraftCount = 0;
    let defaultCount = 0;
    for (const [bucketKey, t] of Object.entries(buckets)) {
      assertKnownKeys(t, BUCKET_KEYS, `ledger.buckets.${bucketKey}`);
      if (t.allowOverdraft === true) overdraftCount++;
      if (t.isDefaultBucket === true) defaultCount++;
      if (t.ttlDays != null && (t.ttlDays as number) <= 0) {
        throw new ConfigError(
          `ledger.buckets.${bucketKey}.ttlDays must be > 0, got ${String(t.ttlDays)}`,
        );
      }
    }
    if (overdraftCount > 1) {
      throw new ConfigError("at most one bucket may set allowOverdraft: true");
    }
    if (defaultCount > 1) {
      throw new ConfigError("at most one bucket may set isDefaultBucket: true");
    }
  }

  const config: PricingConfig = {
    version,
    metering,
    ledger,
  };

  // Populate ledger.buckets
  if (buckets) {
    const bucketDefs: Record<string, BucketDefinition> = {};
    for (const [key, t] of Object.entries(buckets)) {
      bucketDefs[key] = {
        label: (t.label as string | undefined) ?? key,
        priority: Number(t.priority ?? 0),
        expires: Boolean(t.expires ?? false),
        ttlDays: t.ttlDays != null ? Number(t.ttlDays) : null,
        allowOverdraft: Boolean(t.allowOverdraft ?? false),
        isDefaultBucket: Boolean(t.isDefaultBucket ?? false),
      };
    }
    config.ledger.buckets = bucketDefs;
  }

  if (plans) {
    const planDefs: Record<string, PlanDefinition> = {};
    for (const [key, p] of Object.entries(plans)) {
      const allowanceRaw = (p.allowance as Record<string, unknown>) ?? {};
      const allowanceAmount = new Decimal(
        (allowanceRaw["amount"] as number | string | undefined) ?? 0,
      );
      if (allowanceAmount.isNegative()) {
        throw new ConfigError(
          `plans.${key}.allowance.amount must be >= 0, got ${allowanceAmount.toString()}`,
        );
      }
      const safetyRaw = (p.safety as Record<string, unknown>) ?? {};
      const billingMode = (safetyRaw.billingMode ?? "strict") as "strict" | "overdraft";
      const perOperationRaw = safetyRaw.perOperation as Record<string, unknown> | undefined;

      planDefs[key] = {
        label: p.label as string,
        allowance: {
          amount: allowanceAmount,
          period: ((allowanceRaw["period"] as AllowancePeriod) ??
            "calendar_month") as AllowancePeriod,
        },
        safety: {
          billingMode,
          perOperation:
            perOperationRaw != null
              ? normalisePerOperation(key, perOperationRaw as Record<string, unknown>)
              : undefined,
          maxConcurrent: (safetyRaw.maxConcurrent as number | null) ?? null,
          overdraftFloor:
            safetyRaw.overdraftFloor != null
              ? new Decimal(safetyRaw.overdraftFloor as number | string)
              : null,
        },
        rateOverrides: (p.rateOverrides as Record<string, string>) ?? null,
        entitlements: (p.entitlements as Record<string, FeatureLimit>) ?? null,
      };
    }
    config.plans = planDefs;
  }

  if (billing) {
    config.billing = billing;
  }

  validateExpressions(config);
  return config;
}
