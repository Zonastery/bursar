export { PricingEngine } from "./engine.js";
export type { AllowancePeriod, FeatureLimitPeriod } from "./allowance.js";
export { resolveAllowanceWindow, resolveCalendarWindow } from "./allowance.js";
export type { CostBreakdown } from "./breakdown.js";
export { makeCostBreakdown } from "./breakdown.js";
export type { ToolCall, UsageMetrics } from "./metrics.js";
export type { PricingConfig } from "./config.js";
export { loadConfigFromDict } from "./config.js";
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

// Manager
export { CreditManager } from "./manager.js";
export type {
  CanAffordOptions,
  CreditManagerOptions,
  LowBalanceConfig,
  PolicyPreset,
  ReserveOptions,
  RunBilledOptions,
  SettleOptions,
} from "./manager.js";

// Types
export type {
  AddCreditsResult,
  AddTeamMemberResult,
  AggregateStats,
  AllowanceResult,
  AvailableResult,
  BalanceResult,
  BillingMode,
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
  PricingConfigData,
  PricingConfigResult,
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
  TierBalance,
  TierBalancesResult,
  TierDefinition,
  TopUserRow,
} from "./types.js";

// Store options
export type { CreateLeaseOptions, SettleLeaseOptions } from "./stores/credit-store.js";

// Stores
export { CreditStore } from "./stores/credit-store.js";
export { HttpxSupabaseStore } from "./stores/supabase-store.js";
export { PostgresStore } from "./stores/postgres-store.js";

// Events
export type { CreditEvent, CreditEventType } from "./stores/events.js";
export { CreditEventEmitter } from "./stores/events.js";
