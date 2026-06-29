export { PricingEngine } from "./engine.js";
export type { CostBreakdown } from "./breakdown.js";
export { makeCostBreakdown } from "./breakdown.js";
export type { ToolCall, UsageMetrics } from "./metrics.js";
export type { PricingConfig } from "./config.js";
export { loadConfigFromDict } from "./config.js";
export {
  CapReachedError,
  ConfigError,
  ExpressionError,
  ImportError,
  InsufficientCreditsError,
  PricingNotLoadedError,
  RefundError,
  StoreError,
} from "./errors.js";
export { validateExpression, evaluateExpression } from "./expr.js";

// Manager
export { CreditManager } from "./manager.js";
export type { CreditManagerOptions } from "./manager.js";

// Types
export type {
  AddCreditsResult,
  AddTeamMemberResult,
  AggregateStats,
  AllowanceResult,
  BalanceResult,
  CapCheckResult,
  CheckFeatureResult,
  CreateTeamResult,
  CreditMetadata,
  DailySpendRow,
  DeductionResult,
  DeductWithAllowanceOptions,
  GetUserPlanResult,
  PlanDefinition,
  PricingConfigData,
  PricingConfigResult,
  RefundResult,
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

// Stores
export type { CreditStore } from "./stores/credit-store.js";
export { HttpxSupabaseStore } from "./stores/supabase-store.js";
export { PostgresStore } from "./stores/postgres-store.js";

// Events
export type { CreditEvent, CreditEventType } from "./stores/events.js";
export { CreditEventEmitter } from "./stores/events.js";
