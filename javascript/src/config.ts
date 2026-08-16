import Ajv2020, { type ErrorObject } from "ajv/dist/2020.js";
import addFormats from "ajv-formats";
import { Decimal } from "decimal.js";
import { z } from "zod";

import schema from "./generated/pricing-config.schema.json" with { type: "json" };
import { ConfigError } from "./errors.js";
import { parseAdmission, parseEntitlements } from "./config/parse-access.js";
import { parseCommerce } from "./config/parse-commerce.js";
import { parseCredits } from "./config/parse-credits.js";
import { parsePlans } from "./config/parse-plans.js";
import { parsePricing } from "./config/parse-pricing.js";
import { asObject, identifier, semanticError, type JsonObject } from "./config/parse-utils.js";
import type { JsonValue } from "./shared/json.js";
import type {
  BursarConfigData,
  CatalogRollout,
  ParsedBursarConfig,
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
const validateStructure = ajv.compile<BursarConfigData>(schema);

function formatAjvErrors(errors: ErrorObject[] | null | undefined): string {
  if (!errors?.length) return "configuration does not match the Bursar schema";
  return errors
    .map((error) => `${error.instancePath || "$"} ${error.message ?? error.keyword}`)
    .join("; ");
}

export function loadConfigFromDict<T extends object>(data: T): ParsedBursarConfig {
  if (!validateStructure(data)) {
    throw new ConfigError(
      formatAjvErrors(validateStructure.errors),
      validateStructure.errors ?? [],
    );
  }

  const raw = asObject(z.record(z.string(), z.json()).parse(data), "config");
  const pricing = raw.pricing == null ? undefined : parsePricing(raw.pricing);
  const credits = parseCredits(asObject(raw.credits, "credits"));
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
  if (Object.keys(plans).length > 0 && defaultPlan == null) {
    semanticError("catalog.default_plan is required when plans are configured");
  }
  const result: ParsedBursarConfig = {
    version: 1,
    catalog: {},
    credits,
    entitlements,
    admission,
    plans,
    commerce,
  };
  if (defaultPlan != null) result.catalog.defaultPlan = defaultPlan;
  if (pricing != null) result.pricing = pricing;
  return result;
}

type SnakeCaseObject = JsonObject | BursarConfigData | ParsedBursarConfig | CatalogRollout;
type SnakeCaseValue = JsonValue | Decimal | SnakeCaseObject;

function isSnakeCaseObject(value: SnakeCaseValue): value is SnakeCaseObject {
  return Object.prototype.toString.call(value) === "[object Object]";
}

function toSnakeCase(value: SnakeCaseValue): JsonValue {
  if (value instanceof Decimal) return value.toFixed(6);
  const primitive = z.union([z.string(), z.number(), z.boolean(), z.null()]).safeParse(value);
  if (primitive.success) return primitive.data;
  if (Array.isArray(value)) return value.map((child) => toSnakeCase(child));
  if (!isSnakeCaseObject(value))
    throw new ConfigError("configuration contains an unsupported value");
  const result: JsonObject = {};
  for (const [key, child] of Object.entries(value)) {
    if (child === undefined) continue;
    result[key.replace(/[A-Z]/g, (letter) => `_${letter.toLowerCase()}`)] = toSnakeCase(child);
  }
  return result;
}

function canonicalConfig(data: ParsedBursarConfig): BursarConfigData {
  const canonical = asObject(toSnakeCase(data), "canonical config");
  if (!validateStructure(canonical)) {
    throw new ConfigError(
      `canonical configuration is invalid: ${formatAjvErrors(validateStructure.errors)}`,
      validateStructure.errors ?? [],
    );
  }
  return canonical;
}

export function canonicalBursarConfigDict<T extends object>(data: T): BursarConfigData {
  return canonicalConfig(loadConfigFromDict(data));
}

/** Serialize an already parsed configuration without re-validating camelCase fields as raw input. */
export function canonicalParsedBursarConfigDict(data: ParsedBursarConfig): BursarConfigData {
  return canonicalConfig(data);
}

const rolloutStrategySchema = z.enum(["immediate", "next_renewal", "new_assignments_only"]);

function requirePlainObject(value: JsonValue | undefined, path: string): JsonObject {
  if (value == null || Array.isArray(value)) {
    throw new ConfigError(`${path} must be an object`);
  }
  const candidate = asObject(value, path);
  return candidate;
}

function rejectUnknownKeys(value: JsonObject, allowed: readonly string[], path: string): void {
  const unknown = Object.keys(value).filter((key) => !allowed.includes(key));
  if (unknown.length > 0) {
    throw new ConfigError(`${path} contains unknown field '${unknown[0]}'`);
  }
}

const parsedCatalogRolloutSchema = z
  .object({
    plans: z.record(
      z.string(),
      z.object({
        effective: z.enum(["immediate", "next_renewal", "new_assignments_only"]),
        includePinned: z.boolean(),
      }),
    ),
  })
  .strict();

/** Parse a one-release catalog rollout manifest. */
export function loadCatalogRollout<T>(data?: T): CatalogRollout {
  const candidate = data === undefined ? {} : data;
  const parsed = parsedCatalogRolloutSchema.safeParse(candidate);
  if (parsed.success) return parsed.data;
  const jsonValue = z.json().safeParse(candidate);
  if (!jsonValue.success) throw new ConfigError("rollout must be a JSON object");
  const raw = requirePlainObject(jsonValue.data, "rollout");
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
    const effective = rolloutStrategySchema.safeParse(plan.effective);
    if (!effective.success) {
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
    const parsedIncludePinned =
      includePinned == null
        ? { success: true as const, data: false }
        : z.boolean().safeParse(includePinned);
    if (!parsedIncludePinned.success) {
      throw new ConfigError(`rollout.plans.${planKey}.include_pinned must be boolean`);
    }
    plans[planKey] = { effective: effective.data, includePinned: parsedIncludePinned.data };
  }

  return { plans };
}

/** Serialize a rollout manifest for the catalog activation RPC. */
export function canonicalCatalogRolloutDict<T>(data?: T): JsonObject {
  return asObject(toSnakeCase(loadCatalogRollout(data)), "canonical rollout");
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
