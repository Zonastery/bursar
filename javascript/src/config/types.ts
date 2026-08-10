import type Decimal from "decimal.js";
import type { BursarConfig as GeneratedBursarConfigData } from "../generated/pricing-config.types.js";

/** Raw, snake_case configuration accepted by the canonical JSON Schema. */
export type BursarConfigData = GeneratedBursarConfigData;
export type FeatureValue = boolean | number | string;

export interface Duration {
  unit: "second" | "minute" | "hour" | "day" | "week";
  count: number;
}

export interface BillingInterval {
  unit: "day" | "week" | "month" | "year";
  count: number;
}

export type Window =
  | {
      type: "calendar";
      unit: "day" | "week" | "month" | "year";
      count: number;
      timezone: string;
    }
  | { type: "rolling"; duration: Duration }
  | { type: "plan_assignment"; interval: BillingInterval; timezone: string };

export interface Availability {
  startsAt?: string;
  endsAt?: string;
  regions: string[];
}

export type ExpiryPolicy =
  | { type: "never" }
  | { type: "after_grant"; interval: BillingInterval; timezone: string }
  | { type: "end_of_window"; window: Exclude<Window, { type: "rolling" }> }
  | { type: "fixed_at"; at: string }
  | { type: "subscription_end" };

export interface MeasureDefinition {
  unit: string;
}

export interface DimensionDefinition {
  type: "string" | "number" | "boolean";
  required: boolean;
}

export type MatcherScalar = string | Decimal | boolean;
export type DimensionMatcher =
  | { op: "eq"; value: MatcherScalar }
  | { op: "in" | "not_in"; values: MatcherScalar[] }
  | { op: "prefix"; value: string }
  | { op: "range"; gt?: Decimal; gte?: Decimal; lt?: Decimal; lte?: Decimal };

export interface GraduatedTier {
  upTo?: Decimal;
  rate: Decimal;
}

export type Charge =
  | { type: "flat"; amount: Decimal }
  | { type: "per_unit"; measure: string; rate: Decimal; unitSize: Decimal }
  | {
      type: "package";
      measure: string;
      units: Decimal;
      amount: Decimal;
      rounding: "ceil" | "floor" | "nearest";
    }
  | { type: "graduated" | "volume"; measure: string; tiers: GraduatedTier[] }
  | { type: "expression"; formula: string }
  | { type: "sum"; components: Charge[] };

export interface PriceRule {
  when: Record<string, DimensionMatcher>;
  charge: Charge;
}

export interface OperationPricing {
  rules: PriceRule[];
  unmatched: { action: "reject" } | { action: "charge"; charge: Charge };
}

export interface OperationDefinition {
  measures: Record<string, MeasureDefinition>;
  dimensions: Record<string, DimensionDefinition>;
}

export interface RateCard {
  extends?: string;
  operations: Record<string, OperationPricing>;
}

export interface PricingConfig {
  operations: Record<string, OperationDefinition>;
  rateCards: Record<string, RateCard>;
}

export interface BucketDefinition {
  priority: number;
  expiry: ExpiryPolicy;
}

export type CreditPolicy = { type: "prepaid" } | { type: "credit_line"; limit: Decimal };

export interface GrantProgram {
  trigger: "account_created" | "referral_completed" | "promo_code_redeemed" | "manual";
  awards: Array<{
    recipient: "subject" | "referrer";
    amount: Decimal;
    bucket: string;
    expiry?: ExpiryPolicy;
  }>;
  availability?: Availability;
  eligibility: { plans: string[]; regions: string[] };
  maxAwardsPerSubject: number;
  idempotencyScope: "subject" | "event";
}

export interface CreditsConfig {
  buckets: Record<string, BucketDefinition>;
  defaultBucket?: string;
  policies: Record<string, CreditPolicy>;
  grantPrograms: Record<string, GrantProgram>;
  display?: {
    currency: string;
    unitsPerMajor: Decimal;
  };
}

export type FeatureDefinition =
  | { type: "boolean"; default: boolean }
  | { type: "enum"; values: string[]; default: string }
  | { type: "integer"; default: number; minimum?: number; maximum?: number }
  | { type: "string"; default: string; pattern?: string };

export interface EntitlementsConfig {
  features: Record<string, FeatureDefinition>;
}

export interface AdmissionPolicy {
  maxInFlight?: number;
  operations: Record<string, { maxInFlight: number }>;
}

export interface CreditAllowance {
  amount: Decimal;
  /** Shared with bucket priorities. Lower values are spent first. */
  priority: number;
  window: Window;
}

export interface QuotaDefinition {
  operation: string;
  measure: string;
  limit: Decimal;
  window: Window;
  enforcement: "block" | "allow";
  emitAtPercent: number[];
}

export type PlanRolloutStrategy = "immediate" | "next_renewal" | "new_assignments_only";

export interface PlanEvolution {
  defaultRollout: PlanRolloutStrategy;
}

export interface PlanRollout {
  effective: PlanRolloutStrategy;
  includePinned: boolean;
}

export interface CatalogRollout {
  plans: Record<string, PlanRollout>;
}

export interface PlanDefinition {
  displayName: string;
  /** Explicit commercial ordering; never inferred from declaration order. */
  rank: number;
  description?: string;
  rateCard?: string;
  allowedOperations: string[];
  features: Record<string, FeatureValue>;
  creditAllowance?: CreditAllowance;
  quotas: Record<string, QuotaDefinition>;
  creditPolicy?: string;
  admissionPolicy?: string;
  evolution: PlanEvolution;
}

export type ProviderDefinition =
  { type: "stripe" } | { type: "dodo" } | { type: "custom"; adapter: string };

export type ProviderReference =
  | { type: "stripe_price"; priceId: string }
  | { type: "dodo_product"; productId: string }
  | { type: "custom_object"; objectKind: "subscription" | "one_time"; externalId: string };

export interface OfferPrice {
  amountMinor: number;
  currency: string;
  taxBehavior: "inclusive" | "exclusive" | "unspecified";
}

export interface OfferCommon {
  displayName: string;
  description?: string;
  sortOrder: number;
  availability?: Availability;
  price: OfferPrice;
  providers: Record<string, ProviderReference>;
}

export interface SubscriptionOffer extends OfferCommon {
  type: "subscription";
  plan: string;
  billingInterval: BillingInterval;
  trial?: BillingInterval;
  cycleGrant?: {
    amount: Decimal;
    bucket: string;
    renewal: "replace_previous" | "accumulate";
    expiry: ExpiryPolicy;
  };
}

export interface TopupOffer extends OfferCommon {
  type: "topup";
  creditsPerUnit: Decimal;
  quantity: { minimum: number; maximum: number; default: number };
  bucket: string;
  expiry?: ExpiryPolicy;
  lotBehavior: "separate_lots" | "merge_and_refresh";
}

export type CommerceOffer = SubscriptionOffer | TopupOffer;

export interface AutoRechargeGuardrails {
  eligibleTopups: string[];
  balanceBelow: { minimum: Decimal; maximum: Decimal; default: Decimal };
  rearmAbove: Decimal;
  quantity: { minimum: number; maximum: number; default: number };
  limits: {
    maxPurchases: number;
    window: Extract<Window, { type: "calendar" | "rolling" }>;
    maxChargeMinor: number;
    cooldown: Duration;
    maxConsecutiveFailures: number;
    failureAction: "pause";
  };
}

export interface CommerceConfig {
  providers: Record<string, ProviderDefinition>;
  offers: Record<string, CommerceOffer>;
  subscriptionChanges?: Partial<Record<SubscriptionChangeClassification, SubscriptionChangePolicy>>;
  autoRecharge?: AutoRechargeGuardrails;
}

export type SubscriptionChangeClassification =
  "upgrade" | "downgrade" | "lateral" | "cadence_change";

export interface SubscriptionChangePolicy {
  effective: "immediate" | "renewal";
  proration: "prorated" | "none";
  paymentFailure: "prevent_change" | "apply_change";
}

export interface ParsedBursarConfig {
  version: 1;
  catalog: { defaultPlan?: string };
  pricing?: PricingConfig;
  credits: CreditsConfig;
  entitlements: EntitlementsConfig;
  admission: { policies: Record<string, AdmissionPolicy> };
  plans: Record<string, PlanDefinition>;
  commerce: CommerceConfig;
}
