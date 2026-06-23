export { PricingEngine } from "./engine.js";
export type { CostBreakdown } from "./breakdown.js";
export { makeCostBreakdown } from "./breakdown.js";
export type { ToolCall, UsageMetrics } from "./metrics.js";
export type { PricingConfig } from "./config.js";
export { loadConfigFromDict } from "./config.js";
export {
  ConfigError,
  ExpressionError,
  ImportError,
  InsufficientCreditsError,
  PricingNotLoadedError,
} from "./errors.js";
export { validateExpression, evaluateExpression } from "./expr.js";

// Manager
export { CreditManager } from "./manager.js";

// Types
export type {
  AllowanceResult,
  CreditMetadata,
  GetUserPlanResult,
  PlanDefinition,
  PricingConfigData,
  PricingConfigV2,
  PricingConfigResult,
  SetUserPlanResult,
  BalanceResult,
  AddCreditsResult,
  ReserveResult,
  DeductionResult,
  SetupResult,
} from "./types.js";

// Stores
export type { CreditStore } from "./stores/credit-store.js";
export { MemoryStore } from "./stores/memory-store.js";
export { HttpxSupabaseStore } from "./stores/supabase-store.js";
export { PostgresStore } from "./stores/postgres-store.js";

// Utilities
export { loadPricingFile } from "./load-pricing-file.js";
