export { PricingEngine } from "./engine.js";
export type { CostBreakdown } from "./breakdown.js";
export { makeCostBreakdown } from "./breakdown.js";
export type { UsageMetrics } from "./metrics.js";
export type {
  BursarConfigData,
  CatalogRollout,
  ParsedBursarConfig,
  PricingConfig,
  CreditsConfig,
  PlanDefinition,
  PlanEvolution,
  PlanRollout,
  PlanRolloutStrategy,
  CommerceConfig,
  Window,
  Charge,
  FeatureDefinition,
} from "./config.js";
export {
  canonicalBursarConfigDict,
  canonicalCatalogRolloutDict,
  loadCatalogRollout,
  loadConfigFromDict,
  validateCatalogRollout,
} from "./config.js";
export { projectPublicCatalog } from "./catalog.js";
export type {
  PublicCatalog,
  PublicCatalogOffer,
  PublicCatalogPlan,
  PublicCatalogWindow,
} from "./catalog.js";
export {
  BursarError,
  CapabilityNotSupportedError,
  CapReachedError,
  ConcurrencyLimitError,
  CreditError,
  ConfigError,
  ExpressionError,
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
  bursarErrorHttpStatus,
  bursarErrorPublicMessage,
  isRetryableBursarError,
} from "./errors.js";
export type { BursarErrorCategory } from "./errors.js";
export { retryBursarOperation } from "./retry.js";
export type { BursarRetryOptions } from "./retry.js";
export { validateExpression, evaluateExpression } from "./expr.js";

// Application facade. Credit/billing orchestration is internal to Bursar.
export { AccountService, Bursar, CatalogService } from "./bursar.js";
export type {
  AccountCreatedInput,
  AccountCreatedResult,
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
  CheckFeatureResult,
  CreateTeamResult,
  CreditMetadata,
  DailySpendRow,
  DeductionResult,
  DeductWithAllowanceOptions,
  ExecuteGrantProgramRequest,
  GetUserPlanResult,
  GrantProgramAwardResult,
  GrantProgramTrigger,
  LeaseResult,
  LeasePricingContext,
  ListQuotaEventsOptions,
  PlanAllowancePolicy,
  PlanAdmissionPolicy,
  PlanCreditPolicy,
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
  ListUsageChargesOptions,
  ListUsageEntriesOptions,
  LedgerCursor,
  LedgerPage,
  LedgerEntry,
  UsageCharge,
  UsageChargeCursor,
  UsageChargePage,
  UsageRecordResult,
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
