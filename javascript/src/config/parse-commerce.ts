import {
  asDecimal,
  asInteger,
  asObject,
  asString,
  parseAvailability,
  parseBillingInterval,
  parseDuration,
  parseExpiry,
  parseWindow,
  semanticError,
  validateIdentifiers,
} from "./parse-utils.js";
import type {
  AutoRechargeGuardrails,
  CommerceConfig,
  CommerceOffer,
  CreditsConfig,
  OfferCommon,
  OfferPrice,
  ProviderDefinition,
  ProviderReference,
  SubscriptionChangeClassification,
  SubscriptionChangePolicy,
  TopupOffer,
  Window,
} from "./types.js";

function parseProviderReference(value: unknown): ProviderReference {
  const raw = asObject(value);
  if (raw.type === "stripe_price") {
    return { type: "stripe_price", priceId: asString(raw.price_id) };
  }
  if (raw.type === "dodo_product") {
    return { type: "dodo_product", productId: asString(raw.product_id) };
  }
  return {
    type: "custom_object",
    objectKind: raw.object_kind as "subscription" | "one_time",
    externalId: asString(raw.external_id),
  };
}

function providerExternalId(reference: ProviderReference): string {
  switch (reference.type) {
    case "stripe_price":
      return reference.priceId;
    case "dodo_product":
      return reference.productId;
    case "custom_object":
      return reference.externalId;
  }
}

function providerReferenceIsCompatible(
  provider: ProviderDefinition,
  reference: ProviderReference,
): boolean {
  return (
    (provider.type === "stripe" && reference.type === "stripe_price") ||
    (provider.type === "dodo" && reference.type === "dodo_product") ||
    (provider.type === "custom" && reference.type === "custom_object")
  );
}

function validateCurrency(value: string, path: string): string {
  if (!/^[A-Z]{3}$/.test(value)) {
    semanticError(`${path} must be an uppercase ISO-4217 currency code`);
  }
  return value;
}

function validateIntegerRange(
  value: { minimum: number; maximum: number; default: number },
  path: string,
): void {
  if (
    value.minimum > value.maximum ||
    value.default < value.minimum ||
    value.default > value.maximum
  ) {
    semanticError(`${path} requires minimum <= default <= maximum`);
  }
}

export function parseCommerce(value: unknown, credits: CreditsConfig): CommerceConfig {
  const raw = asObject(value ?? {});
  const providersRaw = asObject(raw.providers ?? {});
  const offersRaw = asObject(raw.offers ?? {});
  validateIdentifiers(providersRaw, "commerce.providers");
  validateIdentifiers(offersRaw, "commerce.offers");

  const providers = Object.fromEntries(
    Object.entries(providersRaw).map(([key, input]) => {
      const provider = asObject(input);
      return [
        key,
        provider.type === "custom"
          ? ({ type: "custom", adapter: asString(provider.adapter) } as const)
          : ({ type: provider.type } as ProviderDefinition),
      ];
    }),
  );

  const offers: Record<string, CommerceOffer> = {};
  const seenProviderObjects = new Set<string>();
  for (const [offerKey, input] of Object.entries(offersRaw)) {
    const offer = asObject(input);
    const references = Object.fromEntries(
      Object.entries(asObject(offer.providers)).map(([providerKey, inputReference]) => {
        const provider = providers[providerKey];
        if (!provider) {
          semanticError(`commerce.offers.${offerKey} references unknown provider`);
        }
        const reference = parseProviderReference(inputReference);
        if (!providerReferenceIsCompatible(provider, reference)) {
          semanticError(`commerce.offers.${offerKey} has incompatible provider reference`);
        }
        const uniqueKey = `${providerKey}/${providerExternalId(reference)}`;
        if (seenProviderObjects.has(uniqueKey)) {
          semanticError(`duplicate provider object reference ${uniqueKey}`);
        }
        seenProviderObjects.add(uniqueKey);
        return [providerKey, reference];
      }),
    );

    const common: OfferCommon = {
      displayName: asString(offer.display_name),
      ...(offer.description == null ? {} : { description: asString(offer.description) }),
      sortOrder: asInteger(offer.sort_order ?? 0),
      ...(offer.availability == null
        ? {}
        : { availability: parseAvailability(offer.availability) }),
      price: {
        amountMinor: asInteger(asObject(offer.price).amount_minor),
        currency: validateCurrency(
          asString(asObject(offer.price).currency),
          `commerce.offers.${offerKey}.price.currency`,
        ),
        taxBehavior: (asObject(offer.price).tax_behavior ??
          "unspecified") as OfferPrice["taxBehavior"],
      },
      providers: references,
    };

    if (offer.type === "subscription") {
      const cycleRaw = offer.cycle_grant == null ? undefined : asObject(offer.cycle_grant);
      const cycleGrant =
        cycleRaw == null
          ? undefined
          : {
              amount: asDecimal(cycleRaw.amount),
              bucket: asString(cycleRaw.bucket),
              renewal: cycleRaw.renewal as "replace_previous" | "accumulate",
              expiry: parseExpiry(
                cycleRaw.expiry ?? { type: "subscription_end" },
                `commerce.offers.${offerKey}.cycle_grant.expiry`,
              ),
            };
      if (cycleGrant != null && !credits.buckets[cycleGrant.bucket]) {
        semanticError(`commerce.offers.${offerKey}.cycle_grant references unknown bucket`);
      }
      offers[offerKey] = {
        ...common,
        type: "subscription",
        plan: asString(offer.plan),
        billingInterval: parseBillingInterval(offer.billing_interval),
        ...(offer.trial == null ? {} : { trial: parseBillingInterval(offer.trial) }),
        ...(cycleGrant == null ? {} : { cycleGrant }),
      };
      continue;
    }

    const bucket = asString(offer.bucket);
    if (!credits.buckets[bucket]) {
      semanticError(`commerce.offers.${offerKey}.bucket references unknown bucket`);
    }
    const quantityRaw = asObject(offer.quantity ?? {});
    const parsedQuantity = {
      minimum: asInteger(quantityRaw.minimum ?? 1),
      maximum: asInteger(quantityRaw.maximum ?? 1),
      default: asInteger(quantityRaw.default ?? 1),
    };
    validateIntegerRange(parsedQuantity, `commerce.offers.${offerKey}.quantity`);
    const parsedExpiry =
      offer.expiry == null
        ? undefined
        : parseExpiry(offer.expiry, `commerce.offers.${offerKey}.expiry`);
    if (parsedExpiry?.type === "subscription_end") {
      semanticError(`commerce.offers.${offerKey} top-up cannot use subscription_end expiry`);
    }
    offers[offerKey] = {
      ...common,
      type: "topup",
      creditsPerUnit: asDecimal(offer.credits_per_unit),
      quantity: parsedQuantity,
      bucket,
      ...(parsedExpiry == null ? {} : { expiry: parsedExpiry }),
      lotBehavior: (offer.lot_behavior ?? "separate_lots") as TopupOffer["lotBehavior"],
    };
  }

  const subscriptionChanges = parseSubscriptionChanges(raw.subscription_changes);
  const autoRecharge =
    raw.auto_recharge == null ? undefined : parseAutoRecharge(raw.auto_recharge, offers);
  return {
    providers,
    offers,
    ...(subscriptionChanges == null ? {} : { subscriptionChanges }),
    ...(autoRecharge == null ? {} : { autoRecharge }),
  };
}

const SUBSCRIPTION_CHANGE_CLASSIFICATIONS = [
  "upgrade",
  "downgrade",
  "lateral",
  "cadence_change",
] as const satisfies readonly SubscriptionChangeClassification[];

function parseSubscriptionChanges(
  value: unknown,
): CommerceConfig["subscriptionChanges"] | undefined {
  if (value == null) return undefined;
  const raw = asObject(value);
  const result: Partial<Record<SubscriptionChangeClassification, SubscriptionChangePolicy>> = {};
  for (const classification of SUBSCRIPTION_CHANGE_CLASSIFICATIONS) {
    if (raw[classification] == null) continue;
    const policy = asObject(raw[classification]);
    result[classification] = {
      effective: asString(policy.effective) as SubscriptionChangePolicy["effective"],
      proration: asString(policy.proration) as SubscriptionChangePolicy["proration"],
      paymentFailure: asString(
        policy.payment_failure ?? "prevent_change",
      ) as SubscriptionChangePolicy["paymentFailure"],
    };
  }
  return result;
}

function parseAutoRecharge(
  value: unknown,
  offers: Record<string, CommerceOffer>,
): AutoRechargeGuardrails {
  const auto = asObject(value);
  const threshold = asObject(auto.balance_below);
  const quantity = asObject(auto.quantity);
  const limits = asObject(auto.limits);
  const eligibleTopups = auto.eligible_topups as string[];
  const currencies = new Set(
    eligibleTopups.map((key) => {
      const offer = offers[key];
      if (offer?.type !== "topup") {
        semanticError(`commerce.auto_recharge references non-top-up offer '${key}'`);
      }
      return offer.price.currency;
    }),
  );
  if (currencies.size !== 1) {
    semanticError("commerce.auto_recharge eligible top-ups must use one currency");
  }
  const parsedQuantity = {
    minimum: asInteger(quantity.minimum),
    maximum: asInteger(quantity.maximum),
    default: asInteger(quantity.default),
  };
  validateIntegerRange(parsedQuantity, "commerce.auto_recharge.quantity");
  const parsedMinimum = asDecimal(threshold.minimum);
  const parsedMaximum = asDecimal(threshold.maximum);
  const parsedDefault = asDecimal(threshold.default);
  if (
    parsedMinimum.gt(parsedMaximum) ||
    parsedDefault.lt(parsedMinimum) ||
    parsedDefault.gt(parsedMaximum)
  ) {
    semanticError("commerce.auto_recharge.balance_below requires minimum <= default <= maximum");
  }
  for (const key of eligibleTopups) {
    const offer = offers[key] as TopupOffer;
    if (
      parsedQuantity.minimum < offer.quantity.minimum ||
      parsedQuantity.maximum > offer.quantity.maximum
    ) {
      semanticError(`commerce.auto_recharge.quantity must fit commerce.offers.${key}.quantity`);
    }
  }
  const parsedRearmAbove = asDecimal(auto.rearm_above);
  if (parsedRearmAbove.lte(parsedMaximum)) {
    semanticError("commerce.auto_recharge.rearm_above must exceed balance_below.maximum");
  }
  return {
    eligibleTopups,
    balanceBelow: {
      minimum: parsedMinimum,
      maximum: parsedMaximum,
      default: parsedDefault,
    },
    rearmAbove: parsedRearmAbove,
    quantity: parsedQuantity,
    limits: {
      maxPurchases: asInteger(limits.max_purchases),
      window: parseWindow(limits.window, "commerce.auto_recharge.limits.window") as Extract<
        Window,
        { type: "calendar" | "rolling" }
      >,
      maxChargeMinor: asInteger(limits.max_charge_minor),
      cooldown: parseDuration(limits.cooldown),
      maxConsecutiveFailures: asInteger(limits.max_consecutive_failures ?? 3),
      failureAction: "pause",
    },
  };
}
