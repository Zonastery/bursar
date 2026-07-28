import Ajv2020, { type ErrorObject } from "ajv/dist/2020.js";
import addFormats from "ajv-formats";
import Decimal from "decimal.js";

import schema from "./generated/pricing-config.schema.json";
import { ConfigError } from "./errors.js";
import { parseAdmission, parseEntitlements } from "./config/parse-access.js";
import { parseCommerce } from "./config/parse-commerce.js";
import { parseCredits } from "./config/parse-credits.js";
import { parsePlans } from "./config/parse-plans.js";
import { parsePricing } from "./config/parse-pricing.js";
import { asObject, semanticError, type JsonObject } from "./config/parse-utils.js";
import type { BursarConfigData, ParsedBursarConfig, SubscriptionOffer } from "./config/types.js";

export type * from "./config/types.js";

const ajv = new Ajv2020({
  allErrors: true,
  strict: false,
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
  asObject(catalogRaw.activation ?? { mode: "on_publish" });
  return {
    version: 1,
    catalog: { activation: { mode: "on_publish" } },
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
): BursarConfigData {
  return toSnakeCase(loadConfigFromDict(data)) as BursarConfigData;
}

/** Serialize an already parsed configuration without re-validating camelCase fields as raw input. */
export function canonicalParsedBursarConfigDict(data: ParsedBursarConfig): BursarConfigData {
  return toSnakeCase(data) as BursarConfigData;
}
