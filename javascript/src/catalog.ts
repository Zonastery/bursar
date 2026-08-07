import type { CommerceOffer, FeatureValue, ParsedBursarConfig, Window } from "./config/types.js";

export interface PublicCatalogWindow {
  type: Window["type"];
  unit: string;
  count: number;
  timezone?: string;
}

export interface PublicCatalogOffer {
  key: string;
  type: CommerceOffer["type"];
  displayName: string;
  description?: string;
  sortOrder: number;
  price: {
    amountMinor: number;
    currency: string;
    taxBehavior: "inclusive" | "exclusive" | "unspecified";
  };
  billingInterval?: { unit: string; count: number };
  creditsPerUnit?: string;
  quantity?: { minimum: number; maximum: number; default: number };
}

export interface PublicCatalogPlan {
  key: string;
  displayName: string;
  description?: string;
  rank: number;
  features: Record<string, FeatureValue>;
  allowance?: {
    amount: string;
    priority: number;
    window: PublicCatalogWindow;
  };
  quotas: Record<
    string,
    {
      operation: string;
      measure: string;
      limit: string;
      window: PublicCatalogWindow;
      enforcement: "block" | "allow";
    }
  >;
  offers: PublicCatalogOffer[];
}

export interface PublicCatalog {
  version: 1;
  defaultPlan: string | null;
  creditDisplay: {
    currency: string;
    unitsPerMajor: string;
  } | null;
  plans: PublicCatalogPlan[];
  topups: PublicCatalogOffer[];
}

function publicWindow(window: Window): PublicCatalogWindow {
  if (window.type === "calendar") {
    return {
      type: window.type,
      unit: window.unit,
      count: window.count,
      timezone: window.timezone,
    };
  }
  if (window.type === "plan_assignment") {
    return {
      type: window.type,
      unit: window.interval.unit,
      count: window.interval.count,
      timezone: window.timezone,
    };
  }
  return {
    type: window.type,
    unit: window.duration.unit,
    count: window.duration.count,
  };
}

function publicOffer(key: string, offer: CommerceOffer): PublicCatalogOffer {
  return {
    key,
    type: offer.type,
    displayName: offer.displayName,
    ...(offer.description ? { description: offer.description } : {}),
    sortOrder: offer.sortOrder,
    price: { ...offer.price },
    ...(offer.type === "subscription"
      ? {
          billingInterval: {
            unit: offer.billingInterval.unit,
            count: offer.billingInterval.count,
          },
        }
      : {
          creditsPerUnit: offer.creditsPerUnit.toString(),
          quantity: { ...offer.quantity },
        }),
  };
}

/** Produce a provider-secret-free, JSON-safe catalog for product surfaces. */
export function projectPublicCatalog(config: ParsedBursarConfig): PublicCatalog {
  const offers = Object.entries(config.commerce.offers);
  const plans = Object.entries(config.plans)
    .map(
      ([key, plan]): PublicCatalogPlan => ({
        key,
        displayName: plan.displayName,
        ...(plan.description ? { description: plan.description } : {}),
        rank: plan.rank,
        features: { ...plan.features },
        ...(plan.creditAllowance
          ? {
              allowance: {
                amount: plan.creditAllowance.amount.toString(),
                priority: plan.creditAllowance.priority,
                window: publicWindow(plan.creditAllowance.window),
              },
            }
          : {}),
        quotas: Object.fromEntries(
          Object.entries(plan.quotas).map(([quotaKey, quota]) => [
            quotaKey,
            {
              operation: quota.operation,
              measure: quota.measure,
              limit: quota.limit.toString(),
              window: publicWindow(quota.window),
              enforcement: quota.enforcement,
            },
          ]),
        ),
        offers: offers
          .filter(
            (entry): entry is [string, Extract<CommerceOffer, { type: "subscription" }>] =>
              entry[1].type === "subscription" && entry[1].plan === key,
          )
          .map(([offerKey, offer]) => publicOffer(offerKey, offer))
          .sort((a, b) => a.sortOrder - b.sortOrder || a.key.localeCompare(b.key)),
      }),
    )
    .sort((a, b) => a.rank - b.rank || a.key.localeCompare(b.key));

  return {
    version: 1,
    defaultPlan: config.catalog.defaultPlan ?? plans[0]?.key ?? null,
    creditDisplay: config.credits.display
      ? {
          currency: config.credits.display.currency,
          unitsPerMajor: config.credits.display.unitsPerMajor.toString(),
        }
      : null,
    plans,
    topups: offers
      .filter(
        (entry): entry is [string, Extract<CommerceOffer, { type: "topup" }>] =>
          entry[1].type === "topup",
      )
      .map(([key, offer]) => publicOffer(key, offer))
      .sort((a, b) => a.sortOrder - b.sortOrder || a.key.localeCompare(b.key)),
  };
}
