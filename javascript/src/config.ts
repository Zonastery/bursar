import Ajv2020, { type ErrorObject } from "ajv/dist/2020.js";
import addFormats from "ajv-formats";
import Decimal from "decimal.js";

import schema from "./generated/pricing-config.schema.json" with { type: "json" };
import { ConfigError } from "./errors.js";
import { parseAdmission, parseEntitlements } from "./config/parse-access.js";
import { parseCommerce } from "./config/parse-commerce.js";
import { parseCredits } from "./config/parse-credits.js";
import { parsePlans } from "./config/parse-plans.js";
import { parsePricing } from "./config/parse-pricing.js";
import { asObject, identifier, semanticError, type JsonObject } from "./config/parse-utils.js";
import type {
  BursarConfigData,
  CatalogRollout,
  ParsedBursarConfig,
  PlanRolloutStrategy,
  SubscriptionOffer,
} from "./config/types.js";

export type * from "./config/types.js";

const ajv = new Ajv2020({
  allErrors: true,
  strict: false,
  // Configs loaded from files cannot contain these values, so reject them at
  // the programmatic dictionary boundary as well. Decimal accepts Infinity
  // and NaN, which would otherwise leak invalid monetary values downstream.
  strictNumbers: true,
});
addFormats(ajv, { mode: "full" });
const validateStructure = ajv.compile(schema);

function formatAjvErrors(errors: ErrorObject[] | null | undefined): string {
  if (!errors?.length) return "configuration does not match the Bursar schema";
  return errors
    .map((error) => `${error.instancePath || "$"} ${error.message ?? error.keyword}`)
    .join("; ");
}

export function loadConfigFromDict(
  data: BursarConfigData | Record<string, unknown>,
): ParsedBursarConfig {
  if (!validateStructure(data)) {
    throw new ConfigError(
      formatAjvErrors(validateStructure.errors),
      validateStructure.errors ?? [],
    );
  }

  const raw = data as JsonObject;
  const pricing = raw.pricing == null ? undefined : parsePricing(raw.pricing);
  const credits = parseCredits(raw.credits);
  const entitlements = parseEntitlements(raw.entitlements);
  const admission = parseAdmission(raw.admission);

  if (pricing != null) {
    for (const [policyKey, policy] of Object.entries(admission.policies)) {
      const unknown = Object.keys(policy.operations).filter(
        (operation) => !pricing.operations[operation],
      );
      if (unknown.length) {
        semanticError(
          `admission policy '${policyKey}' references unknown operations ${unknown.join(", ")}`,
        );
      }
    }
  }

  const commerce = parseCommerce(raw.commerce, credits);
  const subscriptionPlans = new Set(
    Object.values(commerce.offers)
      .filter((offer): offer is SubscriptionOffer => offer.type === "subscription")
      .map((offer) => offer.plan),
  );
  const plans = parsePlans(raw.plans, pricing, credits, entitlements, admission, subscriptionPlans);

  for (const [offerKey, offer] of Object.entries(commerce.offers)) {
    if (offer.type === "subscription" && !plans[offer.plan]) {
      semanticError(`commerce.offers.${offerKey}.plan references unknown plan '${offer.plan}'`);
    }
  }
  for (const [programKey, program] of Object.entries(credits.grantPrograms)) {
    const unknown = program.eligibility.plans.filter((plan) => !plans[plan]);
    if (unknown.length) {
      semanticError(`grant program '${programKey}' references unknown plans ${unknown.join(", ")}`);
    }
  }

  const catalogRaw = asObject(raw.catalog ?? {});
  const defaultPlan = catalogRaw.default_plan == null ? undefined : String(catalogRaw.default_plan);
  if (defaultPlan != null && !plans[defaultPlan]) {
    semanticError(`catalog.default_plan references unknown plan '${defaultPlan}'`);
  }
  return {
    version: 1,
    catalog: {
      ...(defaultPlan == null ? {} : { defaultPlan }),
    },
    ...(pricing == null ? {} : { pricing }),
    credits,
    entitlements,
    admission,
    plans,
    commerce,
  };
}

function toSnakeCase(value: unknown): unknown {
  if (value instanceof Decimal) return value.toFixed(6);
  if (Array.isArray(value)) return value.map(toSnakeCase);
  if (typeof value !== "object" || value === null) return value;
  return Object.fromEntries(
    Object.entries(value as JsonObject)
      .filter(([, child]) => child !== undefined)
      .map(([key, child]) => [
        key.replace(/[A-Z]/g, (letter) => `_${letter.toLowerCase()}`),
        toSnakeCase(child),
      ]),
  );
}

export function canonicalBursarConfigDict(
  data: BursarConfigData | Record<string, unknown>,
): BursarConfigData & Record<string, unknown> {
  return toSnakeCase(loadConfigFromDict(data)) as BursarConfigData & Record<string, unknown>;
}

/** Serialize an already parsed configuration without re-validating camelCase fields as raw input. */
export function canonicalParsedBursarConfigDict(
  data: ParsedBursarConfig,
): BursarConfigData & Record<string, unknown> {
  return toSnakeCase(data) as BursarConfigData & Record<string, unknown>;
}

const ROLLOUT_STRATEGIES = new Set<PlanRolloutStrategy>([
  "immediate",
  "next_renewal",
  "new_assignments_only",
]);

function requirePlainObject(value: unknown, path: string): JsonObject {
  if (value == null || Array.isArray(value) || typeof value !== "object") {
    throw new ConfigError(`${path} must be an object`);
  }
  return value as JsonObject;
}

function rejectUnknownKeys(value: JsonObject, allowed: readonly string[], path: string): void {
  const unknown = Object.keys(value).filter((key) => !allowed.includes(key));
  if (unknown.length > 0) {
    throw new ConfigError(`${path} contains unknown field '${unknown[0]}'`);
  }
}

/** Parse a one-release catalog rollout manifest. */
export function loadCatalogRollout(data: unknown = {}): CatalogRollout {
  const raw = requirePlainObject(data, "rollout");
  rejectUnknownKeys(raw, ["plans"], "rollout");
  const plansRaw = raw.plans == null ? {} : requirePlainObject(raw.plans, "rollout.plans");
  const plans: CatalogRollout["plans"] = {};

  for (const [planKey, value] of Object.entries(plansRaw)) {
    identifier(planKey, `rollout.plans.${planKey}`);
    const plan = requirePlainObject(value, `rollout.plans.${planKey}`);
    rejectUnknownKeys(
      plan,
      ["effective", "include_pinned", "includePinned"],
      `rollout.plans.${planKey}`,
    );
    if (!ROLLOUT_STRATEGIES.has(plan.effective as PlanRolloutStrategy)) {
      throw new ConfigError(
        `rollout.plans.${planKey}.effective must be immediate, next_renewal, or new_assignments_only`,
      );
    }
    if (plan.include_pinned != null && plan.includePinned != null) {
      throw new ConfigError(
        `rollout.plans.${planKey} must not set both include_pinned and includePinned`,
      );
    }
    const includePinned = plan.include_pinned ?? plan.includePinned;
    if (includePinned != null && typeof includePinned !== "boolean") {
      throw new ConfigError(`rollout.plans.${planKey}.include_pinned must be boolean`);
    }
    plans[planKey] = {
      effective: plan.effective as PlanRolloutStrategy,
      includePinned: includePinned === true,
    };
  }

  return { plans };
}

/** Serialize a rollout manifest for the catalog activation RPC. */
export function canonicalCatalogRolloutDict(data: unknown = {}): Record<string, unknown> {
  return toSnakeCase(loadCatalogRollout(data)) as Record<string, unknown>;
}

/** Validate rollout references and renewal timing against its target catalog. */
export function validateCatalogRollout(
  config: ParsedBursarConfig,
  rollout: CatalogRollout,
): CatalogRollout {
  const subscriptionPlans = new Set(
    Object.values(config.commerce.offers)
      .filter((offer): offer is SubscriptionOffer => offer.type === "subscription")
      .map((offer) => offer.plan),
  );
  for (const [planKey, policy] of Object.entries(rollout.plans)) {
    if (!config.plans[planKey]) {
      throw new ConfigError(`rollout.plans references unknown plan '${planKey}'`);
    }
    if (policy.effective === "next_renewal" && !subscriptionPlans.has(planKey)) {
      throw new ConfigError(
        `rollout.plans.${planKey}.effective=next_renewal requires a subscription offer`,
      );
    }
  }
  return rollout;
}
