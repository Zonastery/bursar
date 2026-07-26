import Decimal from "decimal.js";

import { ConfigError } from "./errors.js";
import { validateExpression } from "./expr.js";

export type BursarConfigData = Record<string, unknown>;
export type PeriodUnit = "day" | "week" | "month" | "year";
export type PeriodAnchor = "calendar" | "plan_assignment" | "rolling";
export type LimitAction = "deny" | "warn" | "notify";
export type BillingMode = "strict" | "overdraft";

export interface Period {
  unit: PeriodUnit;
  count: number;
  anchor: PeriodAnchor;
  timezone: string;
}
export interface OperationDefinition {
  measures: string[];
  dimensions: string[];
}
export interface DimensionMatcher {
  exact?: string;
  prefix?: string;
}
export interface PriceRule {
  match?: Record<string, DimensionMatcher>;
  default: boolean;
  formula: string;
}
export interface RateCard {
  extends?: string;
  prices: Record<string, PriceRule[]>;
}
export interface UsageConfig {
  operations: Record<string, OperationDefinition>;
  rateCards: Record<string, RateCard>;
}
export interface BucketDefinition {
  expiresAfter?: Period;
}
export interface SignupGrant {
  amount: Decimal;
  bucket: string;
}
export interface CreditsConfig {
  buckets: Record<string, BucketDefinition>;
  spendOrder: string[];
  defaultBucket?: string;
  overdraftBucket?: string;
  signupGrant?: SignupGrant;
}
export interface IncludedCredits {
  amount: Decimal;
  reset: Period;
}
export interface FeatureLimit {
  maxCalls: number;
  period: Period;
  action: LimitAction;
}
export interface OperationSpendingPolicy {
  maxConcurrent?: number;
  mode?: BillingMode;
  overdraftLimit?: Decimal;
}
export interface SpendingPolicy {
  mode: BillingMode;
  overdraftLimit?: Decimal;
  maxConcurrent?: number;
  operations: Record<string, OperationSpendingPolicy>;
}
export interface PlanDefinition {
  displayName: string;
  rateCard?: string;
  includedCredits?: IncludedCredits;
  features: Record<string, unknown>;
  limits: Record<string, FeatureLimit>;
  spending: SpendingPolicy;
}
export interface ProviderReference {
  lookup: { type: string; value: string };
}
export interface RenewalCredits {
  amount: Decimal;
  bucket: string;
  behavior: "replace" | "accumulate";
  onSubscriptionEnd: "expire" | "keep";
}
export interface SubscriptionOffer {
  plan: string;
  billingPeriod: Period;
  providers: Record<string, ProviderReference>;
  renewalCredits?: RenewalCredits;
  stackCredits: boolean;
}
export interface TopupOffer {
  credits: Decimal;
  bucket: string;
  providers: Record<string, ProviderReference>;
}
export interface AutoRechargePolicy {
  trigger: { balanceBelow: Decimal };
  purchase: { topup: string; quantity: number };
  limit: { maxPurchases: number; period: Period };
}
export interface PaymentsConfig {
  subscriptions: Record<string, SubscriptionOffer>;
  topups: Record<string, TopupOffer>;
  autoRecharge?: AutoRechargePolicy;
}
export interface ParsedBursarConfig {
  version: 1;
  usage?: UsageConfig;
  credits: CreditsConfig;
  plans: Record<string, PlanDefinition>;
  payments?: PaymentsConfig;
}

const ID = /^[a-z][a-z0-9_]*$/;
const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const assertRecord = (value: unknown, path: string): Record<string, unknown> => {
  if (!isRecord(value)) throw new ConfigError(`${path} must be an object`);
  return value;
};
const assertKnown = (value: Record<string, unknown>, keys: string[], path: string): void => {
  for (const key of Object.keys(value))
    if (!keys.includes(key)) throw new ConfigError(`unknown config key in ${path}: ${key}`);
};
const id = (value: string, path: string): string => {
  if (!ID.test(value)) throw new ConfigError(`${path} must be a non-empty snake_case identifier`);
  return value;
};
const string = (value: unknown, path: string): string => {
  if (typeof value !== "string" || !value)
    throw new ConfigError(`${path} must be a non-empty string`);
  return value;
};
const decimal = (value: unknown, path: string, allowZero = true): Decimal => {
  try {
    const result = new Decimal(value as Decimal.Value);
    if (!result.isFinite() || result.isNegative() || (!allowZero && result.isZero()))
      throw new Error();
    return result;
  } catch {
    throw new ConfigError(
      `${path} must be a finite ${allowZero ? "non-negative" : "positive"} decimal`,
    );
  }
};
const positiveInt = (value: unknown, path: string, allowZero = false): number => {
  if (!Number.isInteger(value) || (value as number) < (allowZero ? 0 : 1))
    throw new ConfigError(`${path} must be an integer ${allowZero ? ">= 0" : ">= 1"}`);
  return value as number;
};

function parsePeriod(value: unknown, path: string): Period {
  const raw = assertRecord(value, path);
  assertKnown(raw, ["unit", "count", "anchor", "timezone"], path);
  const unit = string(raw.unit, `${path}.unit`) as PeriodUnit;
  if (!["day", "week", "month", "year"].includes(unit))
    throw new ConfigError(`${path}.unit is invalid`);
  const anchor = (raw.anchor ?? "calendar") as PeriodAnchor;
  if (!["calendar", "plan_assignment", "rolling"].includes(anchor))
    throw new ConfigError(`${path}.anchor is invalid`);
  return {
    unit,
    count: raw.count == null ? 1 : positiveInt(raw.count, `${path}.count`),
    anchor,
    timezone: raw.timezone == null ? "UTC" : string(raw.timezone, `${path}.timezone`),
  };
}

function parseOperations(value: unknown): Record<string, OperationDefinition> {
  const raw = assertRecord(value, "usage.operations");
  const out: Record<string, OperationDefinition> = {};
  if (Object.keys(raw).length === 0) throw new ConfigError("usage.operations must not be empty");
  for (const [key, input] of Object.entries(raw)) {
    id(key, "usage.operations key");
    const obj = assertRecord(input, `usage.operations.${key}`);
    assertKnown(obj, ["measures", "dimensions"], `usage.operations.${key}`);
    const parseNames = (name: "measures" | "dimensions") => {
      const values = obj[name] ?? [];
      if (!Array.isArray(values) || values.some((v) => typeof v !== "string"))
        throw new ConfigError(`usage.operations.${key}.${name} must be a string array`);
      values.forEach((v) => id(v, `usage.operations.${key}.${name}`));
      if (new Set(values).size !== values.length)
        throw new ConfigError(`usage.operations.${key}.${name} contains duplicates`);
      return values as string[];
    };
    const measures = parseNames("measures"),
      dimensions = parseNames("dimensions");
    if (measures.some((x) => dimensions.includes(x)))
      throw new ConfigError(`usage.operations.${key} reuses a measure as a dimension`);
    out[key] = { measures, dimensions };
  }
  return out;
}

function parseRule(value: unknown, path: string, operation: OperationDefinition): PriceRule {
  const raw = assertRecord(value, path);
  assertKnown(raw, ["match", "default", "formula"], path);
  const isDefault = raw.default === true;
  const hasMatch = raw.match != null;
  if (isDefault === hasMatch)
    throw new ConfigError(`${path} must define exactly one of default or match`);
  const formula = string(raw.formula, `${path}.formula`);
  try {
    validateExpression(formula, new Set(operation.measures));
  } catch (error) {
    throw new ConfigError(`invalid formula in ${path}: ${(error as Error).message}`);
  }
  if (!hasMatch) return { default: true, formula };
  const matchRaw = assertRecord(raw.match, `${path}.match`);
  const match: Record<string, DimensionMatcher> = {};
  for (const [key, input] of Object.entries(matchRaw)) {
    if (!operation.dimensions.includes(key))
      throw new ConfigError(`${path}.match references undeclared dimension '${key}'`);
    const matcher = assertRecord(input, `${path}.match.${key}`);
    assertKnown(matcher, ["exact", "prefix"], `${path}.match.${key}`);
    const exact =
      matcher.exact == null ? undefined : string(matcher.exact, `${path}.match.${key}.exact`);
    const prefix =
      matcher.prefix == null ? undefined : string(matcher.prefix, `${path}.match.${key}.prefix`);
    if ((exact == null) === (prefix == null))
      throw new ConfigError(`${path}.match.${key} requires exactly one of exact or prefix`);
    match[key] = { ...(exact != null ? { exact } : {}), ...(prefix != null ? { prefix } : {}) };
  }
  if (Object.keys(match).length === 0) throw new ConfigError(`${path}.match must not be empty`);
  return { match, default: false, formula };
}

function parseUsage(value: unknown): UsageConfig {
  const raw = assertRecord(value, "usage");
  assertKnown(raw, ["operations", "rate_cards"], "usage");
  const operations = parseOperations(raw.operations);
  const cardsRaw = assertRecord(raw.rate_cards, "usage.rate_cards");
  if (Object.keys(cardsRaw).length === 0)
    throw new ConfigError("usage.rate_cards must not be empty");
  const rateCards: Record<string, RateCard> = {};
  for (const [key, input] of Object.entries(cardsRaw)) {
    id(key, "usage.rate_cards key");
    const card = assertRecord(input, `usage.rate_cards.${key}`);
    assertKnown(card, ["extends", "prices"], `usage.rate_cards.${key}`);
    const pricesRaw = assertRecord(card.prices ?? {}, `usage.rate_cards.${key}.prices`);
    const prices: Record<string, PriceRule[]> = {};
    for (const [operation, rulesInput] of Object.entries(pricesRaw)) {
      if (!operations[operation])
        throw new ConfigError(
          `usage.rate_cards.${key}.prices references unknown operation '${operation}'`,
        );
      if (!Array.isArray(rulesInput) || rulesInput.length === 0)
        throw new ConfigError(
          `usage.rate_cards.${key}.prices.${operation} must be a non-empty array`,
        );
      const rules = rulesInput.map((rule, index) =>
        parseRule(
          rule,
          `usage.rate_cards.${key}.prices.${operation}[${index}]`,
          operations[operation],
        ),
      );
      if (!rules.at(-1)?.default || rules.filter((rule) => rule.default).length !== 1)
        throw new ConfigError(
          `usage.rate_cards.${key}.prices.${operation} must end with exactly one default rule`,
        );
      prices[operation] = rules;
    }
    rateCards[key] = {
      ...(card.extends == null
        ? {}
        : { extends: string(card.extends, `usage.rate_cards.${key}.extends`) }),
      prices,
    };
  }
  for (const [key, card] of Object.entries(rateCards))
    if (card.extends && !rateCards[card.extends])
      throw new ConfigError(
        `usage.rate_cards.${key}.extends references unknown rate card '${card.extends}'`,
      );
  const visiting = new Set<string>(),
    visited = new Set<string>();
  const visit = (key: string): void => {
    if (visiting.has(key))
      throw new ConfigError(`usage.rate_cards inheritance cycle includes '${key}'`);
    if (visited.has(key)) return;
    visiting.add(key);
    const parent = rateCards[key].extends;
    if (parent) visit(parent);
    visiting.delete(key);
    visited.add(key);
  };
  Object.keys(rateCards).forEach(visit);
  const pricesOperation = (card: string, operation: string): boolean =>
    Object.prototype.hasOwnProperty.call(rateCards[card].prices, operation) ||
    (rateCards[card].extends != null && pricesOperation(rateCards[card].extends!, operation));
  for (const card of Object.keys(rateCards))
    for (const operation of Object.keys(operations))
      if (!pricesOperation(card, operation))
        throw new ConfigError(`usage.rate_cards.${card} has no price for operation '${operation}'`);
  return { operations, rateCards };
}

function parseCredits(value: unknown): CreditsConfig {
  if (value == null) return { buckets: {}, spendOrder: [] };
  const raw = assertRecord(value, "credits");
  assertKnown(
    raw,
    ["buckets", "spend_order", "default_bucket", "overdraft_bucket", "signup_grant"],
    "credits",
  );
  const bucketsRaw = assertRecord(raw.buckets ?? {}, "credits.buckets");
  const buckets: Record<string, BucketDefinition> = {};
  for (const [key, input] of Object.entries(bucketsRaw)) {
    id(key, "credits.buckets key");
    const bucket = assertRecord(input, `credits.buckets.${key}`);
    assertKnown(bucket, ["expires_after"], `credits.buckets.${key}`);
    buckets[key] =
      bucket.expires_after == null
        ? {}
        : {
            expiresAfter: parsePeriod(bucket.expires_after, `credits.buckets.${key}.expires_after`),
          };
  }
  const order = raw.spend_order ?? [];
  if (!Array.isArray(order) || order.some((v) => typeof v !== "string"))
    throw new ConfigError("credits.spend_order must be a string array");
  if (Object.keys(buckets).length === 0) {
    if (
      order.length ||
      raw.default_bucket != null ||
      raw.overdraft_bucket != null ||
      raw.signup_grant != null
    )
      throw new ConfigError("credits bucket settings require credits.buckets");
    return { buckets, spendOrder: [] };
  }
  if (
    new Set(order).size !== order.length ||
    order.length !== Object.keys(buckets).length ||
    order.some((key) => !buckets[key])
  )
    throw new ConfigError("credits.spend_order must list every bucket exactly once");
  const defaultBucket = string(raw.default_bucket, "credits.default_bucket");
  if (!buckets[defaultBucket])
    throw new ConfigError("credits.default_bucket references an unknown bucket");
  const overdraftBucket =
    raw.overdraft_bucket == null
      ? undefined
      : string(raw.overdraft_bucket, "credits.overdraft_bucket");
  if (overdraftBucket && !buckets[overdraftBucket])
    throw new ConfigError("credits.overdraft_bucket references an unknown bucket");
  let signupGrant: SignupGrant | undefined;
  if (raw.signup_grant != null) {
    const grant = assertRecord(raw.signup_grant, "credits.signup_grant");
    assertKnown(grant, ["amount", "bucket"], "credits.signup_grant");
    const bucket = string(grant.bucket, "credits.signup_grant.bucket");
    if (!buckets[bucket])
      throw new ConfigError("credits.signup_grant.bucket references an unknown bucket");
    signupGrant = { amount: decimal(grant.amount, "credits.signup_grant.amount", false), bucket };
  }
  return {
    buckets,
    spendOrder: order as string[],
    defaultBucket,
    ...(overdraftBucket ? { overdraftBucket } : {}),
    ...(signupGrant ? { signupGrant } : {}),
  };
}

function parsePlans(
  value: unknown,
  usage?: UsageConfig,
  credits?: CreditsConfig,
): Record<string, PlanDefinition> {
  if (value == null) return {};
  const raw = assertRecord(value, "plans");
  const out: Record<string, PlanDefinition> = {};
  for (const [key, input] of Object.entries(raw)) {
    id(key, "plans key");
    const plan = assertRecord(input, `plans.${key}`);
    assertKnown(
      plan,
      ["display_name", "rate_card", "included_credits", "features", "limits", "spending"],
      `plans.${key}`,
    );
    const displayName = string(plan.display_name, `plans.${key}.display_name`);
    const rateCard =
      plan.rate_card == null ? undefined : string(plan.rate_card, `plans.${key}.rate_card`);
    if (rateCard && !usage?.rateCards[rateCard])
      throw new ConfigError(`plans.${key}.rate_card references an unknown rate card`);
    let includedCredits: IncludedCredits | undefined;
    if (plan.included_credits != null) {
      const included = assertRecord(plan.included_credits, `plans.${key}.included_credits`);
      assertKnown(included, ["amount", "reset"], `plans.${key}.included_credits`);
      includedCredits = {
        amount: decimal(included.amount, `plans.${key}.included_credits.amount`),
        reset: parsePeriod(included.reset, `plans.${key}.included_credits.reset`),
      };
    }
    const features =
      plan.features == null ? {} : assertRecord(plan.features, `plans.${key}.features`);
    Object.keys(features).forEach((feature) => id(feature, `plans.${key}.features key`));
    const limitsRaw = plan.limits == null ? {} : assertRecord(plan.limits, `plans.${key}.limits`);
    const limits: Record<string, FeatureLimit> = {};
    for (const [feature, rawLimit] of Object.entries(limitsRaw)) {
      id(feature, `plans.${key}.limits key`);
      const limit = assertRecord(rawLimit, `plans.${key}.limits.${feature}`);
      assertKnown(limit, ["max_calls", "period", "action"], `plans.${key}.limits.${feature}`);
      const action = (limit.action ?? "deny") as LimitAction;
      if (!["deny", "warn", "notify"].includes(action))
        throw new ConfigError(`plans.${key}.limits.${feature}.action is invalid`);
      limits[feature] = {
        maxCalls: positiveInt(limit.max_calls, `plans.${key}.limits.${feature}.max_calls`, true),
        period: parsePeriod(limit.period, `plans.${key}.limits.${feature}.period`),
        action,
      };
    }
    const spendingRaw =
      plan.spending == null ? {} : assertRecord(plan.spending, `plans.${key}.spending`);
    assertKnown(
      spendingRaw,
      ["mode", "overdraft_limit", "max_concurrent", "operations"],
      `plans.${key}.spending`,
    );
    const mode = (spendingRaw.mode ?? "strict") as BillingMode;
    if (!["strict", "overdraft"].includes(mode))
      throw new ConfigError(`plans.${key}.spending.mode is invalid`);
    const overdraftLimit =
      spendingRaw.overdraft_limit == null
        ? undefined
        : decimal(spendingRaw.overdraft_limit, `plans.${key}.spending.overdraft_limit`);
    if (overdraftLimit && mode !== "overdraft")
      throw new ConfigError(`plans.${key}.spending.overdraft_limit requires overdraft mode`);
    if (mode === "overdraft" && !credits?.overdraftBucket)
      throw new ConfigError(
        `plans.${key}.spending overdraft mode requires credits.overdraft_bucket`,
      );
    const operationsRaw =
      spendingRaw.operations == null
        ? {}
        : assertRecord(spendingRaw.operations, `plans.${key}.spending.operations`);
    const operations: Record<string, OperationSpendingPolicy> = {};
    for (const [operation, rawPolicy] of Object.entries(operationsRaw)) {
      id(operation, `plans.${key}.spending.operations key`);
      const policy = assertRecord(rawPolicy, `plans.${key}.spending.operations.${operation}`);
      assertKnown(
        policy,
        ["max_concurrent", "mode", "overdraft_limit"],
        `plans.${key}.spending.operations.${operation}`,
      );
      const policyMode = policy.mode == null ? undefined : (policy.mode as BillingMode);
      if (policyMode && !["strict", "overdraft"].includes(policyMode))
        throw new ConfigError(`plans.${key}.spending.operations.${operation}.mode is invalid`);
      const policyLimit =
        policy.overdraft_limit == null
          ? undefined
          : decimal(
              policy.overdraft_limit,
              `plans.${key}.spending.operations.${operation}.overdraft_limit`,
            );
      if (policyLimit && policyMode !== "overdraft")
        throw new ConfigError(
          `plans.${key}.spending.operations.${operation}.overdraft_limit requires overdraft mode`,
        );
      operations[operation] = {
        ...(policy.max_concurrent == null
          ? {}
          : {
              maxConcurrent: positiveInt(
                policy.max_concurrent,
                `plans.${key}.spending.operations.${operation}.max_concurrent`,
              ),
            }),
        ...(policyMode ? { mode: policyMode } : {}),
        ...(policyLimit ? { overdraftLimit: policyLimit } : {}),
      };
    }
    out[key] = {
      displayName,
      ...(rateCard ? { rateCard } : {}),
      ...(includedCredits ? { includedCredits } : {}),
      features,
      limits,
      spending: {
        mode,
        ...(overdraftLimit ? { overdraftLimit } : {}),
        ...(spendingRaw.max_concurrent == null
          ? {}
          : {
              maxConcurrent: positiveInt(
                spendingRaw.max_concurrent,
                `plans.${key}.spending.max_concurrent`,
              ),
            }),
        operations,
      },
    };
  }
  return out;
}

function parseProviders(value: unknown, path: string): Record<string, ProviderReference> {
  const raw = assertRecord(value, path);
  if (Object.keys(raw).length === 0) throw new ConfigError(`${path} must not be empty`);
  const out: Record<string, ProviderReference> = {};
  for (const [provider, input] of Object.entries(raw)) {
    id(provider, `${path} key`);
    const ref = assertRecord(input, `${path}.${provider}`);
    assertKnown(ref, ["lookup"], `${path}.${provider}`);
    const lookup = assertRecord(ref.lookup, `${path}.${provider}.lookup`);
    assertKnown(lookup, ["type", "value"], `${path}.${provider}.lookup`);
    out[provider] = {
      lookup: {
        type: string(lookup.type, `${path}.${provider}.lookup.type`),
        value: string(lookup.value, `${path}.${provider}.lookup.value`),
      },
    };
  }
  return out;
}

function parsePayments(
  value: unknown,
  plans: Record<string, PlanDefinition>,
  credits: CreditsConfig,
): PaymentsConfig | undefined {
  if (value == null) return undefined;
  const raw = assertRecord(value, "payments");
  assertKnown(raw, ["subscriptions", "topups", "auto_recharge"], "payments");
  const subscriptions: Record<string, SubscriptionOffer> = {},
    topups: Record<string, TopupOffer> = {};
  const seen = new Set<string>();
  const checkRefs = (refs: Record<string, ProviderReference>) =>
    Object.entries(refs).forEach(([provider, ref]) => {
      const key = `${provider}/${ref.lookup.type}/${ref.lookup.value}`;
      if (seen.has(key)) throw new ConfigError(`duplicate provider lookup ${key}`);
      seen.add(key);
    });
  const subscriptionsRaw = assertRecord(raw.subscriptions ?? {}, "payments.subscriptions");
  for (const [key, input] of Object.entries(subscriptionsRaw)) {
    id(key, "payments.subscriptions key");
    const offer = assertRecord(input, `payments.subscriptions.${key}`);
    assertKnown(
      offer,
      ["plan", "billing_period", "providers", "renewal_credits", "stack_credits"],
      `payments.subscriptions.${key}`,
    );
    const plan = string(offer.plan, `payments.subscriptions.${key}.plan`);
    if (!plans[plan])
      throw new ConfigError(`payments.subscriptions.${key}.plan references an unknown plan`);
    const providers = parseProviders(offer.providers, `payments.subscriptions.${key}.providers`);
    checkRefs(providers);
    let renewalCredits: RenewalCredits | undefined;
    if (offer.renewal_credits != null) {
      const grant = assertRecord(
        offer.renewal_credits,
        `payments.subscriptions.${key}.renewal_credits`,
      );
      assertKnown(
        grant,
        ["amount", "bucket", "behavior", "on_subscription_end"],
        `payments.subscriptions.${key}.renewal_credits`,
      );
      const bucket = string(grant.bucket, `payments.subscriptions.${key}.renewal_credits.bucket`);
      if (!credits.buckets[bucket])
        throw new ConfigError(
          `payments.subscriptions.${key}.renewal_credits.bucket references an unknown bucket`,
        );
      const behavior = string(
        grant.behavior,
        `payments.subscriptions.${key}.renewal_credits.behavior`,
      ) as RenewalCredits["behavior"];
      const onSubscriptionEnd = string(
        grant.on_subscription_end,
        `payments.subscriptions.${key}.renewal_credits.on_subscription_end`,
      ) as RenewalCredits["onSubscriptionEnd"];
      if (
        !["replace", "accumulate"].includes(behavior) ||
        !["expire", "keep"].includes(onSubscriptionEnd)
      )
        throw new ConfigError(
          `payments.subscriptions.${key}.renewal_credits has an invalid policy`,
        );
      renewalCredits = {
        amount: decimal(
          grant.amount,
          `payments.subscriptions.${key}.renewal_credits.amount`,
          false,
        ),
        bucket,
        behavior,
        onSubscriptionEnd,
      };
    }
    const stackCredits = offer.stack_credits === true;
    if (renewalCredits && plans[plan].includedCredits && !stackCredits)
      throw new ConfigError(
        `payments.subscriptions.${key} combines included_credits and renewal_credits; set stack_credits: true`,
      );
    subscriptions[key] = {
      plan,
      billingPeriod: parsePeriod(
        offer.billing_period,
        `payments.subscriptions.${key}.billing_period`,
      ),
      providers,
      ...(renewalCredits ? { renewalCredits } : {}),
      stackCredits,
    };
  }
  const topupsRaw = assertRecord(raw.topups ?? {}, "payments.topups");
  for (const [key, input] of Object.entries(topupsRaw)) {
    id(key, "payments.topups key");
    const offer = assertRecord(input, `payments.topups.${key}`);
    assertKnown(offer, ["credits", "bucket", "providers"], `payments.topups.${key}`);
    const bucket = string(offer.bucket, `payments.topups.${key}.bucket`);
    if (!credits.buckets[bucket])
      throw new ConfigError(`payments.topups.${key}.bucket references an unknown bucket`);
    const providers = parseProviders(offer.providers, `payments.topups.${key}.providers`);
    checkRefs(providers);
    topups[key] = {
      credits: decimal(offer.credits, `payments.topups.${key}.credits`, false),
      bucket,
      providers,
    };
  }
  let autoRecharge: AutoRechargePolicy | undefined;
  if (raw.auto_recharge != null) {
    const policy = assertRecord(raw.auto_recharge, "payments.auto_recharge");
    assertKnown(policy, ["trigger", "purchase", "limit"], "payments.auto_recharge");
    const trigger = assertRecord(policy.trigger, "payments.auto_recharge.trigger");
    assertKnown(trigger, ["balance_below"], "payments.auto_recharge.trigger");
    const purchase = assertRecord(policy.purchase, "payments.auto_recharge.purchase");
    assertKnown(purchase, ["topup", "quantity"], "payments.auto_recharge.purchase");
    const topup = string(purchase.topup, "payments.auto_recharge.purchase.topup");
    if (!topups[topup])
      throw new ConfigError("payments.auto_recharge.purchase.topup references an unknown top-up");
    const limit = assertRecord(policy.limit, "payments.auto_recharge.limit");
    assertKnown(limit, ["max_purchases", "period"], "payments.auto_recharge.limit");
    autoRecharge = {
      trigger: {
        balanceBelow: decimal(
          trigger.balance_below,
          "payments.auto_recharge.trigger.balance_below",
        ),
      },
      purchase: {
        topup,
        quantity:
          purchase.quantity == null
            ? 1
            : positiveInt(purchase.quantity, "payments.auto_recharge.purchase.quantity"),
      },
      limit: {
        maxPurchases: positiveInt(
          limit.max_purchases,
          "payments.auto_recharge.limit.max_purchases",
        ),
        period: parsePeriod(limit.period, "payments.auto_recharge.limit.period"),
      },
    };
  }
  return { subscriptions, topups, ...(autoRecharge ? { autoRecharge } : {}) };
}

export function loadConfigFromDict(data: BursarConfigData): ParsedBursarConfig {
  const raw = assertRecord(data, "config");
  assertKnown(raw, ["version", "usage", "credits", "plans", "payments"], "config");
  if ((raw.version ?? 1) !== 1) throw new ConfigError("version must be 1");
  const usage = raw.usage == null ? undefined : parseUsage(raw.usage);
  const credits = parseCredits(raw.credits);
  const plans = parsePlans(raw.plans, usage, credits);
  const payments = parsePayments(raw.payments, plans, credits);
  return {
    version: 1,
    ...(usage ? { usage } : {}),
    credits,
    plans,
    ...(payments ? { payments } : {}),
  };
}

function toSnakeCase(value: unknown): unknown {
  if (value instanceof Decimal) return value.toString();
  if (Array.isArray(value)) return value.map(toSnakeCase);
  if (!isRecord(value)) return value;
  return Object.fromEntries(
    Object.entries(value)
      .filter(([, child]) => child !== undefined)
      .map(([key, child]) => [
        key.replace(/[A-Z]/g, (letter) => `_${letter.toLowerCase()}`),
        toSnakeCase(child),
      ]),
  );
}

export function canonicalBursarConfigDict(data: BursarConfigData): BursarConfigData {
  return toSnakeCase(loadConfigFromDict(data)) as BursarConfigData;
}
