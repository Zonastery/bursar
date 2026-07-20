import Decimal from "decimal.js";
import { ConfigError } from "./errors.js";
import { validateExpression } from "./expr.js";
import type { AllowancePeriod, FeatureLimitPeriod } from "./allowance.js";
import type { BillingMode, OperationPolicy, PlanDefinition, BucketDefinition } from "./types.js";
import type {
  BillingCreditTopup,
  BillingOffer,
  BillingOfferInterval,
  SubscriptionGrant,
} from "./billing/billing-types.js";

/** Valid `allowancePeriod` values (WS9b). */
const ALLOWANCE_PERIODS: ReadonlySet<string> = new Set([
  "calendar_month",
  "rolling_30d",
  "anniversary",
]);

/** Valid `FeatureLimit.period` cadences (mirrors Python `resolve_calendar_window`). */
const FEATURE_LIMIT_PERIODS: ReadonlySet<string> = new Set([
  "daily",
  "weekly",
  "monthly",
  "yearly",
]);

/** Valid `FeatureLimit.onExceed` values (mirrors `SpendCap.action`). */
const FEATURE_LIMIT_ACTIONS: ReadonlySet<string> = new Set(["deny", "warn", "notify"]);

/**
 * Canonical metric-variable set — MUST mirror `PricingEngine.buildVariables`
 * exactly. Expressions may only reference these names (or allowed functions).
 * Passed into `validateExpression` so typos like `inputtokens` fail at
 * config-load, not at first runtime use (M5).
 */
export const KNOWN_VARIABLES: ReadonlySet<string> = new Set([
  "input_tokens",
  "output_tokens",
  "cache_read_tokens",
  "cache_write_tokens",
  "tool_calls",
  "search_queries",
  "search_results",
  "web_search_calls",
  "code_exec_calls",
]);

/** Metering configuration section. */
export interface MeteringConfig {
  models: Record<string, string>;
  tools: Record<string, string>;
  search?: string | null;
  cacheDiscount?: string | null;
  flatJobs: Record<string, Decimal>;
}

/** Credits granted to new users on signup, routed to a configured bucket. */
export interface SignupGrant {
  amount: number;
  bucket: string;
}

/** Ledger configuration section. */
export interface LedgerConfig {
  minBalance: Decimal;
  signupGrant?: SignupGrant | null;
  buckets?: Record<string, BucketDefinition> | null;
}

/** Parsed entitlement entry (missing `maxCalls` means unlimited). */
export interface PlanEntitlement {
  value?: unknown;
  maxCalls: number | null;
  period: FeatureLimitPeriod;
  onExceed: "deny" | "warn" | "notify";
}

/** Billing configuration section. */
export interface BillingSection {
  currency: string;
  subscriptions: Record<string, BillingOffer>;
  topups: Record<string, BillingCreditTopup>;
}

/** Internal validated pricing configuration. */
export interface ParsedBursarConfig {
  version: number;
  metering: MeteringConfig;
  ledger: LedgerConfig;
  plans?: Record<string, PlanDefinition> | null;
  billing?: BillingSection;
}

/** Canonical snake_case configuration document accepted by the SDK. */
export type BursarConfigData = Record<string, unknown>;

/** Known top-level keys, checked after decoding the snake_case wire shape. */
const TOP_LEVEL_KEYS: ReadonlySet<string> = new Set([
  "version",
  "metering",
  "ledger",
  "plans",
  "billing",
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

/** Known metering-section keys after decoding. */
const METERING_KEYS: ReadonlySet<string> = new Set([
  "models",
  "tools",
  "search",
  "cacheDiscount",
  "flatJobs",
]);

/** Known ledger-section keys after decoding. */
const LEDGER_KEYS: ReadonlySet<string> = new Set(["minBalance", "signupGrant", "buckets"]);

/** Known plan-definition keys (new nested schema). */
const PLAN_KEYS: ReadonlySet<string> = new Set([
  "label",
  "tier",
  "allowance",
  "safety",
  "rateOverrides",
  "entitlements",
]);

/** Known `FeatureLimit` keys. */
const FEATURE_LIMIT_KEYS: ReadonlySet<string> = new Set([
  "maxCalls",
  "period",
  "onExceed",
  "value",
]);

/** Known `OperationPolicy` keys (entries of a plan's `safety.perOperation` map). */
const OPERATION_POLICY_KEYS: ReadonlySet<string> = new Set([
  "billingMode",
  "maxConcurrent",
  "overdraftFloor",
]);

/** Known billing-section keys. */
const BILLING_KEYS: ReadonlySet<string> = new Set(["currency", "subscriptions", "topups"]);

/** Known billing-offer keys. */
const BILLING_OFFER_KEYS: ReadonlySet<string> = new Set([
  "plan",
  "interval",
  "intervalCount",
  "grant",
  "providers",
  "validFrom",
  "validTo",
]);

/** Known billing-topup keys. */
const BILLING_TOPUP_KEYS: ReadonlySet<string> = new Set([
  "depositTo",
  "creditsPerUnit",
  "minAmountMinor",
  "maxAmountMinor",
  "taxBehavior",
  "providers",
]);

/** Known provider-ref keys. */
const PROVIDER_REF_KEYS: ReadonlySet<string> = new Set([
  "productId",
  "priceId",
  "variantId",
  "lookupKey",
]);

const BILLING_OFFER_INTERVALS: ReadonlySet<string> = new Set(["day", "week", "month", "year"]);
const TAX_BEHAVIORS: ReadonlySet<string> = new Set(["exclude_tax", "include_tax"]);

/** Known signup-grant keys. */
const SIGNUP_GRANT_KEYS: ReadonlySet<string> = new Set(["amount", "bucket"]);

/** Known bucket-definition keys. */
const BUCKET_KEYS: ReadonlySet<string> = new Set([
  "label",
  "priority",
  "expires",
  "ttlDays",
  "allowOverdraft",
  "default",
]);

/** Known allowance-section keys. */
const ALLOWANCE_KEYS: ReadonlySet<string> = new Set(["amount", "period"]);

/** Known safety-section keys. */
const SAFETY_KEYS: ReadonlySet<string> = new Set([
  "billingMode",
  "perOperation",
  "maxConcurrent",
  "overdraftFloor",
]);

/** Reject any key not in `known` — catches typos that would otherwise silently fall back to a default. */
function assertKnownKeys(
  obj: Record<string, unknown>,
  known: ReadonlySet<string>,
  context: string,
): void {
  for (const key of Object.keys(obj)) {
    if (!known.has(key)) {
      throw new ConfigError(`unknown config key in ${context}: ${key}`);
    }
  }
}

/** Variable set for validating `tools` expressions: base set + `calls` (WS2). */
const TOOLS_VARIABLES: ReadonlySet<string> = new Set([...KNOWN_VARIABLES, "calls"]);

function validateExpressions(raw: ParsedBursarConfig): void {
  for (const [key, expr] of Object.entries(raw.metering.models)) {
    try {
      validateExpression(expr, KNOWN_VARIABLES);
    } catch (e) {
      throw new ConfigError(
        `invalid expression in metering.models.${key}: ${(e as Error).message}`,
      );
    }
  }
  for (const [key, expr] of Object.entries(raw.metering.tools)) {
    try {
      validateExpression(expr, TOOLS_VARIABLES);
    } catch (e) {
      throw new ConfigError(`invalid expression in metering.tools.${key}: ${(e as Error).message}`);
    }
  }
  if (raw.metering.search) {
    try {
      validateExpression(raw.metering.search, KNOWN_VARIABLES);
    } catch (e) {
      throw new ConfigError(`invalid expression in metering.search: ${(e as Error).message}`);
    }
  }
  if (raw.metering.cacheDiscount) {
    try {
      validateExpression(raw.metering.cacheDiscount, KNOWN_VARIABLES);
    } catch (e) {
      throw new ConfigError(
        `invalid expression in metering.cacheDiscount: ${(e as Error).message}`,
      );
    }
  }
}

/**
 * Decode documented snake_case config fields into the camelCase internal model.
 * CamelCase input is rejected so mixed representations cannot collide or
 * silently select a different value.
 */
function decodeSnakeFields(
  data: Record<string, unknown>,
  context: string,
): Record<string, unknown> {
  const keyMap: Record<string, string> = {
    min_balance: "minBalance",
    signup_grant: "signupGrant",
    rate_overrides: "rateOverrides",
    billing_mode: "billingMode",
    overdraft_floor: "overdraftFloor",
    per_operation: "perOperation",
    max_concurrent: "maxConcurrent",
    ttl_days: "ttlDays",
    allow_overdraft: "allowOverdraft",
    max_calls: "maxCalls",
    on_exceed: "onExceed",
    cache_discount: "cacheDiscount",
    flat_jobs: "flatJobs",
    interval_count: "intervalCount",
    deposit_to: "depositTo",
    credits_per_unit: "creditsPerUnit",
    min_amount_minor: "minAmountMinor",
    max_amount_minor: "maxAmountMinor",
    tax_behavior: "taxBehavior",
    product_id: "productId",
    price_id: "priceId",
    variant_id: "variantId",
    lookup_key: "lookupKey",
    replace_prior: "replacePrior",
    valid_from: "validFrom",
    valid_to: "validTo",
    allowance: "allowance",
    safety: "safety",
    entitlements: "entitlements",
    label: "label",
    metering: "metering",
    ledger: "ledger",
    billing: "billing",
  };
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(data)) {
    if (Object.entries(keyMap).some(([snake, camel]) => snake !== camel && camel === k)) {
      throw new ConfigError(`${context}.${k} must use snake_case`);
    }
    const mapped = keyMap[k] ?? k;
    out[mapped] = v;
  }
  return out;
}

/** Decode a plan definition while preserving its dynamic map keys. */
function decodePlan(p: Record<string, unknown>): Record<string, unknown> {
  const out = decodeSnakeFields(p, "plan");
  // Normalise nested allowance section
  if (out.allowance != null && typeof out.allowance === "object" && !Array.isArray(out.allowance)) {
    out.allowance = decodeSnakeFields(out.allowance as Record<string, unknown>, "plan.allowance");
  }
  // Normalise nested safety section
  if (out.safety != null && typeof out.safety === "object" && !Array.isArray(out.safety)) {
    out.safety = decodeSnakeFields(out.safety as Record<string, unknown>, "plan.safety");
  }
  return out;
}

/** Decode a bucket definition while preserving the bucket identifier. */
function decodeBucket(t: Record<string, unknown>): Record<string, unknown> {
  return decodeSnakeFields(t, "ledger.buckets");
}

/**
 * Decode + validate a plan's `entitlements` map. Feature identifiers and
 * arbitrary entitlement values are opaque and remain unchanged.
 */
function decodeEntitlements(
  planKey: string,
  raw: Record<string, unknown>,
): Record<string, PlanEntitlement> {
  const out: Record<string, PlanEntitlement> = {};
  for (const [featureKey, rawLimit] of Object.entries(raw)) {
    const limit = decodeSnakeFields(
      (rawLimit ?? {}) as Record<string, unknown>,
      `plans.${planKey}.entitlements.${featureKey}`,
    );
    assertKnownKeys(limit, FEATURE_LIMIT_KEYS, `plans.${planKey}.entitlements.${featureKey}`);
    const rawMax = limit.maxCalls;
    let maxCalls: number | null = null;
    if (rawMax !== undefined && rawMax !== null) {
      maxCalls = Number(rawMax);
      if (!Number.isFinite(maxCalls) || maxCalls < 0) {
        throw new ConfigError(
          `invalid entitlements in plans.${planKey}.${featureKey}: maxCalls must be >= 0, got ${String(limit.maxCalls)}`,
        );
      }
    }
    const period = (limit.period as string | undefined) ?? "monthly";
    const onExceed = (limit.onExceed as string | undefined) ?? "deny";
    if (!FEATURE_LIMIT_PERIODS.has(period)) {
      throw new ConfigError(
        `invalid entitlements in plans.${planKey}.${featureKey}: unknown period '${period}' ` +
          `(expected one of ${[...FEATURE_LIMIT_PERIODS].sort().join(", ")})`,
      );
    }
    if (!FEATURE_LIMIT_ACTIONS.has(onExceed)) {
      throw new ConfigError(
        `invalid entitlements in plans.${planKey}.${featureKey}: unknown onExceed '${onExceed}' ` +
          `(expected one of ${[...FEATURE_LIMIT_ACTIONS].sort().join(", ")})`,
      );
    }
    out[featureKey] = {
      maxCalls,
      period: period as FeatureLimitPeriod,
      onExceed: onExceed as PlanEntitlement["onExceed"],
      value: limit.value,
    };
  }
  return out;
}

/**
 * Decode + validate a plan's `safety.per_operation` map (per-operation
 * financial-safety policy overrides, mirroring Python's `OperationPolicy`).
 */
function decodePerOperation(
  planKey: string,
  raw: Record<string, unknown>,
): Record<string, OperationPolicy> {
  const out: Record<string, OperationPolicy> = {};
  for (const [opType, rawPolicy] of Object.entries(raw)) {
    const policy = decodeSnakeFields(
      (rawPolicy ?? {}) as Record<string, unknown>,
      `plans.${planKey}.safety.per_operation.${opType}`,
    );
    assertKnownKeys(
      policy,
      OPERATION_POLICY_KEYS,
      `plans.${planKey}.safety.perOperation.${opType}`,
    );
    const billingMode = (policy.billingMode as string | undefined) ?? "strict";
    if (billingMode !== "strict" && billingMode !== "overdraft") {
      throw new ConfigError(
        `invalid billingMode in plans.${planKey}.safety.perOperation.${opType}: '${billingMode}' ` +
          `(expected 'strict' or 'overdraft')`,
      );
    }
    out[opType] = {
      billingMode: billingMode as BillingMode,
      maxConcurrent: (policy.maxConcurrent as number | null) ?? null,
      overdraftFloor:
        policy.overdraftFloor != null
          ? new Decimal(policy.overdraftFloor as number | string)
          : null,
    };
  }
  return out;
}

// ── Section parsers (extracted from loadConfigFromDict, M6) ─────────

function parseMetering(raw: Record<string, unknown>): MeteringConfig {
  const normalised = decodeSnakeFields(raw, "metering");
  assertKnownKeys(normalised, METERING_KEYS, "metering");

  if (normalised.models == null) throw new ConfigError("missing required section: metering.models");
  if (
    typeof normalised.models !== "object" ||
    Object.keys(normalised.models as object).length === 0
  ) {
    throw new ConfigError("metering.models must be a non-empty dict");
  }

  const metering: MeteringConfig = {
    models: normalised.models as Record<string, string>,
    tools: (normalised.tools as Record<string, string> | undefined) ?? { "*": "calls * 0" },
    search: (normalised.search as string | null | undefined) ?? null,
    cacheDiscount: (normalised.cacheDiscount as string | null | undefined) ?? null,
    flatJobs: Object.fromEntries(
      Object.entries(
        (normalised.flatJobs as Record<string, number | string> | undefined) ?? {},
      ).map(([job, cost]) => [job, new Decimal(cost)]),
    ),
  };

  for (const [job, cost] of Object.entries(metering.flatJobs)) {
    if (cost.isNegative()) {
      throw new ConfigError(`metering.flatJobs.${job} must be >= 0, got ${cost.toString()}`);
    }
  }

  return metering;
}

function parseSignupGrant(raw: unknown): SignupGrant | null {
  if (raw === undefined || raw === null) return null;
  if (typeof raw === "number") {
    throw new ConfigError(
      "ledger.signupGrant must be an object { amount, bucket }, not a scalar number",
    );
  }
  if (typeof raw !== "object" || Array.isArray(raw)) {
    throw new ConfigError("ledger.signupGrant must be an object { amount, bucket }");
  }
  const grant = decodeSnakeFields(raw as Record<string, unknown>, "ledger.signup_grant");
  assertKnownKeys(grant, SIGNUP_GRANT_KEYS, "ledger.signupGrant");
  const amount = Number(grant.amount);
  if (!Number.isFinite(amount) || amount < 0) {
    throw new ConfigError(`ledger.signupGrant.amount must be >= 0, got ${String(grant.amount)}`);
  }
  if (grant.bucket == null || grant.bucket === "") {
    throw new ConfigError("ledger.signupGrant.bucket is required");
  }
  return { amount, bucket: String(grant.bucket) };
}

function parseLedger(raw: Record<string, unknown>): LedgerConfig {
  const normalised = decodeSnakeFields(raw, "ledger");
  assertKnownKeys(normalised, LEDGER_KEYS, "ledger");

  const minBalance = new Decimal((normalised.minBalance as number | string | undefined) ?? 0);
  if (minBalance.isNegative()) throw new ConfigError("ledger.minBalance must be >= 0");

  return {
    minBalance,
    signupGrant: parseSignupGrant(normalised.signupGrant),
    buckets: undefined,
  };
}

function parseBuckets(raw: Record<string, unknown>): Record<string, BucketDefinition> | undefined {
  if (raw.buckets === undefined || raw.buckets === null) return undefined;
  if (typeof raw.buckets !== "object" || Array.isArray(raw.buckets)) {
    throw new ConfigError("ledger.buckets must be a dict of bucket definitions");
  }
  if (Object.keys(raw.buckets as object).length === 0) {
    throw new ConfigError(
      "ledger.buckets must not be an empty object; omit the `buckets` key entirely for no buckets",
    );
  }

  const rawBuckets = raw.buckets as Record<string, Record<string, unknown>>;
  const buckets = Object.fromEntries(
    Object.entries(rawBuckets).map(([k, v]) => [k, decodeBucket(v)]),
  );

  let overdraftCount = 0;
  let defaultCount = 0;
  for (const [bucketKey, t] of Object.entries(buckets)) {
    assertKnownKeys(t, BUCKET_KEYS, `ledger.buckets.${bucketKey}`);
    if (t.allowOverdraft === true) overdraftCount++;
    if (t["default"] === true) defaultCount++;
    if (t.ttlDays != null && (t.ttlDays as number) <= 0) {
      throw new ConfigError(
        `ledger.buckets.${bucketKey}.ttlDays must be > 0, got ${String(t.ttlDays)}`,
      );
    }
  }
  if (overdraftCount > 1) {
    throw new ConfigError("at most one bucket may set allowOverdraft: true");
  }
  if (defaultCount > 1) {
    throw new ConfigError('at most one bucket may set "default": true');
  }

  const bucketDefs: Record<string, BucketDefinition> = {};
  for (const [key, t] of Object.entries(buckets)) {
    bucketDefs[key] = {
      label: (t.label as string | undefined) ?? key,
      priority: Number(t.priority ?? 0),
      expires: Boolean(t.expires ?? false),
      ttlDays: t.ttlDays != null ? Number(t.ttlDays) : null,
      allowOverdraft: Boolean(t.allowOverdraft ?? false),
      default: Boolean(t["default"] ?? false),
    };
  }
  return bucketDefs;
}

function parsePlans(raw: Record<string, unknown>): Record<string, PlanDefinition> | undefined {
  const rawPlans = raw.plans as Record<string, Record<string, unknown>> | undefined;
  if (!rawPlans) return undefined;

  const plans = Object.fromEntries(Object.entries(rawPlans).map(([k, v]) => [k, decodePlan(v)]));

  for (const [planKey, plan] of Object.entries(plans)) {
    assertKnownKeys(plan, PLAN_KEYS, `plans.${planKey}`);
    if (plan.label == null) {
      throw new ConfigError(`plan definition is missing required 'label' field: plans.${planKey}`);
    }
    if (plan.tier != null && (!Number.isInteger(plan.tier) || Number(plan.tier) < 0)) {
      throw new ConfigError(`plans.${planKey}.tier must be a non-negative integer`);
    }
    if (plan.allowance != null) {
      if (typeof plan.allowance !== "object" || Array.isArray(plan.allowance)) {
        throw new ConfigError(`plans.${planKey}.allowance must be a dict`);
      }
      assertKnownKeys(
        plan.allowance as Record<string, unknown>,
        ALLOWANCE_KEYS,
        `plans.${planKey}.allowance`,
      );
      const allowance = plan.allowance as Record<string, unknown>;
      if (allowance.period != null && !ALLOWANCE_PERIODS.has(allowance.period as string)) {
        throw new ConfigError(
          `invalid allowance period in plans.${planKey}: ${String(allowance.period)} ` +
            `(expected one of ${[...ALLOWANCE_PERIODS].sort().join(", ")})`,
        );
      }
    }
    if (plan.safety != null) {
      if (typeof plan.safety !== "object" || Array.isArray(plan.safety)) {
        throw new ConfigError(`plans.${planKey}.safety must be a dict`);
      }
      assertKnownKeys(
        plan.safety as Record<string, unknown>,
        SAFETY_KEYS,
        `plans.${planKey}.safety`,
      );
      const safety = plan.safety as Record<string, unknown>;
      if (
        safety.billingMode != null &&
        safety.billingMode !== "strict" &&
        safety.billingMode !== "overdraft"
      ) {
        throw new ConfigError(
          `invalid billingMode in plans.${planKey}.safety: '${String(safety.billingMode)}' ` +
            `(expected 'strict' or 'overdraft')`,
        );
      }
    }
    const overrides = plan.rateOverrides as Record<string, string> | undefined;
    if (overrides) {
      for (const [modelKey, expr] of Object.entries(overrides)) {
        try {
          validateExpression(expr, KNOWN_VARIABLES);
        } catch (e) {
          throw new ConfigError(
            `invalid expression in plans.${planKey}.rateOverrides.${modelKey}: ${(e as Error).message}`,
          );
        }
      }
    }
    if (plan.entitlements != null) {
      if (typeof plan.entitlements !== "object" || Array.isArray(plan.entitlements)) {
        throw new ConfigError(`plans.${planKey}.entitlements must be a dict`);
      }
      plan.entitlements = decodeEntitlements(planKey, plan.entitlements as Record<string, unknown>);
    }
  }

  const planLabels = Object.values(plans).map((p) => p.label as string);
  if (new Set(planLabels).size !== planLabels.length) {
    throw new ConfigError("duplicate plan labels in pricing config");
  }

  const planDefs: Record<string, PlanDefinition> = {};
  for (const [key, p] of Object.entries(plans)) {
    const allowanceRaw = (p.allowance as Record<string, unknown>) ?? {};
    const allowanceAmount = new Decimal(
      (allowanceRaw["amount"] as number | string | undefined) ?? 0,
    );
    if (allowanceAmount.isNegative()) {
      throw new ConfigError(
        `plans.${key}.allowance.amount must be >= 0, got ${allowanceAmount.toString()}`,
      );
    }
    const safetyRaw = (p.safety as Record<string, unknown>) ?? {};
    const billingMode = (safetyRaw.billingMode ?? "strict") as "strict" | "overdraft";
    const perOperationRaw = safetyRaw.perOperation as Record<string, unknown> | undefined;

    planDefs[key] = {
      label: p.label as string,
      ...(p.tier != null ? { tier: Number(p.tier) } : {}),
      allowance: {
        amount: allowanceAmount,
        period: ((allowanceRaw["period"] as AllowancePeriod) ??
          "calendar_month") as AllowancePeriod,
      },
      safety: {
        billingMode,
        perOperation:
          perOperationRaw != null
            ? decodePerOperation(key, perOperationRaw as Record<string, unknown>)
            : undefined,
        maxConcurrent: (safetyRaw.maxConcurrent as number | null) ?? null,
        overdraftFloor:
          safetyRaw.overdraftFloor != null
            ? new Decimal(safetyRaw.overdraftFloor as number | string)
            : null,
      },
      rateOverrides: (p.rateOverrides as Record<string, string>) ?? null,
      entitlements: (p.entitlements as Record<string, PlanEntitlement>) ?? null,
    };
  }
  return planDefs;
}

function parseProviderRefs(
  raw: unknown,
  context: string,
): Record<
  string,
  { productId?: string; priceId?: string; variantId?: string; lookupKey?: string }
> {
  if (raw == null) return {};
  if (typeof raw !== "object" || Array.isArray(raw)) {
    throw new ConfigError(`${context} must be a dict of provider refs`);
  }
  const out: Record<
    string,
    { productId?: string; priceId?: string; variantId?: string; lookupKey?: string }
  > = {};
  for (const [providerKey, providerVal] of Object.entries(raw as Record<string, unknown>)) {
    const ref = decodeSnakeFields(
      (providerVal ?? {}) as Record<string, unknown>,
      `${context}.${providerKey}`,
    );
    assertKnownKeys(ref, PROVIDER_REF_KEYS, `${context}.${providerKey}`);
    out[providerKey] = {
      ...(ref.productId != null ? { productId: String(ref.productId) } : {}),
      ...(ref.priceId != null ? { priceId: String(ref.priceId) } : {}),
      ...(ref.variantId != null ? { variantId: String(ref.variantId) } : {}),
      ...(ref.lookupKey != null ? { lookupKey: String(ref.lookupKey) } : {}),
    };
  }
  return out;
}

function parseGrant(raw: unknown, context: string): SubscriptionGrant {
  const grant = decodeSnakeFields(
    (raw ?? { mode: "allowance" }) as Record<string, unknown>,
    context,
  );
  const mode = (grant.mode as string | undefined) ?? "allowance";
  if (mode === "allowance") {
    assertKnownKeys(grant, new Set(["mode"]), context);
    return { mode: "allowance" };
  }
  if (mode === "cycle_grant") {
    assertKnownKeys(grant, new Set(["mode", "credits", "bucket", "replacePrior"]), context);
    const credits = Number(grant.credits);
    if (!Number.isFinite(credits) || credits < 0) {
      throw new ConfigError(`${context}.credits must be >= 0, got ${String(grant.credits)}`);
    }
    if (grant.bucket == null || grant.bucket === "") {
      throw new ConfigError(`${context}.bucket is required for cycle_grant`);
    }
    return {
      mode: "cycle_grant",
      credits,
      bucket: String(grant.bucket),
      replacePrior: Boolean(grant.replacePrior ?? true),
    };
  }
  throw new ConfigError(`unknown grant mode in ${context}: '${mode}'`);
}

function parseBillingOffer(raw: unknown, context: string): BillingOffer {
  const offer = decodeSnakeFields((raw ?? {}) as Record<string, unknown>, context);
  assertKnownKeys(offer, BILLING_OFFER_KEYS, context);
  if (offer.plan == null || offer.plan === "") {
    throw new ConfigError(`${context} is missing required 'plan' field`);
  }
  const interval = (offer.interval as string | undefined) ?? "month";
  if (!BILLING_OFFER_INTERVALS.has(interval)) {
    throw new ConfigError(
      `invalid interval in ${context}: '${interval}' ` +
        `(expected one of ${[...BILLING_OFFER_INTERVALS].sort().join(", ")})`,
    );
  }
  const intervalCount = Number(offer.intervalCount ?? 1);
  if (!Number.isFinite(intervalCount) || intervalCount < 1) {
    throw new ConfigError(
      `${context}.intervalCount must be >= 1, got ${String(offer.intervalCount)}`,
    );
  }
  return {
    plan: String(offer.plan),
    interval: interval as BillingOfferInterval,
    intervalCount,
    grant: parseGrant(offer.grant, `${context}.grant`),
    providers: parseProviderRefs(offer.providers, `${context}.providers`),
    validFrom: offer.validFrom != null ? String(offer.validFrom) : null,
    validTo: offer.validTo != null ? String(offer.validTo) : null,
  };
}

function parseBillingTopup(raw: unknown, context: string): BillingCreditTopup {
  const topup = decodeSnakeFields((raw ?? {}) as Record<string, unknown>, context);
  assertKnownKeys(topup, BILLING_TOPUP_KEYS, context);
  const creditsPerUnit = Number(topup.creditsPerUnit ?? 1000);
  if (!Number.isFinite(creditsPerUnit) || creditsPerUnit < 0) {
    throw new ConfigError(
      `${context}.creditsPerUnit must be >= 0, got ${String(topup.creditsPerUnit)}`,
    );
  }
  const minAmountMinor = Number(topup.minAmountMinor ?? 500);
  const maxAmountMinor = Number(topup.maxAmountMinor ?? 500_000);
  if (!Number.isFinite(minAmountMinor) || minAmountMinor < 0) {
    throw new ConfigError(
      `${context}.minAmountMinor must be >= 0, got ${String(topup.minAmountMinor)}`,
    );
  }
  if (!Number.isFinite(maxAmountMinor) || maxAmountMinor < 0) {
    throw new ConfigError(
      `${context}.maxAmountMinor must be >= 0, got ${String(topup.maxAmountMinor)}`,
    );
  }
  const taxBehavior = (topup.taxBehavior as string | undefined) ?? "exclude_tax";
  if (!TAX_BEHAVIORS.has(taxBehavior)) {
    throw new ConfigError(
      `invalid taxBehavior in ${context}: '${taxBehavior}' ` +
        `(expected one of ${[...TAX_BEHAVIORS].sort().join(", ")})`,
    );
  }
  if (topup.depositTo == null || topup.depositTo === "") {
    throw new ConfigError(`${context} is missing required 'depositTo' field`);
  }
  return {
    depositTo: String(topup.depositTo),
    creditsPerUnit,
    minAmountMinor,
    maxAmountMinor,
    taxBehavior: taxBehavior as BillingCreditTopup["taxBehavior"],
    providers: parseProviderRefs(topup.providers, `${context}.providers`),
  };
}

function parseBilling(raw: unknown): BillingSection | undefined {
  if (raw == null) return undefined;
  if (!isRecord(raw)) throw new ConfigError("billing must be a dict");
  const section = decodeSnakeFields(raw, "billing");
  assertKnownKeys(section, BILLING_KEYS, "billing");
  const subscriptions: Record<string, BillingOffer> = {};
  if (section.subscriptions != null) {
    if (typeof section.subscriptions !== "object" || Array.isArray(section.subscriptions)) {
      throw new ConfigError("billing.subscriptions must be a dict");
    }
    for (const [key, val] of Object.entries(section.subscriptions as Record<string, unknown>)) {
      subscriptions[key] = parseBillingOffer(val, `billing.subscriptions.${key}`);
    }
  }
  const topups: Record<string, BillingCreditTopup> = {};
  if (section.topups != null) {
    if (typeof section.topups !== "object" || Array.isArray(section.topups)) {
      throw new ConfigError("billing.topups must be a dict");
    }
    for (const [key, val] of Object.entries(section.topups as Record<string, unknown>)) {
      topups[key] = parseBillingTopup(val, `billing.topups.${key}`);
    }
  }
  return {
    currency: String(section.currency ?? "USD"),
    subscriptions,
    topups,
  };
}

function validatePlanReferences(config: ParsedBursarConfig): void {
  const billing = config.billing;
  if (!billing?.subscriptions || Object.keys(billing.subscriptions).length === 0) return;
  const plans = config.plans ?? {};
  for (const [offerKey, offer] of Object.entries(billing.subscriptions)) {
    if (!Object.prototype.hasOwnProperty.call(plans, offer.plan)) {
      throw new ConfigError(
        `billing.subscriptions.${offerKey}.plan references unknown plan '${offer.plan}'`,
      );
    }
  }
}

function validateRateOverrideKeys(config: ParsedBursarConfig): void {
  if (!config.plans) return;
  const modelKeys = new Set(Object.keys(config.metering.models));
  for (const [planId, planDef] of Object.entries(config.plans)) {
    if (!planDef.rateOverrides) continue;
    for (const overrideKey of Object.keys(planDef.rateOverrides)) {
      if (overrideKey !== "*" && !modelKeys.has(overrideKey)) {
        throw new ConfigError(
          `plans.${planId}.rateOverrides.${overrideKey} references unknown model ` +
            `(must be one of ${[...modelKeys].sort().join(", ")} or '*')`,
        );
      }
    }
  }
}

function validateBucketReferences(config: ParsedBursarConfig): void {
  const buckets = config.ledger.buckets;
  const bucketKeys = buckets ? new Set(Object.keys(buckets)) : null;

  const signupGrant = config.ledger.signupGrant;
  if (signupGrant != null) {
    if (!bucketKeys) {
      throw new ConfigError("ledger.buckets must be defined when ledger.signupGrant is set");
    }
    if (!bucketKeys.has(signupGrant.bucket)) {
      throw new ConfigError(
        `ledger.signupGrant.bucket references unknown bucket '${signupGrant.bucket}' ` +
          `(must be one of ${[...bucketKeys].sort().join(", ")})`,
      );
    }
  }

  if (!bucketKeys) return;

  const billing = config.billing;
  if (!billing) return;

  for (const [offerKey, offer] of Object.entries(billing.subscriptions)) {
    const grant = offer.grant;
    if (grant?.mode === "cycle_grant" && grant.bucket != null) {
      if (!bucketKeys.has(grant.bucket)) {
        throw new ConfigError(
          `billing.subscriptions.${offerKey}.grant.bucket references unknown bucket '${grant.bucket}' ` +
            `(must be one of ${[...bucketKeys].sort().join(", ")})`,
        );
      }
    }
  }

  for (const [topupKey, topup] of Object.entries(billing.topups)) {
    if (topup.depositTo != null && !bucketKeys.has(topup.depositTo)) {
      throw new ConfigError(
        `billing.topups.${topupKey}.depositTo references unknown bucket '${topup.depositTo}' ` +
          `(must be one of ${[...bucketKeys].sort().join(", ")})`,
      );
    }
  }
}

function decToJson(value: Decimal): number | string {
  return value.isInteger() ? value.toNumber() : value.toString();
}

function providerRefsToSnake(
  refs: Record<
    string,
    {
      productId?: string | null;
      priceId?: string | null;
      variantId?: string | null;
      lookupKey?: string | null;
    }
  >,
): Record<string, Record<string, string>> {
  return Object.fromEntries(
    Object.entries(refs).map(([provider, ref]) => [
      provider,
      {
        ...(ref.productId != null ? { product_id: ref.productId } : {}),
        ...(ref.priceId != null ? { price_id: ref.priceId } : {}),
        ...(ref.variantId != null ? { variant_id: ref.variantId } : {}),
        ...(ref.lookupKey != null ? { lookup_key: ref.lookupKey } : {}),
      },
    ]),
  );
}

/** Convert a validated config to the canonical snake_case JSON shape. */
export function bursarConfigToSnakeDict(config: ParsedBursarConfig): Record<string, unknown> {
  const plans = config.plans
    ? Object.fromEntries(
        Object.entries(config.plans).map(([key, plan]) => [
          key,
          {
            label: plan.label,
            ...(plan.tier != null ? { tier: plan.tier } : {}),
            allowance: {
              amount: decToJson(plan.allowance.amount),
              period: plan.allowance.period,
            },
            safety: {
              billing_mode: plan.safety.billingMode,
              ...(plan.safety.maxConcurrent != null
                ? { max_concurrent: plan.safety.maxConcurrent }
                : {}),
              ...(plan.safety.overdraftFloor != null
                ? { overdraft_floor: decToJson(plan.safety.overdraftFloor) }
                : {}),
              ...(plan.safety.perOperation
                ? {
                    per_operation: Object.fromEntries(
                      Object.entries(plan.safety.perOperation).map(([op, policy]) => [
                        op,
                        {
                          billing_mode: policy.billingMode,
                          ...(policy.maxConcurrent != null
                            ? { max_concurrent: policy.maxConcurrent }
                            : {}),
                          ...(policy.overdraftFloor != null
                            ? { overdraft_floor: decToJson(policy.overdraftFloor) }
                            : {}),
                        },
                      ]),
                    ),
                  }
                : {}),
            },
            ...(plan.rateOverrides ? { rate_overrides: plan.rateOverrides } : {}),
            ...(plan.entitlements
              ? {
                  entitlements: Object.fromEntries(
                    Object.entries(plan.entitlements).map(([fk, ent]) => [
                      fk,
                      {
                        ...(ent.value !== undefined ? { value: ent.value } : {}),
                        ...(ent.maxCalls != null ? { max_calls: ent.maxCalls } : {}),
                        period: ent.period,
                        on_exceed: ent.onExceed,
                      },
                    ]),
                  ),
                }
              : {}),
          },
        ]),
      )
    : undefined;

  const billing = config.billing
    ? {
        currency: config.billing.currency,
        subscriptions: Object.fromEntries(
          Object.entries(config.billing.subscriptions).map(([key, offer]) => [
            key,
            {
              plan: offer.plan,
              interval: offer.interval ?? "month",
              interval_count: offer.intervalCount ?? 1,
              grant:
                offer.grant?.mode === "cycle_grant"
                  ? {
                      mode: "cycle_grant",
                      credits: offer.grant.credits,
                      bucket: offer.grant.bucket,
                      replace_prior: offer.grant.replacePrior ?? true,
                    }
                  : (offer.grant ?? { mode: "allowance" }),
              ...(offer.providers && Object.keys(offer.providers).length > 0
                ? { providers: providerRefsToSnake(offer.providers) }
                : {}),
              ...(offer.validFrom != null ? { valid_from: offer.validFrom } : {}),
              ...(offer.validTo != null ? { valid_to: offer.validTo } : {}),
            },
          ]),
        ),
        topups: Object.fromEntries(
          Object.entries(config.billing.topups).map(([key, topup]) => [
            key,
            {
              deposit_to: topup.depositTo,
              credits_per_unit: topup.creditsPerUnit ?? 1000,
              min_amount_minor: topup.minAmountMinor ?? 500,
              max_amount_minor: topup.maxAmountMinor ?? 500_000,
              tax_behavior: topup.taxBehavior ?? "exclude_tax",
              ...(topup.providers && Object.keys(topup.providers).length > 0
                ? { providers: providerRefsToSnake(topup.providers) }
                : {}),
            },
          ]),
        ),
      }
    : undefined;

  return {
    version: config.version,
    metering: {
      models: config.metering.models,
      tools: config.metering.tools,
      search: config.metering.search,
      cache_discount: config.metering.cacheDiscount,
      flat_jobs: Object.fromEntries(
        Object.entries(config.metering.flatJobs).map(([job, cost]) => [job, decToJson(cost)]),
      ),
    },
    ledger: {
      min_balance: decToJson(config.ledger.minBalance),
      ...(config.ledger.signupGrant ? { signup_grant: config.ledger.signupGrant } : {}),
      ...(config.ledger.buckets
        ? {
            buckets: Object.fromEntries(
              Object.entries(config.ledger.buckets).map(([bk, bucket]) => [
                bk,
                {
                  label: bucket.label,
                  priority: bucket.priority,
                  expires: bucket.expires,
                  ...(bucket.ttlDays != null ? { ttl_days: bucket.ttlDays } : {}),
                  ...(bucket.allowOverdraft ? { allow_overdraft: true } : {}),
                  ...(bucket.default ? { default: true } : {}),
                },
              ]),
            ),
          }
        : {}),
    },
    ...(plans ? { plans } : {}),
    ...(billing ? { billing } : {}),
  };
}

/** Validate and return a canonical snake_case config dict for persistence. */
export function canonicalBursarConfigDict(data: BursarConfigData): BursarConfigData {
  const config = loadConfigFromDict(data);
  return bursarConfigToSnakeDict(config);
}

/** Load and validate a pricing config from a raw dictionary. */
export function loadConfigFromDict(data: BursarConfigData): ParsedBursarConfig {
  if (!isRecord(data)) throw new ConfigError("config must be a dict");
  const d = decodeSnakeFields(data, "config");
  assertKnownKeys(d, TOP_LEVEL_KEYS, "config");

  for (const section of ["metering", "ledger", "plans"] as const) {
    if (d[section] != null && !isRecord(d[section])) {
      throw new ConfigError(`${section} must be a dict`);
    }
  }

  const version = (d.version as number | undefined) ?? 1;
  if (version !== 1) {
    throw new ConfigError(`version must be 1, got ${JSON.stringify(d.version)}`);
  }

  const metering = parseMetering((d.metering ?? {}) as Record<string, unknown>);
  const ledger = parseLedger((d.ledger ?? {}) as Record<string, unknown>);
  const buckets = parseBuckets((d.ledger ?? {}) as Record<string, unknown>);
  if (buckets) ledger.buckets = buckets;

  const plans = parsePlans(d as Record<string, unknown>);
  const billing = parseBilling(d.billing);

  const config: ParsedBursarConfig = { version, metering, ledger };
  if (plans) config.plans = plans;
  if (billing) config.billing = billing;

  validateExpressions(config);
  validatePlanReferences(config);
  validateRateOverrideKeys(config);
  validateBucketReferences(config);
  return config;
}
