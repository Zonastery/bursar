import { resolvesOperation } from "./parse-pricing.js";
import {
  asArray,
  asDecimal,
  asInteger,
  asObject,
  asString,
  asStringArray,
  identifier,
  parseWindow,
  semanticError,
  validateIdentifiers,
} from "./parse-utils.js";
import type { JsonValue } from "../shared/json.js";
import type {
  AdmissionPolicy,
  CreditsConfig,
  EntitlementsConfig,
  FeatureValue,
  PlanDefinition,
  PlanRolloutStrategy,
  PricingConfig,
  QuotaDefinition,
} from "./types.js";
import { z } from "zod";

const rolloutSchema = z.enum(["immediate", "next_renewal", "new_assignments_only"]);
const enforcementSchema = z.enum(["block", "allow"]);
interface PlanCatalog {
  [planKey: string]: PlanDefinition;
}

function parseFeatureValue(value: JsonValue, path: string): FeatureValue {
  const parsed = z.union([z.boolean(), z.number().finite(), z.string()]).safeParse(value);
  if (!parsed.success) semanticError(`${path} must be a boolean, number, or string`);
  return parsed.data;
}

function validateFeatureValue(
  planKey: string,
  featureKey: string,
  featureValue: FeatureValue,
  entitlements: EntitlementsConfig,
): void {
  const definition = entitlements.features[featureKey];
  if (!definition) {
    semanticError(`plans.${planKey} references unknown feature '${featureKey}'`);
  }
  if (definition.type === "boolean" && !z.boolean().safeParse(featureValue).success) {
    semanticError(`plans.${planKey}.features.${featureKey} must be boolean`);
  }
  const integerValue = z.number().int().safeParse(featureValue);
  if (definition.type === "integer" && !integerValue.success) {
    semanticError(`plans.${planKey}.features.${featureKey} must be integer`);
  }
  if (
    definition.type === "integer" &&
    definition.minimum != null &&
    integerValue.success &&
    integerValue.data < definition.minimum
  ) {
    semanticError(`plans.${planKey}.features.${featureKey} is below the feature minimum`);
  }
  if (
    definition.type === "integer" &&
    definition.maximum != null &&
    integerValue.success &&
    integerValue.data > definition.maximum
  ) {
    semanticError(`plans.${planKey}.features.${featureKey} exceeds the feature maximum`);
  }
  if (definition.type === "enum") {
    const stringValue = z.string().safeParse(featureValue);
    if (!stringValue.success || !definition.values.includes(stringValue.data)) {
      semanticError(`plans.${planKey}.features.${featureKey} has an invalid enum value`);
    }
  }
  if (definition.type === "string" && !z.string().safeParse(featureValue).success) {
    semanticError(`plans.${planKey}.features.${featureKey} must be string`);
  }
  if (
    definition.type === "string" &&
    definition.pattern != null &&
    !new RegExp(definition.pattern).test(String(featureValue))
  ) {
    semanticError(`plans.${planKey}.features.${featureKey} does not match the feature pattern`);
  }
}

export function parsePlans(
  value: JsonValue | undefined,
  pricing: PricingConfig | undefined,
  credits: CreditsConfig,
  entitlements: EntitlementsConfig,
  admission: { policies: Record<string, AdmissionPolicy> },
  subscriptionPlans: Set<string>,
): PlanCatalog {
  const raw = asObject(value ?? {});
  validateIdentifiers(raw, "plans");
  const plans: PlanCatalog = {};

  for (const [planKey, input] of Object.entries(raw)) {
    const plan = asObject(input);
    const evolution = plan.evolution == null ? undefined : asObject(plan.evolution);
    const parsedRollout = rolloutSchema.safeParse(
      evolution?.default_rollout ?? (subscriptionPlans.has(planKey) ? "next_renewal" : "immediate"),
    );
    if (!parsedRollout.success) {
      semanticError(`plans.${planKey}.evolution.default_rollout is invalid`);
    }
    const defaultRollout: PlanRolloutStrategy = parsedRollout.data;
    if (defaultRollout === "next_renewal" && !subscriptionPlans.has(planKey)) {
      semanticError(
        `plans.${planKey}.evolution.default_rollout=next_renewal requires a subscription offer`,
      );
    }
    const rateCard = plan.rate_card == null ? undefined : asString(plan.rate_card);
    const allowedOperations =
      plan.allowed_operations == null
        ? []
        : asStringArray(plan.allowed_operations, `plans.${planKey}.allowed_operations`);
    if (new Set(allowedOperations).size !== allowedOperations.length) {
      semanticError(`plans.${planKey}.allowed_operations must not contain duplicates`);
    }
    if (rateCard != null && !pricing?.rateCards[rateCard]) {
      semanticError(`plans.${planKey}.rate_card references unknown rate card '${rateCard}'`);
    }
    for (const operation of allowedOperations) {
      identifier(operation, `plans.${planKey}.allowed_operations`);
      if (!pricing?.operations[operation]) {
        semanticError(`plans.${planKey} references unknown operation '${operation}'`);
      }
      if (rateCard == null || !resolvesOperation(pricing, rateCard, operation)) {
        semanticError(`plans.${planKey} enables '${operation}' without pricing`);
      }
    }

    const features: PlanDefinition["features"] = Object.fromEntries(
      Object.entries(asObject(plan.features ?? {})).map(([featureKey, featureValue]) => [
        featureKey,
        parseFeatureValue(featureValue, `plans.${planKey}.features.${featureKey}`),
      ]),
    );
    validateIdentifiers(features, `plans.${planKey}.features`);
    for (const [featureKey, featureValue] of Object.entries(features)) {
      validateFeatureValue(planKey, featureKey, featureValue, entitlements);
    }

    const quotasRaw = asObject(plan.quotas ?? {});
    validateIdentifiers(quotasRaw, `plans.${planKey}.quotas`);
    const quotas: Record<string, QuotaDefinition> = {};
    for (const [quotaKey, quotaInput] of Object.entries(quotasRaw)) {
      const quota = asObject(quotaInput);
      const operation = asString(quota.operation);
      const measure = asString(quota.measure);
      const emitAtPercent =
        quota.emit_at_percent == null
          ? [100]
          : asArray(quota.emit_at_percent, `${planKey}.${quotaKey}.emit_at_percent`).map(
              (threshold, index) =>
                asInteger(
                  threshold,
                  `plans.${planKey}.quotas.${quotaKey}.emit_at_percent[${index}]`,
                ),
            );
      if (
        emitAtPercent.some((threshold) => threshold < 1 || threshold > 100) ||
        emitAtPercent.some((threshold, index) => {
          const previous = emitAtPercent[index - 1];
          return index > 0 && previous !== undefined && threshold <= previous;
        })
      ) {
        semanticError(
          `plans.${planKey}.quotas.${quotaKey}.emit_at_percent must be unique, increasing, and between 1 and 100`,
        );
      }
      if (!pricing?.operations[operation]?.measures[measure]) {
        semanticError(
          `plans.${planKey}.quotas.${quotaKey} references an unknown operation measure`,
        );
      }
      quotas[quotaKey] = {
        operation,
        measure,
        limit: asDecimal(quota.limit),
        window: parseWindow(quota.window, `plans.${planKey}.quotas.${quotaKey}.window`),
        enforcement: enforcementSchema.parse(quota.enforcement),
        emitAtPercent,
      };
    }

    const creditPolicy = plan.credit_policy == null ? undefined : asString(plan.credit_policy);
    if (creditPolicy != null && !credits.policies[creditPolicy]) {
      semanticError(`plans.${planKey}.credit_policy references unknown policy`);
    }
    const admissionPolicy =
      plan.admission_policy == null ? undefined : asString(plan.admission_policy);
    if (admissionPolicy != null && !admission.policies[admissionPolicy]) {
      semanticError(`plans.${planKey}.admission_policy references unknown policy`);
    }
    const allowanceInput =
      plan.credit_allowance == null ? undefined : asObject(plan.credit_allowance);
    const creditAllowance =
      allowanceInput == null
        ? undefined
        : {
            amount: asDecimal(allowanceInput.amount),
            priority: asInteger(allowanceInput.priority),
            window: parseWindow(allowanceInput.window, `plans.${planKey}.credit_allowance.window`),
          };
    if (creditAllowance != null && creditAllowance.priority < 0) {
      semanticError(`plans.${planKey}.credit_allowance.priority must be non-negative`);
    }
    if (
      creditAllowance != null &&
      Object.values(credits.buckets).some((bucket) => bucket.priority === creditAllowance.priority)
    ) {
      semanticError(
        `plans.${planKey}.credit_allowance.priority conflicts with credit bucket priority ${creditAllowance.priority}`,
      );
    }

    const parsedPlan: PlanDefinition = {
      displayName: asString(plan.display_name),
      rank: asInteger(plan.rank ?? 0),
      allowedOperations,
      features,
      quotas,
      evolution: { defaultRollout },
    };
    if (plan.description != null) parsedPlan.description = asString(plan.description);
    if (rateCard != null) parsedPlan.rateCard = rateCard;
    if (creditAllowance != null) parsedPlan.creditAllowance = creditAllowance;
    if (creditPolicy != null) parsedPlan.creditPolicy = creditPolicy;
    if (admissionPolicy != null) parsedPlan.admissionPolicy = admissionPolicy;
    plans[planKey] = parsedPlan;
    if (plans[planKey].creditAllowance != null && credits.defaultBucket == null) {
      semanticError(`plans.${planKey}.credit_allowance requires credits.default_bucket`);
    }
  }
  return plans;
}
