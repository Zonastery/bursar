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
  AutoRechargeDisabledError,
  AutoRechargeNotConfiguredError,
  BillingError,
  BursarError,
  CapabilityNotConfiguredError,
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
  PaymentMethodRequiredError,
  ProviderCapabilityNotSupportedError,
  ProviderResponseError,
  CatalogNotLoadedError,
  RefundError,
  QuotaExceededError,
  StoreError,
  StoreClosedError,
  StoreTimeoutError,
  StoreUnavailableError,
  bursarErrorHttpStatus,
  bursarErrorPublicMessage,
  isBursarError,
  isRetryableBursarError,
} from "./errors.js";
export type {
  BursarErrorCategory,
  BursarErrorDetails,
  BursarErrorOptions,
  SerializedBursarError,
  StoreErrorOptions,
} from "./errors.js";
export { retryBursarOperation } from "./retry.js";
export type { BursarRetryOptions } from "./retry.js";
export { validateExpression, evaluateExpression } from "./expr.js";
export {
  BURSAR_INSTRUMENTATION_SCOPE,
  BURSAR_INSTRUMENTATION_VERSION,
  NOOP_INSTRUMENTATION,
  NoopInstrumentation,
  getDefaultInstrumentation,
  sanitizeTelemetryAttributes,
  setDefaultInstrumentation,
  telemetryErrorAttributes,
  telemetryOperationAttributes,
} from "./telemetry/index.js";
export type {
  Instrumentation,
  TelemetryAttributes,
  TelemetryAttributeValue,
} from "./telemetry/index.js";

// Application facade. Credit/billing orchestration is internal to Bursar.
export { AccountService, Bursar, CatalogService } from "./bursar.js";
export type {
  AccountCreatedInput,
  AccountCreatedResult,
  BillingBursarOptions,
  BursarOptions,
  BillingCapability,
  BillingEventSink,
  CommerceOptions,
  CreditsService,
  CreditsOnlyBursarOptions,
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
  CatalogRevisionSummary,
  CatalogRevision,
  BucketBalance,
  BucketBalancesResult,
  BucketDefinition,
  CanAffordResult,
  CheckFeatureResult,
  CreateTeamResult,
  CreditMetadata,
  DailySpendRow,
  DeductionResult,
  DeductionFailure,
  DeductionSuccess,
  DeductWithAllowanceOptions,
  ExecuteGrantProgramRequest,
  GetUserPlanResult,
  GrantProgramAwardResult,
  GrantProgramTrigger,
  LeaseResult,
  LeaseFailure,
  LeaseSuccess,
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
  RefundFailure,
  RefundSuccess,
  RevokeCreditsResult,
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
  UsageRecordFailure,
  UsageRecordSuccess,
  SweepResult,
  Team,
  TeamBalanceResult,
  TeamDeductionResult,
  TeamDeductionFailure,
  TeamDeductionSuccess,
  TeamMember,
  TopUserRow,
  UsageAnalyticsStore,
} from "./credits/types/index.js";

// Store options
export type { CreateLeaseOptions, CreateTeamOptions, SettleLeaseOptions } from "./credits/store.js";
export type {
  AddCreditsOptions,
  BeginBilledOperationOptions,
  CanAffordOptions,
  CreditsServiceOptions,
  DeductCreditsOptions,
  DeductFlatJobOptions,
  DeductOptions,
  DeductTeamOptions,
  ExactAmount,
  GrantSubscriptionCycleOptions,
  LowBalanceConfig,
  MetricsOrAmount,
  PostDeductionContext,
  RecordUsageOptions,
  RefundCreditsOptions,
  ReserveOptions,
  RunBilledOptions,
  SettleOptions,
} from "./credits/service-types.js";

// Stores
export { CreditStore } from "./credits/store.js";
export { PostgresStore } from "./credits/postgres/store.js";
export type { PostgresStoreOptions } from "./credits/postgres/store.js";
export type { PostgresConnectionOptions } from "./shared/postgres-client.js";
export { PROVIDER_ENVIRONMENTS } from "./providers/environment.js";
export type { ProviderEnvironment } from "./providers/environment.js";

// Events
export type { CreditEvent, CreditEventType } from "./credits/events.js";
export { CreditEventEmitter } from "./credits/events.js";

// Billing
export { BillingStore, PostgresBillingStore } from "./billing/index.js";
export { AUTO_RECHARGE_STATES, BillingEventType } from "./billing/index.js";
export type { BillingServiceOptions } from "./billing/billing-service.js";

export type {
  BillingAutoRechargeAttempt,
  BillingAutoRechargeAttemptState,
  BillingAutoRechargeProfile,
  BillingAutoRechargeStatus,
  BillingProvisioningPort,
  BillingCreditPostingResult,
  BillingCustomerInfo,
  BillingCustomerRecord,
  BillingDisputeInfo,
  BillingEvent,
  BillingEventClaim,
  BillingEventHandler,
  BillingEventResult,
  BillingInvoiceInfo,
  BillingInvoiceRecord,
  BillingOfferInterval,
  BillingPaymentInfo,
  BillingPaymentRecord,
  BillingPreferences,
  PostgresBillingStoreOptions,
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
