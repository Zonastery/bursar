import type { Decimal } from "decimal.js";

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
  /** Shared with bucket priorities; null means legacy allowance-first ordering. */
  priority?: number | null;
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
  allowance: PlanAllowancePolicy | null;
  entitlements: Record<string, { value: unknown }>;
  rateCard?: string | null;
  creditPolicy: PlanCreditPolicy | null;
  admission: PlanAdmissionPolicy | null;
  allowedOperations: string[];
  planAssignedAt?: Date | null;
  assignmentSourceType?: string | null;
  assignmentSourceId?: string | null;
  revisionPolicy?: string | null;
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
