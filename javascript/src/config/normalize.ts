import { ConfigError } from "../errors.js";
import { isJsonObject, type JsonObject } from "../shared/json.js";

import { PROVIDER_ENVIRONMENTS, type ProviderEnvironment } from "../providers/environment.js";
const object = isJsonObject;

/** Resolve shorthand and provider bindings for the validated wire configuration. */
export function normalizeConfig(data: JsonObject, environment?: ProviderEnvironment): JsonObject {
  if (environment != null && !PROVIDER_ENVIRONMENTS.includes(environment))
    throw new ConfigError("invalid provider environment");
  const result: JsonObject = structuredClone(data);
  const credits = (result.credits ??= {});
  if (!object(credits)) throw new ConfigError("credits must be an object");
  const hasSignup = "on_signup" in credits;
  const signup = credits.on_signup;
  delete credits.on_signup;
  if (!("buckets" in credits)) {
    credits.buckets = { default: { priority: 0 } };
    if (!("default_bucket" in credits)) credits.default_bucket = "default";
  }
  if (hasSignup && signup != null) {
    if (typeof credits.default_bucket !== "string")
      throw new ConfigError("credits.on_signup requires credits.default_bucket");
    const programs = (credits.grant_programs ??= {});
    if (
      !object(programs) ||
      "signup" in programs ||
      Object.values(programs).some(
        (program) => object(program) && program.trigger === "account_created",
      )
    )
      throw new ConfigError("credits.on_signup conflicts with an account_created grant program");
    programs.signup = {
      trigger: "account_created",
      awards: [{ amount: signup, bucket: credits.default_bucket }],
    };
  }
  const pricing = result.pricing;
  if (object(pricing) && object(pricing.operations)) {
    const cards = (pricing.rate_cards ??= {});
    if (!object(cards)) throw new ConfigError("pricing.rate_cards must be an object");
    const shorthand: JsonObject = {};
    for (const [key, operation] of Object.entries(pricing.operations)) {
      if (!object(operation)) continue;
      if ("charge" in operation) {
        if (operation.charge != null)
          shorthand[key] = { unmatched: { action: "charge", charge: operation.charge } };
        delete operation.charge;
      }
    }
    if (Object.keys(shorthand).length) {
      if (!Object.keys(cards).length) cards.standard = { operations: {} };
      for (const [cardKey, card] of Object.entries(cards)) {
        if (!object(card)) throw new ConfigError(`pricing.rate_cards.${cardKey} must be an object`);
        if (card.extends != null) continue;
        const operations = (card.operations ??= {});
        if (!object(operations))
          throw new ConfigError(`pricing.rate_cards.${cardKey}.operations must be an object`);
        for (const [key, value] of Object.entries(shorthand)) {
          if (!(key in operations)) operations[key] = value;
        }
      }
    }
  }
  if (object(result.plans))
    for (const plan of Object.values(result.plans)) {
      if (!object(plan)) continue;
      if (object(pricing) && object(pricing.rate_cards)) {
        const keys = Object.keys(pricing.rate_cards);
        if (keys.length === 1 && keys[0] !== undefined && plan.rate_card == null)
          plan.rate_card = keys[0];
      }
    }
  if (
    object(result.commerce) &&
    object(result.commerce.providers) &&
    object(result.commerce.offers)
  ) {
    const definitions = result.commerce.providers;
    for (const offer of Object.values(result.commerce.offers)) {
      if (!object(offer) || !object(offer.providers)) continue;
      for (const [key, reference] of Object.entries(offer.providers)) {
        const provider = definitions[key];
        if (!object(reference) || !object(provider) || "type" in reference) continue;
        if (provider.type === "stripe") reference.type = "stripe_price";
        if (provider.type === "dodo") reference.type = "dodo_product";
      }
    }
  }

  if (environment != null && object(result.commerce) && object(result.commerce.offers)) {
    for (const offer of Object.values(result.commerce.offers)) {
      if (!object(offer) || !object(offer.providers)) continue;
      for (const reference of Object.values(offer.providers)) {
        if (!object(reference)) continue;
        for (const field of ["price_id", "product_id", "external_id"]) {
          const value = reference[field];
          if (typeof value === "string") reference[field] = { [environment]: value };
        }
      }
    }
  }
  return result;
}
