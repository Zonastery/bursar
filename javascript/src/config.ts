import { ConfigError } from "./errors.js";
import { validateExpression } from "./expr.js";

/** Internal validated pricing configuration. */
export interface PricingConfig {
  version: number;
  models: Record<string, string>;
  tools: Record<string, string>;
  search: Record<string, string>;
  cache: Record<string, string>;
  minBalance: number;
  fixed: Record<string, number>;
}

function validateConfigData(data: Record<string, unknown>): void {
  if (data.version == null) throw new ConfigError("missing required field: version");
  if (data.version !== 1 && data.version !== 2)
    throw new ConfigError(`unsupported version: ${data.version}`);
  if (data.models == null) throw new ConfigError("missing required section: models");
  if (typeof data.models !== "object" || Object.keys(data.models as object).length === 0) {
    throw new ConfigError("models must be a non-empty dict");
  }
  // Validate plan rate overrides for v2 configs
  if (data.version === 2) {
    const plans = data.plans as Record<string, Record<string, unknown>> | undefined;
    if (plans) {
      for (const [planKey, plan] of Object.entries(plans)) {
        const overrides = plan.rateOverrides as Record<string, string> | undefined;
        if (overrides) {
          for (const [modelKey, expr] of Object.entries(overrides)) {
            try {
              validateExpression(expr);
            } catch (e) {
              throw new ConfigError(
                `invalid expression in plans.${planKey}.rateOverrides.${modelKey}: ${(e as Error).message}`,
              );
            }
          }
        }
      }
      // Validate duplicate plan names
      const planNames = Object.values(plans).map((p) => p.name as string);
      if (new Set(planNames).size !== planNames.length) {
        throw new ConfigError("duplicate plan names in pricing config");
      }
    }
  }
}

function validateExpressions(raw: PricingConfig): void {
  for (const [key, expr] of Object.entries(raw.models)) {
    try {
      validateExpression(expr);
    } catch (e) {
      throw new ConfigError(`invalid expression in models.${key}: ${(e as Error).message}`);
    }
  }
  for (const [key, expr] of Object.entries(raw.tools)) {
    try {
      validateExpression(expr);
    } catch (e) {
      throw new ConfigError(`invalid expression in tools.${key}: ${(e as Error).message}`);
    }
  }
  for (const [key, expr] of Object.entries(raw.search)) {
    try {
      validateExpression(expr);
    } catch (e) {
      throw new ConfigError(`invalid expression in search.${key}: ${(e as Error).message}`);
    }
  }
  for (const [key, expr] of Object.entries(raw.cache)) {
    try {
      validateExpression(expr);
    } catch (e) {
      throw new ConfigError(`invalid expression in cache.${key}: ${(e as Error).message}`);
    }
  }
}

/** Load and validate a pricing config from a raw dictionary. */
export function loadConfigFromDict(data: Record<string, unknown>): PricingConfig {
  validateConfigData(data);
  const config: PricingConfig = {
    version: data.version as number,
    models: data.models as Record<string, string>,
    tools: { _default: "tool_calls * 0", ...(data.tools as Record<string, string> | undefined) },
    search: (data.search as Record<string, string>) ?? {},
    cache: (data.cache as Record<string, string>) ?? {},
    minBalance: (data.minBalance as number) ?? 5,
    fixed: (data.fixed as Record<string, number>) ?? {},
  };
  if (config.minBalance < 0) throw new ConfigError("min_balance must be >= 0");
  validateExpressions(config);
  if (data.version === 2) {
    return {
      ...config,
      plans: data.plans as Record<string, unknown> | undefined,
    } as PricingConfig & { plans?: Record<string, unknown> };
  }
  return config;
}
