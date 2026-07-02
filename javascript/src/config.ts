import Decimal from "decimal.js";
import { ConfigError } from "./errors.js";
import { validateExpression } from "./expr.js";
import type { AllowancePeriod, FeatureLimitPeriod } from "./allowance.js";
import type { FeatureLimit, PlanDefinition, TierDefinition } from "./types.js";

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
  minBalance: number;
  signupBonus?: number | null;
  fixed: Record<string, number>;
  plans?: Record<string, PlanDefinition> | null;
  tiers?: Record<string, TierDefinition> | null;
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

/** Load and validate a pricing config from a raw dictionary. */
export function loadConfigFromDict(data: Record<string, unknown>): PricingConfig {
  // H6: normalise top-level keys from snake_case to camelCase first.
  const d = normaliseKeys(data);

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
    tools: { _default: "tool_calls * 0", ...(d.tools as Record<string, string> | undefined) },
    search: (d.search as string | null | undefined) ?? null,
    cache: (d.cache as string | null | undefined) ?? null,
    minBalance: (d.minBalance as number) ?? 0,
    signupBonus: d.signupBonus as number | undefined,
    fixed: (d.fixed as Record<string, number>) ?? {},
  };
  if (config.minBalance < 0) throw new ConfigError("min_balance must be >= 0");

  // WS3: fixed job costs may be fractional (Decimal-compatible) — only non-negative
  // is enforced. Was: Number.isInteger check (contradicted docs).
  for (const [job, cost] of Object.entries(config.fixed)) {
    if (cost < 0) {
      throw new ConfigError(`fixed.${job} must be non-negative, got ${cost}`);
    }
  }

  if (plans) {
    const planDefs: Record<string, PlanDefinition> = {};
    for (const [key, p] of Object.entries(plans)) {
      planDefs[key] = {
        id: (p.id as string) ?? key,
        name: p.name as string,
        freeAllowance: new Decimal((p.freeAllowance as number | string | undefined) ?? 0),
        rateOverrides: (p.rateOverrides as Record<string, string>) ?? null,
        features: (p.features as Record<string, unknown>) ?? null,
        featureLimits: (p.featureLimits as Record<string, FeatureLimit>) ?? null,
        defaultBillingMode: ((p.defaultBillingMode ?? p.billingMode) as "strict" | "overdraft") ?? "strict",
        overdraftFloor:
          p.overdraftFloor != null ? new Decimal(p.overdraftFloor as number | string) : null,
        maxConcurrent: (p.maxConcurrent as number | null) ?? null,
        allowancePeriod: ((p.allowancePeriod as AllowancePeriod) ?? "calendar_month") as AllowancePeriod,
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
