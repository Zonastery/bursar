import { z } from "zod";
import type { CallProc } from "./types.js";
import { pgBoolean, safeParse } from "./_shared.js";

export const UserPlanRowSchema = z
  .object({
    user_id: z.string().optional(),
    plan_id: z.string().nullable().optional(),
    plan_label: z.string().nullable().optional(),
    allowance_amount: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
    allowance_period: z.string().optional(),
    entitlements: z.record(z.string(), z.unknown()).optional(),
    billing_mode: z.string().optional(),
    per_operation: z.record(z.string(), z.unknown()).optional(),
    max_concurrent: z.number().nullable().optional(),
    overdraft_floor: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
    plan_assigned_at: z.string().nullable().optional(),
    config_version: z.number().nullable().optional(),
  })
  .passthrough();

export const SetUserPlanRowSchema = z
  .object({
    user_id: z.string().optional(),
    plan_id: z.string().optional(),
    plan_assigned_at: z.string().nullable().optional(),
  })
  .passthrough();

export const MigratePlanRowSchema = z
  .object({
    plan_key: z.string().optional(),
    target_plan_id: z.string().optional(),
    target_config_version: z.number().optional(),
    migrated_count: z.number().optional(),
  })
  .passthrough();

export const AllowanceRowSchema = z
  .object({
    plan_id: z.string().nullable().optional(),
    allowance_remaining: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
    period_start: z.string().nullable().optional(),
    period_end: z.string().nullable().optional(),
  })
  .passthrough();

export const FeatureLimitRowSchema = z
  .object({
    user_id: z.string().optional(),
    feature: z.string().optional(),
    limited: pgBoolean.nullable().optional(),
    limit: z.number().optional(),
    used: z.number().optional(),
    remaining: z.number().optional(),
    period_start: z.string().optional(),
    period_end: z.string().optional(),
    action: z.string().nullable().optional(),
  })
  .passthrough();

export const CapCheckRowSchema = z
  .object({
    capped: pgBoolean.nullable().optional(),
    current_spend: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
    cap_limit: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
    action: z.string().nullable().optional(),
    model: z.string().nullable().optional(),
  })
  .passthrough();

export const UnsetPlanRowSchema = z
  .object({
    user_id: z.string().optional(),
  })
  .passthrough();

export type UnsetPlanRow = z.infer<typeof UnsetPlanRowSchema>;
export type UserPlanRow = z.infer<typeof UserPlanRowSchema>;
export type SetUserPlanRow = z.infer<typeof SetUserPlanRowSchema>;
export type MigratePlanRow = z.infer<typeof MigratePlanRowSchema>;
export type AllowanceRow = z.infer<typeof AllowanceRowSchema>;
export type FeatureLimitRow = z.infer<typeof FeatureLimitRowSchema>;
export type CapCheckRow = z.infer<typeof CapCheckRowSchema>;

/** Repository for plan management operations. */
export class PlanRepository {
  constructor(private callproc: CallProc) {}

  /** Fetch a user's current plan. Returns null if user has no plan. */
  async getUserPlan(userId: string): Promise<UserPlanRow | null> {
    const rows = await this.callproc("get_user_plan", [userId]);
    if (!rows || rows.length === 0) return null;
    return safeParse(UserPlanRowSchema, rows[0], "PlanRepository.getUserPlan");
  }

  /** Assign a plan to a user. */
  async setUserPlan(
    userId: string,
    planId: string,
    planAssignedAt: string | null,
  ): Promise<SetUserPlanRow> {
    const params = planAssignedAt ? [userId, planId, planAssignedAt] : [userId, planId];
    const rows = await this.callproc("set_user_plan", params);
    return safeParse(SetUserPlanRowSchema, rows?.[0] ?? {}, "PlanRepository.setUserPlan");
  }

  /** Remove a user's plan assignment. */
  async unsetUserPlan(userId: string): Promise<UnsetPlanRow> {
    const rows = await this.callproc("unset_user_plan", [userId]);
    return safeParse(UnsetPlanRowSchema, rows?.[0] ?? {}, "PlanRepository.unsetUserPlan");
  }

  /** Migrate all users on a plan_key to a target config version. */
  async migratePlanUsers(
    planKey: string,
    targetConfigVersion: number | null,
  ): Promise<MigratePlanRow> {
    const rows = await this.callproc("migrate_plan_users", [planKey, targetConfigVersion]);
    return safeParse(MigratePlanRowSchema, rows?.[0] ?? {}, "PlanRepository.migratePlanUsers");
  }

  /** Check a user's remaining free allowance. */
  async checkAllowance(userId: string, periodStart: string | null): Promise<AllowanceRow | null> {
    const rows = await this.callproc("check_plan_allowance", [userId, periodStart]);
    if (!rows || rows.length === 0) return null;
    return safeParse(AllowanceRowSchema, rows[0], "PlanRepository.checkAllowance");
  }

  /** Increment a user's usage window. */
  async incrementUsageWindow(userId: string, planId: string, amount: string): Promise<void> {
    await this.callproc("increment_usage_window", [userId, planId, amount]);
  }

  /** Check a per-feature invocation limit. */
  async checkFeatureLimit(
    userId: string,
    feature: string,
    maxCalls: number,
    periodStart: string,
    periodEnd: string,
  ): Promise<FeatureLimitRow | null> {
    const rows = await this.callproc("check_feature_limit", [
      userId,
      feature,
      maxCalls,
      periodStart,
      periodEnd,
    ]);
    if (!rows || rows.length === 0) return null;
    return safeParse(FeatureLimitRowSchema, rows[0], "PlanRepository.checkFeatureLimit");
  }

  /** Check a user's spend cap. */
  async checkSpendCap(
    userId: string,
    model: string | null,
    amount: string,
  ): Promise<CapCheckRow | null> {
    const rows = await this.callproc("check_spend_cap", [userId, model, amount]);
    if (!rows || rows.length === 0) return null;
    return safeParse(CapCheckRowSchema, rows[0], "PlanRepository.checkSpendCap");
  }
}
