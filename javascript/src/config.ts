import Decimal from "decimal.js";
import { ConfigError } from "./errors.js";
import { validateExpression } from "./expr.js";
import type { PlanDefinition } from "./types.js";

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
  search: Record<string, string>;
  cache: Record<string, string>;
  minBalance: number;
  fixed: Record<string, number>;
  plans?: Record<string, PlanDefinition> | null;
}

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
      validateExpression(expr, KNOWN_VARIABLES);
    } catch (e) {
      throw new ConfigError(`invalid expression in tools.${key}: ${(e as Error).message}`);
    }
  }
  for (const [key, expr] of Object.entries(raw.search)) {
    try {
      validateExpression(expr, KNOWN_VARIABLES);
    } catch (e) {
      throw new ConfigError(`invalid expression in search.${key}: ${(e as Error).message}`);
    }
  }
  for (const [key, expr] of Object.entries(raw.cache)) {
    try {
      validateExpression(expr, KNOWN_VARIABLES);
    } catch (e) {
      throw new ConfigError(`invalid expression in cache.${key}: ${(e as Error).message}`);
    }
  }
}

/** Load and validate a pricing config from a raw dictionary. */
export function loadConfigFromDict(data: Record<string, unknown>): PricingConfig {
  if (data.models == null) throw new ConfigError("missing required section: models");
  if (typeof data.models !== "object" || Object.keys(data.models as object).length === 0) {
    throw new ConfigError("models must be a non-empty dict");
  }

  // Validate plan rate overrides and duplicate names
  const plans = data.plans as Record<string, Record<string, unknown>> | undefined;
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
    }
    const planNames = Object.values(plans).map((p) => p.name as string);
    if (new Set(planNames).size !== planNames.length) {
      throw new ConfigError("duplicate plan names in pricing config");
    }
  }

  const config: PricingConfig = {
    models: data.models as Record<string, string>,
    tools: { _default: "tool_calls * 0", ...(data.tools as Record<string, string> | undefined) },
    search: (data.search as Record<string, string>) ?? {},
    cache: (data.cache as Record<string, string>) ?? {},
    minBalance: (data.minBalance as number) ?? 5,
    fixed: (data.fixed as Record<string, number>) ?? {},
  };
  if (config.minBalance < 0) throw new ConfigError("min_balance must be >= 0");

  if (plans) {
    const planDefs: Record<string, PlanDefinition> = {};
    for (const [key, p] of Object.entries(plans)) {
      planDefs[key] = {
        id: p.id as string,
        name: p.name as string,
        freeAllowance: new Decimal((p.freeAllowance as number | string | undefined) ?? 0),
        rateOverrides: (p.rateOverrides as Record<string, string>) ?? null,
        features: (p.features as Record<string, unknown>) ?? null,
      };
    }
    config.plans = planDefs;
  }

  validateExpressions(config);
  return config;
}
