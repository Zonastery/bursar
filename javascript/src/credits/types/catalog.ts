import type { Decimal } from "decimal.js";

export interface CatalogRevision {
  id: string;
  config: Record<string, unknown>;
  version: number;
}

export interface CatalogRevisionSummary {
  id: string;
  version: number;
  label: string | null;
  active: boolean;
  createdAt: string;
}

export interface PlanAllowancePolicy {
  amount: Decimal;
  priority: number;
  resetUnit: "second" | "minute" | "hour" | "day" | "week" | "month" | "year";
  resetCount: number;
  resetAnchor: "calendar" | "plan_assignment" | "rolling";
  resetTimezone: string;
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
  rateCard: string | null;
  creditPolicy: PlanCreditPolicy | null;
  admission: PlanAdmissionPolicy | null;
  allowedOperations: string[];
  planAssignedAt: Date | null;
  planAssignmentEndsAt: Date | null;
  assignmentSourceType: "manual" | "subscription" | "migration" | "system" | null;
  assignmentSourceId: string | null;
  catalogRevisionPinned: boolean;
  catalogVersion: number | null;
}

export interface SetUserPlanResult {
  userId: string;
  planId: string;
  planKey: string;
  planAssignedAt: Date;
  assignmentState: "applied" | "scheduled";
}

export interface UnsetUserPlanResult {
  userId: string;
}

export interface PlanMigrationStartResult {
  migrationId: string;
}

export interface PlanMigrationBatchResult {
  migrated: number;
  done: boolean;
  nextCursor: string | null;
}
