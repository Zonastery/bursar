export { PricingEngine } from "./engine.js";
export type { CostBreakdown } from "./breakdown.js";
export { makeCostBreakdown } from "./breakdown.js";
export type { ToolCall, UsageMetrics } from "./metrics.js";
export type { PricingConfig } from "./config.js";
export { loadConfigFromDict } from "./config.js";
export {
  CapReachedError,
  ConcurrencyLimitError,
  ConfigError,
  ExpressionError,
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
  GetUserPlanResult,
  LeaseResult,
  OperationPolicy,
  PlanDefinition,
  PricingConfigData,
  PricingConfigResult,
  RefundResult,
  ReleaseResult,
  ReserveResult,
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
export type { CreditStore } from "./stores/credit-store.js";
export { HttpxSupabaseStore } from "./stores/supabase-store.js";
export { PostgresStore } from "./stores/postgres-store.js";

// Events
export type { CreditEvent, CreditEventType } from "./stores/events.js";
export { CreditEventEmitter } from "./stores/events.js";
