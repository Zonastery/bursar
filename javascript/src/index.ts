export { PricingEngine } from "./engine.js";
export type { AllowancePeriod, FeatureLimitPeriod } from "./allowance.js";
export { resolveAllowanceWindow, resolveCalendarWindow } from "./allowance.js";
export type { CostBreakdown } from "./breakdown.js";
export { makeCostBreakdown } from "./breakdown.js";
export type { UsageMetrics } from "./metrics.js";
export type {
  BursarConfigData,
  ParsedBursarConfig,
  PricingConfig,
  CreditsConfig,
  PlanDefinition as PricingPlanDefinition,
  CommerceConfig,
  Window,
  Charge,
  FeatureDefinition,
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
  OperationNotAllowedError,
  PricingNotLoadedError,
  RefundError,
  QuotaExceededError,
  StoreError,
} from "./errors.js";
export { validateExpression, evaluateExpression } from "./expr.js";

// Application facade. Credit/billing orchestration is internal to Bursar.
export { Bursar, CatalogService } from "./bursar.js";
export type {
  BursarOptions,
  BillingEventSink,
  BillingService,
  CommerceOptions,
  CreditsService,
} from "./bursar.js";
export * from "./commerce/index.js";

// Types
export type {
  AddCreditsResult,
  AddTeamMemberResult,
  AggregateStats,
  AllowanceResult,
  AvailableResult,
  BalanceResult,
  BillingMode,
  BursarConfigHistoryItem,
  BursarConfigResult,
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
  ExecuteGrantProgramRequest,
  FeatureLimit,
  FeatureLimitResult,
  GetUserPlanResult,
  GrantProgramAwardResult,
  GrantProgramTrigger,
  LeaseResult,
  LeasePricingContext,
  ListQuotaEventsOptions,
  MigratePlanUsersResult,
  OperationPolicy,
  PlanAllowancePolicy,
  PlanAdmissionPolicy,
  PlanCreditPolicy,
  PlanDefinition,
  PlanMigrationBatchResult,
  PlanMigrationStartResult,
  QuotaEvent,
  QuotaState,
  RefundResult,
  ReleaseResult,
  SetUserPlanResult,
  SpendByModelRow,
  SpendByUserRow,
  ListLedgerEntriesOptions,
  ListUsageEntriesOptions,
  LedgerCursor,
  LedgerPage,
  LedgerEntry,
  SpendCap,
  SweepResult,
  Team,
  TeamBalanceResult,
  TeamDeductionResult,
  TeamMember,
  TopUserRow,
  UsageAnalyticsStore,
} from "./credits/types/index.js";

// Store options
export type { CreateLeaseOptions, SettleLeaseOptions } from "./credits/store.js";

// Stores
export { CreditStore } from "./credits/store.js";
export { PostgresStore } from "./credits/postgres/store.js";

// Events
export type { CreditEvent, CreditEventType } from "./credits/events.js";
export { CreditEventEmitter } from "./credits/events.js";

// Billing
export { BillingStore, PostgresBillingStore } from "./billing/index.js";
export { AUTO_RECHARGE_STATES, BillingEventType } from "./billing/index.js";

export type {
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
  BillingSubscriptionChange,
  BillingSubscriptionChangeInput,
  BillingSubscriptionOfferContext,
  BillingSubscriptionChangeState,
  BillingSubscriptionState,
  BillingSubscriptionStatus,
  EntitlementMode,
  ProviderRef,
  SubscriptionGrant,
} from "./billing/index.js";
