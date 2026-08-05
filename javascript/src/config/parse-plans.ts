import { resolvesOperation } from "./parse-pricing.js";
import {
  asDecimal,
  asInteger,
  asObject,
  asString,
  identifier,
  parseWindow,
  semanticError,
  validateIdentifiers,
} from "./parse-utils.js";
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
  if (definition.type === "boolean" && typeof featureValue !== "boolean") {
    semanticError(`plans.${planKey}.features.${featureKey} must be boolean`);
  }
  if (definition.type === "integer" && !Number.isInteger(featureValue)) {
    semanticError(`plans.${planKey}.features.${featureKey} must be integer`);
  }
  if (
    definition.type === "integer" &&
    definition.minimum != null &&
    Number(featureValue) < definition.minimum
  ) {
    semanticError(`plans.${planKey}.features.${featureKey} is below the feature minimum`);
  }
  if (
    definition.type === "integer" &&
    definition.maximum != null &&
    Number(featureValue) > definition.maximum
  ) {
    semanticError(`plans.${planKey}.features.${featureKey} exceeds the feature maximum`);
  }
  if (
    definition.type === "enum" &&
    (typeof featureValue !== "string" || !definition.values.includes(featureValue))
  ) {
    semanticError(`plans.${planKey}.features.${featureKey} has an invalid enum value`);
  }
  if (definition.type === "string" && typeof featureValue !== "string") {
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
  value: unknown,
  pricing: PricingConfig | undefined,
  credits: CreditsConfig,
  entitlements: EntitlementsConfig,
  admission: { policies: Record<string, AdmissionPolicy> },
  subscriptionPlans: Set<string>,
): Record<string, PlanDefinition> {
  const raw = asObject(value ?? {});
  validateIdentifiers(raw, "plans");
  const plans: Record<string, PlanDefinition> = {};

  for (const [planKey, input] of Object.entries(raw)) {
    const plan = asObject(input);
    const evolution = plan.evolution == null ? undefined : asObject(plan.evolution);
    const defaultRollout = (evolution?.default_rollout ??
      (subscriptionPlans.has(planKey) ? "next_renewal" : "immediate")) as PlanRolloutStrategy;
    if (
      !(["immediate", "next_renewal", "new_assignments_only"] as const).includes(defaultRollout)
    ) {
      semanticError(`plans.${planKey}.evolution.default_rollout is invalid`);
    }
    if (defaultRollout === "next_renewal" && !subscriptionPlans.has(planKey)) {
      semanticError(
        `plans.${planKey}.evolution.default_rollout=next_renewal requires a subscription offer`,
      );
    }
    const rateCard = plan.rate_card == null ? undefined : asString(plan.rate_card);
    const allowedOperations = (plan.allowed_operations ?? []) as string[];
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

    const features = asObject(plan.features ?? {}) as Record<string, FeatureValue>;
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
      const emitAtPercent = (quota.emit_at_percent ?? [100]) as number[];
      if (
        emitAtPercent.some((threshold) => threshold < 1 || threshold > 100) ||
        emitAtPercent.some((threshold, index) => index > 0 && threshold <= emitAtPercent[index - 1])
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
        enforcement: quota.enforcement as "block" | "allow",
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
    const allowancePriority =
      allowanceInput?.priority == null ? undefined : asInteger(allowanceInput.priority);
    if (allowancePriority != null && allowancePriority < 0) {
      semanticError(`plans.${planKey}.credit_allowance.priority must be non-negative`);
    }
    if (
      allowancePriority != null &&
      Object.values(credits.buckets).some((bucket) => bucket.priority === allowancePriority)
    ) {
      semanticError(
        `plans.${planKey}.credit_allowance.priority conflicts with credit bucket priority ${allowancePriority}`,
      );
    }

    plans[planKey] = {
      displayName: asString(plan.display_name),
      rank: asInteger(plan.rank ?? 0),
      ...(plan.description == null ? {} : { description: asString(plan.description) }),
      ...(rateCard == null ? {} : { rateCard }),
      allowedOperations,
      features,
      ...(allowanceInput == null
        ? {}
        : {
            creditAllowance: {
              amount: asDecimal(allowanceInput.amount),
              ...(allowancePriority == null ? {} : { priority: allowancePriority }),
              window: parseWindow(
                allowanceInput.window,
                `plans.${planKey}.credit_allowance.window`,
              ),
            },
          }),
      quotas,
      ...(creditPolicy == null ? {} : { creditPolicy }),
      ...(admissionPolicy == null ? {} : { admissionPolicy }),
      evolution: { defaultRollout },
    };
    if (plans[planKey].creditAllowance != null && credits.defaultBucket == null) {
      semanticError(`plans.${planKey}.credit_allowance requires credits.default_bucket`);
    }
  }
  return plans;
}
