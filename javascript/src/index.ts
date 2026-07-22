export { PricingEngine } from "./engine.js";
export type { AllowancePeriod, FeatureLimitPeriod } from "./allowance.js";
export { resolveAllowanceWindow, resolveCalendarWindow } from "./allowance.js";
export type { CostBreakdown } from "./breakdown.js";
export { makeCostBreakdown } from "./breakdown.js";
export type { ToolCall, UsageMetrics } from "./metrics.js";
export type {
  BursarConfigData,
  MeteringConfig,
  LedgerConfig,
  SignupGrant,
  BillingSection,
} from "./config.js";
export { loadConfigFromDict, canonicalBursarConfigDict } from "./config.js";
export {
  CapabilityNotSupportedError,
  CapReachedError,
  ConcurrencyLimitError,
  ConfigError,
  ExpressionError,
  FeatureLimitReachedError,
  FeatureNotEntitledError,
  ImportError,
  InsufficientCreditsError,
  LeaseExpiredError,
  LeaseNotFoundError,
  PricingNotLoadedError,
  RefundError,
  StoreError,
} from "./errors.js";
export { validateExpression, evaluateExpression } from "./expr.js";

// Application facade. Credit/billing orchestration is internal to Bursar.
export { Bursar, CatalogService } from "./bursar.js";
export type { BursarOptions, BillingEventSink, BillingService, CreditsService } from "./bursar.js";

// Types
export type {
  AddCreditsResult,
  AddTeamMemberResult,
  AggregateStats,
  AllowanceResult,
  AvailableResult,
  BalanceResult,
  BillingMode,
  BucketBalance,
  BucketBalancesResult,
  BucketDefinition,
  CanAffordResult,
  CapCheckResult,
  CheckFeatureResult,
  CreateTeamResult,
  CreditMetadata,
  DailySpendRow,
  DeductionResult,
  DeductWithAllowanceOptions,
  FeatureLimit,
  FeatureLimitResult,
  GetUserPlanResult,
  LeaseResult,
  OperationPolicy,
  PlanDefinition,
  BursarConfigResult,
  RefundResult,
  ReleaseResult,
  SetUserPlanResult,
  SetupResult,
  SpendByModelRow,
  SpendByUserRow,
  ListTransactionsOptions,
  ListUsageEventsOptions,
  PaginatedTransactions,
  UserTransactionRow,
  SpendCap,
  SweepResult,
  Team,
  TeamBalanceResult,
  TeamDeductionResult,
  TeamMember,
  TopUserRow,
} from "./types.js";

// Store options
export type { CreateLeaseOptions, SettleLeaseOptions } from "./stores/credit-store.js";

// Stores
export { CreditStore } from "./stores/credit-store.js";
export { PostgresStore } from "./stores/postgres-store.js";

// Events
export type { CreditEvent, CreditEventType } from "./stores/events.js";
export { CreditEventEmitter } from "./stores/events.js";

// Billing
export { BillingStore, PostgresBillingStore } from "./billing/index.js";
export { AUTO_RECHARGE_STATES, BillingEventType } from "./billing/index.js";

export type {
  BillingConfig,
  BillingAutoRechargeConfig,
  BillingAutoRechargeAttempt,
  BillingAutoRechargeProfile,
  BillingAutoRechargeStatus,
  BillingProvisioningPort,
  BillingCreditTopup,
  BillingCustomerInfo,
  BillingCustomerRecord,
  BillingDisputeInfo,
  BillingEvent,
  BillingEventClaim,
  BillingEventHandler,
  BillingEventResult,
  BillingInvoiceInfo,
  BillingOffer,
  BillingOfferInterval,
  BillingPaymentInfo,
  BillingPreferences,
  BillingProvider,
  BillingRefundInfo,
  BillingSubscriptionInfo,
  BillingSubscriptionState,
  BillingSubscriptionStatus,
  EntitlementMode,
  ProviderRef,
  SubscriptionGrant,
} from "./billing/index.js";
