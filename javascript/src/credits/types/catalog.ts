import type { Decimal } from "decimal.js";
import type { AllowancePeriod, FeatureLimitPeriod } from "../../allowance.js";
import type { BillingMode } from "./account.js";

export interface BursarConfigResult {
  id: string;
  config: Record<string, unknown>;
  version: number;
}

export interface BursarConfigHistoryItem {
  id: string;
  version: number;
  label: string | null;
  active: boolean;
  createdAt: string;
}

export interface PlanAllowancePolicy {
  amount: Decimal | null;
  resetUnit: string | null;
  resetCount: number | null;
  resetAnchor: string | null;
  resetTimezone: string | null;
}

export interface PlanCreditPolicy {
  type: "prepaid" | "credit_line";
  creditLimit: Decimal | null;
}

export interface PlanAdmissionPolicy {
  maxInFlight: number | null;
  operations: Record<string, { maxInFlight: number | null }>;
}

export interface OperationPolicy {
  billingMode: BillingMode;
  maxConcurrent?: number | null;
  overdraftFloor?: Decimal | null;
}

/** @deprecated Configure plan quotas in the catalog. */
export interface FeatureLimit {
  value?: unknown;
  maxCalls: number | null;
  period: FeatureLimitPeriod;
  onExceed: "deny" | "warn" | "notify";
}

/** @deprecated Use `PricingPlanDefinition`. */
export interface PlanDefinition {
  label: string;
  tier?: number;
  allowance: { amount: Decimal; period: AllowancePeriod };
  safety: {
    billingMode: BillingMode;
    perOperation?: Record<string, OperationPolicy>;
    maxConcurrent?: number | null;
    overdraftFloor?: Decimal | null;
  };
  rateCard?: string | null;
  entitlements?: Record<string, { value?: unknown }> | null;
}

export interface AllowanceResult {
  planId: string;
  allowanceRemaining: Decimal;
  periodStart: string;
  periodEnd: string;
}

export interface GetUserPlanResult {
  userId: string;
  planId: string | null;
  planKey: string | null;
  planLabel: string | null;
  allowanceAmount: Decimal;
  allowance: PlanAllowancePolicy | null;
  allowancePeriod: AllowancePeriod | null;
  entitlements: Record<string, { value: unknown }>;
  rateCard?: string | null;
  billingMode: BillingMode;
  creditPolicy: PlanCreditPolicy | null;
  admission: PlanAdmissionPolicy | null;
  allowedOperations: string[];
  perOperation?: Record<string, OperationPolicy>;
  maxConcurrent?: number | null;
  overdraftFloor?: Decimal | null;
  planAssignedAt?: Date | null;
  assignmentSourceType?: string | null;
  assignmentSourceId?: string | null;
  revisionPolicy?: string | null;
  configVersion?: number | null;
  catalogVersion?: number | null;
}

export interface SetUserPlanResult {
  userId: string;
  planId: string;
  planAssignedAt?: string | null;
}

export interface PlanMigrationStartResult {
  migrationId: string;
}

export interface PlanMigrationBatchResult {
  migrated: number;
  done: boolean;
  nextCursor: string | null;
}

/** @deprecated Prefer resumable plan migrations. */
export interface MigratePlanUsersResult {
  planKey: string;
  targetPlanId: string;
  targetConfigVersion: number;
  migratedCount: number;
}
