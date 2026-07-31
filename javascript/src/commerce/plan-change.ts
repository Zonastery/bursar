import type {
  ParsedBursarConfig,
  SubscriptionChangePolicy,
  SubscriptionOffer,
} from "../config/types.js";
import { CommerceResourceNotFoundError, MissingPlanChangePolicyError } from "./errors.js";
import type { PlanChangeClassification } from "./types.js";

function normalizeInterval(value: string | null | undefined): "month" | "year" | null {
  const normalized = value?.toLowerCase();
  return normalized === "month" || normalized === "year" ? normalized : null;
}

export interface ClassifiedSubscriptionChange {
  classification: PlanChangeClassification;
  targetInterval: "month" | "year";
  policy?: SubscriptionChangePolicy;
}

/** Classify a subscription transition from explicit rank and cadence. */
export function classifySubscriptionChange(
  config: ParsedBursarConfig,
  currentPlan: string,
  currentIntervalValue: string | null | undefined,
  targetOffer: SubscriptionOffer,
): ClassifiedSubscriptionChange {
  const currentDefinition = config.plans[currentPlan];
  const targetDefinition = config.plans[targetOffer.plan];
  if (!currentDefinition || !targetDefinition) {
    throw new CommerceResourceNotFoundError("Subscription plan is absent from the catalog");
  }
  const currentInterval = normalizeInterval(currentIntervalValue);
  const targetInterval = normalizeInterval(targetOffer.billingInterval.unit);
  if (!currentInterval || !targetInterval) {
    throw new CommerceResourceNotFoundError("Subscription cadence is unknown");
  }

  let classification: PlanChangeClassification;
  if (targetOffer.plan === currentPlan && targetInterval === currentInterval) {
    classification = "unchanged";
  } else if (targetDefinition.rank > currentDefinition.rank) {
    classification = "upgrade";
  } else if (targetDefinition.rank < currentDefinition.rank) {
    classification = "downgrade";
  } else if (targetOffer.plan !== currentPlan) {
    classification = "lateral";
  } else {
    classification = "cadence_change";
  }
  const policy =
    classification === "unchanged"
      ? undefined
      : config.commerce.subscriptionChanges?.[classification];
  if (classification !== "unchanged" && !policy) {
    throw new MissingPlanChangePolicyError(classification);
  }
  return { classification, targetInterval, policy };
}
