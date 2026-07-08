import Decimal from "decimal.js";
import { ConfigError } from "./errors.js";
import { validateExpression } from "./expr.js";
import type { AllowancePeriod, FeatureLimitPeriod } from "./allowance.js";
import type { BillingMode, FeatureLimit, OperationPolicy, PlanDefinition, TierDefinition } from "./types.js";

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

/** Valid `FeatureLimit.action` values (mirrors `SpendCap.action`). */
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

/** Internal validated pricing configuration. */
export interface PricingConfig {
  models: Record<string, string>;
  tools: Record<string, string>;
  search?: string | null;
  cache?: string | null;
  // Money field: fractional credits, never a binary `number` (contract §1) — mirrors Python's `Decimal`.
  minBalance: Decimal;
  signupBonus?: number | null;
  fixed: Record<string, Decimal>;
  plans?: Record<string, PlanDefinition> | null;
  tiers?: Record<string, TierDefinition> | null;
}

/** Known top-level config keys, checked after snake→camel normalisation. */
const TOP_LEVEL_KEYS: ReadonlySet<string> = new Set([
  "version",
  "models",
  "tools",
  "search",
  "cache",
  "minBalance",
  "signupBonus",
  "fixed",
  "plans",
  "tiers",
]);

/** Known plan-definition keys (`billingMode` is the short-form alias for `defaultBillingMode`). */
const PLAN_KEYS: ReadonlySet<string> = new Set([
  "id",
  "name",
  "freeAllowance",
  "rateOverrides",
  "features",
  "featureLimits",
  "defaultBillingMode",
  "billingMode",
  "perOperation",
  "maxConcurrent",
  "overdraftFloor",
  "allowancePeriod",
]);

/** Known `FeatureLimit` keys. */
const FEATURE_LIMIT_KEYS: ReadonlySet<string> = new Set(["maxCalls", "period", "action"]);

/** Known `OperationPolicy` keys (entries of a plan's `perOperation` map). */
const OPERATION_POLICY_KEYS: ReadonlySet<string> = new Set([
  "billingMode",
  "maxConcurrent",
  "overdraftFloor",
]);

/** Known tier-definition keys. */
const TIER_KEYS: ReadonlySet<string> = new Set([
  "name",
  "priority",
  "expires",
  "defaultTtlDays",
  "allowOverdraft",
  "isDefault",
]);

/** Reject any key not in `known` — catches typos (`min_balnce`) that would otherwise silently fall back to a default. */
function assertKnownKeys(obj: Record<string, unknown>, known: ReadonlySet<string>, context: string): void {
  for (const key of Object.keys(obj)) {
    if (!known.has(key)) {
      throw new ConfigError(`unknown config key in ${context}: ${key}`);
    }
  }
}

/** Variable set for validating `tools` expressions: base set + `this_tool_calls` (WS2). */
const TOOLS_VARIABLES: ReadonlySet<string> = new Set([...KNOWN_VARIABLES, "this_tool_calls"]);

function validateExpressions(raw: PricingConfig): void {
  for (const [key, expr] of Object.entries(raw.models)) {
    try {
      validateExpression(expr, KNOWN_VARIABLES);
    } catch (e) {
      throw new ConfigError(`invalid expression in models.${key}: ${(e as Error).message}`);
    }
  }
  for (const [key, expr] of Object.entries(raw.tools)) {
    try {
      validateExpression(expr, TOOLS_VARIABLES);
    } catch (e) {
      throw new ConfigError(`invalid expression in tools.${key}: ${(e as Error).message}`);
    }
  }
  if (raw.search) {
    try {
      validateExpression(raw.search, KNOWN_VARIABLES);
    } catch (e) {
      throw new ConfigError(`invalid expression in search: ${(e as Error).message}`);
    }
  }
  if (raw.cache) {
    try {
      validateExpression(raw.cache, KNOWN_VARIABLES);
    } catch (e) {
      throw new ConfigError(`invalid expression in cache: ${(e as Error).message}`);
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
    free_allowance: "freeAllowance",
    rate_overrides: "rateOverrides",
    billing_mode: "billingMode",
    overdraft_floor: "overdraftFloor",
    default_billing_mode: "defaultBillingMode",
    per_operation: "perOperation",
    max_concurrent: "maxConcurrent",
    signup_bonus: "signupBonus",
    allowance_period: "allowancePeriod",
    default_ttl_days: "defaultTtlDays",
    allow_overdraft: "allowOverdraft",
    is_default: "isDefault",
    feature_limits: "featureLimits",
    max_calls: "maxCalls",
  };
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(data)) {
    const mapped = keyMap[k] ?? k;
    out[mapped] = v;
  }
  return out;
}

/** Recursively normalise a plan definition object. */
function normalisePlan(p: Record<string, unknown>): Record<string, unknown> {
  return normaliseKeys(p);
}

/** Recursively normalise a tier definition object (credit tiers). */
function normaliseTier(t: Record<string, unknown>): Record<string, unknown> {
  return normaliseKeys(t);
}

/**
 * Normalise + validate a plan's `featureLimits` map (per-feature
 * invocation-count limits, mirrors `per_operation`/`OperationPolicy`
 * handling). Each entry's own keys are snake/camel-normalised (`max_calls`
 * -> `maxCalls`) since, unlike top-level plan fields, these nested objects
 * are not otherwise touched by `normaliseKeys`.
 */
function normaliseFeatureLimits(
  planKey: string,
  raw: Record<string, unknown>,
): Record<string, FeatureLimit> {
  const out: Record<string, FeatureLimit> = {};
  for (const [featureKey, rawLimit] of Object.entries(raw)) {
    const limit = normaliseKeys((rawLimit ?? {}) as Record<string, unknown>);
    assertKnownKeys(limit, FEATURE_LIMIT_KEYS, `plans.${planKey}.featureLimits.${featureKey}`);
    const maxCalls = Number(limit.maxCalls ?? 0);
    const period = (limit.period as string | undefined) ?? "monthly";
    const action = (limit.action as string | undefined) ?? "deny";
    if (!Number.isFinite(maxCalls) || maxCalls < 0) {
      throw new ConfigError(
        `invalid featureLimits in plans.${planKey}.${featureKey}: maxCalls must be >= 0, got ${String(limit.maxCalls)}`,
      );
    }
    if (!FEATURE_LIMIT_PERIODS.has(period)) {
      throw new ConfigError(
        `invalid featureLimits in plans.${planKey}.${featureKey}: unknown period '${period}' ` +
          `(expected one of ${[...FEATURE_LIMIT_PERIODS].sort().join(", ")})`,
      );
    }
    if (!FEATURE_LIMIT_ACTIONS.has(action)) {
      throw new ConfigError(
        `invalid featureLimits in plans.${planKey}.${featureKey}: unknown action '${action}' ` +
          `(expected one of ${[...FEATURE_LIMIT_ACTIONS].sort().join(", ")})`,
      );
    }
    out[featureKey] = {
      maxCalls,
      period: period as FeatureLimitPeriod,
      action: action as FeatureLimit["action"],
    };
  }
  return out;
}

/**
 * Normalise + validate a plan's `perOperation` map (per-operation
 * financial-safety policy overrides, mirrors Python's `OperationPolicy`).
 * Each entry's own keys are snake/camel-normalised since, unlike top-level
 * plan fields, these nested objects are not otherwise touched by
 * `normaliseKeys`.
 */
function normalisePerOperation(
  planKey: string,
  raw: Record<string, unknown>,
): Record<string, OperationPolicy> {
  const out: Record<string, OperationPolicy> = {};
  for (const [opType, rawPolicy] of Object.entries(raw)) {
    const policy = normaliseKeys((rawPolicy ?? {}) as Record<string, unknown>);
    assertKnownKeys(policy, OPERATION_POLICY_KEYS, `plans.${planKey}.perOperation.${opType}`);
    const billingMode = (policy.billingMode as string | undefined) ?? "strict";
    if (billingMode !== "strict" && billingMode !== "overdraft") {
      throw new ConfigError(
        `invalid billingMode in plans.${planKey}.perOperation.${opType}: '${billingMode}' ` +
          `(expected 'strict' or 'overdraft')`,
      );
    }
    out[opType] = {
      billingMode: billingMode as BillingMode,
      maxConcurrent: (policy.maxConcurrent as number | null) ?? null,
      overdraftFloor:
        policy.overdraftFloor != null ? new Decimal(policy.overdraftFloor as number | string) : null,
    };
  }
  return out;
}

/** Load and validate a pricing config from a raw dictionary. */
export function loadConfigFromDict(data: Record<string, unknown>): PricingConfig {
  // H6: normalise top-level keys from snake_case to camelCase first.
  const d = normaliseKeys(data);
  assertKnownKeys(d, TOP_LEVEL_KEYS, "config");

  // Only `1` is a valid version (mirrors Python's `Literal[1]`); JS previously
  // never inspected this field at all.
  if (d.version !== undefined && d.version !== 1) {
    throw new ConfigError(`version must be 1, got ${JSON.stringify(d.version)}`);
  }

  if (d.models == null) throw new ConfigError("missing required section: models");
  if (typeof d.models !== "object" || Object.keys(d.models as object).length === 0) {
    throw new ConfigError("models must be a non-empty dict");
  }

  // Validate plan rate overrides and duplicate names
  const rawPlans = d.plans as Record<string, Record<string, unknown>> | undefined;
  // Normalise each plan's keys too (free_allowance etc.)
  const plans = rawPlans
    ? Object.fromEntries(Object.entries(rawPlans).map(([k, v]) => [k, normalisePlan(v)]))
    : undefined;

  if (plans) {
    for (const [planKey, plan] of Object.entries(plans)) {
      assertKnownKeys(plan, PLAN_KEYS, `plans.${planKey}`);
      // A plan definition must carry a `name` (mirrors Python's `config.py:62`);
      // JS previously left this unchecked and silently produced `undefined`.
      if (plan.name == null) {
        throw new ConfigError(`plan definition is missing required 'name' field: plans.${planKey}`);
      }
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
      // WS9b: allowancePeriod must be one of the three valid strings if present.
      if (plan.allowancePeriod != null && !ALLOWANCE_PERIODS.has(plan.allowancePeriod as string)) {
        throw new ConfigError(
          `invalid allowancePeriod in plans.${planKey}: ${String(plan.allowancePeriod)} ` +
            `(expected one of ${[...ALLOWANCE_PERIODS].sort().join(", ")})`,
        );
      }
      // Per-feature invocation-count limits: normalise nested keys + validate.
      if (plan.featureLimits != null) {
        if (typeof plan.featureLimits !== "object" || Array.isArray(plan.featureLimits)) {
          throw new ConfigError(`plans.${planKey}.featureLimits must be a dict`);
        }
        plan.featureLimits = normaliseFeatureLimits(
          planKey,
          plan.featureLimits as Record<string, unknown>,
        );
      }
      // Per-operation financial-safety policy overrides: validate shape here;
      // normalised into `OperationPolicy` objects when `planDefs` is built below.
      if (plan.perOperation != null) {
        if (typeof plan.perOperation !== "object" || Array.isArray(plan.perOperation)) {
          throw new ConfigError(`plans.${planKey}.perOperation must be a dict`);
        }
      }
    }
    const planNames = Object.values(plans).map((p) => p.name as string);
    if (new Set(planNames).size !== planNames.length) {
      throw new ConfigError("duplicate plan names in pricing config");
    }
  }

  // Credit tiers (sibling to `plans`): validate + normalise. An explicit empty
  // `tiers: {}` is a config error (ambiguous intent — omit the key entirely
  // for "no tiers"), mirroring the Python `config.py` validator exactly.
  if (d.tiers !== undefined && d.tiers !== null) {
    if (typeof d.tiers !== "object" || Array.isArray(d.tiers)) {
      throw new ConfigError("tiers must be a dict of tier definitions");
    }
    if (Object.keys(d.tiers as object).length === 0) {
      throw new ConfigError(
        "tiers must not be an empty object; omit the `tiers` key entirely for no tiers",
      );
    }
  }
  const rawTiers = d.tiers as Record<string, Record<string, unknown>> | undefined;
  const tiers = rawTiers
    ? Object.fromEntries(Object.entries(rawTiers).map(([k, v]) => [k, normaliseTier(v)]))
    : undefined;

  if (tiers) {
    let overdraftCount = 0;
    let defaultCount = 0;
    for (const [tierKey, t] of Object.entries(tiers)) {
      assertKnownKeys(t, TIER_KEYS, `tiers.${tierKey}`);
      if (t.allowOverdraft === true) overdraftCount++;
      if (t.isDefault === true) defaultCount++;
      if (t.defaultTtlDays != null && (t.defaultTtlDays as number) <= 0) {
        throw new ConfigError(
          `tiers.${tierKey}.defaultTtlDays must be > 0, got ${String(t.defaultTtlDays)}`,
        );
      }
    }
    if (overdraftCount > 1) {
      throw new ConfigError("at most one tier may set allowOverdraft: true");
    }
    if (defaultCount > 1) {
      throw new ConfigError("at most one tier may set isDefault: true");
    }
  }

  const config: PricingConfig = {
    models: d.models as Record<string, string>,
    // Only default `tools` when the key is entirely absent (mirrors Python's
    // pydantic `default_factory`); a user-supplied `tools` map — even one
    // without `_default` — is used as-is. `PricingEngine.calcTools` already
    // falls back to `"tool_calls * 0"` at evaluation time when `_default` is
    // missing, so behavior is unaffected either way.
    tools: (d.tools as Record<string, string> | undefined) ?? { _default: "tool_calls * 0" },
    search: (d.search as string | null | undefined) ?? null,
    cache: (d.cache as string | null | undefined) ?? null,
    // Money fields: Decimal, never a binary `number` (contract §1).
    minBalance: new Decimal((d.minBalance as number | string | undefined) ?? 0),
    signupBonus: d.signupBonus as number | undefined,
    fixed: Object.fromEntries(
      Object.entries((d.fixed as Record<string, number | string> | undefined) ?? {}).map(
        ([job, cost]) => [job, new Decimal(cost)],
      ),
    ),
  };
  if (config.minBalance.isNegative()) throw new ConfigError("min_balance must be >= 0");

  if (config.signupBonus != null && config.signupBonus < 0) {
    throw new ConfigError(`signup_bonus must be >= 0, got ${config.signupBonus}`);
  }

  // WS3: fixed job costs may be fractional (Decimal-compatible) — only non-negative
  // is enforced. Was: Number.isInteger check (contradicted docs).
  for (const [job, cost] of Object.entries(config.fixed)) {
    if (cost.isNegative()) {
      throw new ConfigError(`fixed.${job} must be non-negative, got ${cost.toString()}`);
    }
  }

  if (plans) {
    const planDefs: Record<string, PlanDefinition> = {};
    for (const [key, p] of Object.entries(plans)) {
      const freeAllowance = new Decimal((p.freeAllowance as number | string | undefined) ?? 0);
      if (freeAllowance.isNegative()) {
        throw new ConfigError(`plans.${key}.freeAllowance must be >= 0, got ${freeAllowance.toString()}`);
      }
      planDefs[key] = {
        id: (p.id as string) ?? key,
        name: p.name as string,
        freeAllowance,
        rateOverrides: (p.rateOverrides as Record<string, string>) ?? null,
        features: (p.features as Record<string, unknown>) ?? null,
        featureLimits: (p.featureLimits as Record<string, FeatureLimit>) ?? null,
        defaultBillingMode: ((p.defaultBillingMode ?? p.billingMode) as "strict" | "overdraft") ?? "strict",
        overdraftFloor:
          p.overdraftFloor != null ? new Decimal(p.overdraftFloor as number | string) : null,
        maxConcurrent: (p.maxConcurrent as number | null) ?? null,
        allowancePeriod: ((p.allowancePeriod as AllowancePeriod) ?? "calendar_month") as AllowancePeriod,
        // Was silently dropped: `PlanDefinition.perOperation` was declared in
        // types.ts but never populated here, so per-operation billing policy
        // from config was ignored by the JS SDK.
        perOperation:
          p.perOperation != null
            ? normalisePerOperation(key, p.perOperation as Record<string, unknown>)
            : undefined,
      };
    }
    config.plans = planDefs;
  }

  if (tiers) {
    const tierDefs: Record<string, TierDefinition> = {};
    for (const [key, t] of Object.entries(tiers)) {
      tierDefs[key] = {
        name: (t.name as string | undefined) ?? key,
        priority: Number(t.priority ?? 0),
        expires: Boolean(t.expires ?? false),
        defaultTtlDays: t.defaultTtlDays != null ? Number(t.defaultTtlDays) : null,
        allowOverdraft: Boolean(t.allowOverdraft ?? false),
        isDefault: Boolean(t.isDefault ?? false),
      };
    }
    config.tiers = tierDefs;
  }

  validateExpressions(config);
  return config;
}
