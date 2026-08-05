import { z } from "zod";
import type { CallProc } from "../../../shared/postgres-types.js";
import { safeParse } from "../../../shared/postgres-validation.js";

const UserPlanRowSchema = z
  .object({
    user_id: z.string().optional(),
    plan_id: z.string().nullable().optional(),
    plan_key: z.string().nullable().optional(),
    plan_label: z.string().nullable().optional(),
    rate_card: z.string().nullable().optional(),
    credit_allowance_amount: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
    credit_allowance_priority: z.coerce.number().int().nonnegative().nullable().optional(),
    credit_allowance_reset_unit: z.string().nullable().optional(),
    credit_allowance_reset_count: z.coerce.number().nullable().optional(),
    credit_allowance_reset_anchor: z.string().nullable().optional(),
    credit_allowance_reset_timezone: z.string().nullable().optional(),
    entitlements: z.record(z.string(), z.unknown()).optional(),
    credit_policy_type: z.string().nullable().optional(),
    credit_limit: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
    admission_max_in_flight: z.coerce.number().nullable().optional(),
    operation_admission: z.record(z.string(), z.unknown()).optional(),
    allowed_operations: z.array(z.string()).optional(),
    assignment_source_type: z.string().nullable().optional(),
    assignment_source_id: z.string().nullable().optional(),
    catalog_revision_pinned: z.boolean().optional(),
    plan_assigned_at: z
      .union([z.string(), z.date().transform((value) => value.toISOString())])
      .nullable()
      .optional(),
    catalog_revision_no: z.coerce.number().nullable().optional(),
  })
  .passthrough();

const SetUserPlanRowSchema = z
  .object({
    user_id: z.string().optional(),
    plan_id: z.string().optional(),
    plan_assigned_at: z
      .union([z.string(), z.date().transform((value) => value.toISOString())])
      .nullable()
      .optional(),
  })
  .passthrough();

const AllowanceRowSchema = z
  .object({
    plan_id: z.string().nullable().optional(),
    allowance_remaining: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
    period_start: z
      .union([z.string(), z.date().transform((value) => value.toISOString())])
      .nullable()
      .optional(),
    period_end: z
      .union([z.string(), z.date().transform((value) => value.toISOString())])
      .nullable()
      .optional(),
  })
  .passthrough();

const UnsetPlanRowSchema = z
  .object({
    user_id: z.string().optional(),
  })
  .passthrough();

const PlanMigrationStartRowSchema = z.string().nullable();
const PlanMigrationBatchRowSchema = z
  .object({
    migrated: z.number(),
    done: z.boolean(),
    next_cursor: z.string().nullable(),
  })
  .passthrough();
const decimal = z.union([z.string(), z.number()] as const);
const QuotaStateRowSchema = z
  .object({
    user_id: z.string(),
    quota_key: z.string(),
    operation_key: z.string(),
    measure_key: z.string(),
    quota_limit: decimal,
    consumed: decimal,
    reserved: decimal,
    remaining: decimal,
    overage: decimal,
    enforcement: z.enum(["block", "allow"]),
    window_start: z.union([z.string(), z.date().transform((value) => value.toISOString())]),
    window_end: z.union([z.string(), z.date().transform((value) => value.toISOString())]),
    emit_at_percent: z.array(z.number()),
  })
  .passthrough();
const QuotaEventRowSchema = z
  .object({
    event_id: z.string(),
    quota_key: z.string(),
    operation_key: z.string(),
    measure_key: z.string(),
    event_type: z.enum(["threshold", "blocked"]),
    threshold_percent: z.coerce.number().nullable(),
    idempotency_key: z.string(),
    usage_charge_id: z.string().nullable(),
    created_at: z.union([z.string(), z.date().transform((value) => value.toISOString())]),
  })
  .passthrough();

export type UnsetPlanRow = z.infer<typeof UnsetPlanRowSchema>;
export type UserPlanRow = z.infer<typeof UserPlanRowSchema>;
export type SetUserPlanRow = z.infer<typeof SetUserPlanRowSchema>;
export type AllowanceRow = z.infer<typeof AllowanceRowSchema>;
export type PlanMigrationStartRow = z.infer<typeof PlanMigrationStartRowSchema>;
export type PlanMigrationBatchRow = z.infer<typeof PlanMigrationBatchRowSchema>;
export type QuotaStateRow = z.infer<typeof QuotaStateRowSchema>;
export type QuotaEventRow = z.infer<typeof QuotaEventRowSchema>;

/** Repository for plan management operations. */
export class PlanRepository {
  constructor(private callproc: CallProc) {}

  /** Fetch a user's current plan. Returns null if user has no plan. */
  async getUserPlan(userId: string): Promise<UserPlanRow | null> {
    const rows = await this.callproc("get_subject_plan", [userId]);
    if (!rows || rows.length === 0) return null;
    return safeParse(UserPlanRowSchema, rows[0], "PlanRepository.getUserPlan");
  }

  async getEntitlement(userId: string, feature: string): Promise<Record<string, unknown> | null> {
    const rows = await this.callproc("get_subject_entitlements", [userId]);
    const row = (rows as Array<Record<string, unknown>>).find(
      (candidate) => candidate.feature_key === feature,
    );
    return row ?? null;
  }

  /** Assign a plan to a user. */
  async setUserPlan(
    userId: string,
    planKey: string,
    planAssignedAt: string | null,
  ): Promise<SetUserPlanRow> {
    const planRows = await this.callproc("resolve_active_plan", [planKey]);
    const planRow = planRows[0] as Record<string, unknown> | undefined;
    if (planRow?.id == null) throw new Error(`unknown active plan '${planKey}'`);
    const planId = String(planRow.id);
    const params = planAssignedAt ? [userId, planId, planAssignedAt] : [userId, planId];
    const rows = await this.callproc("assign_plan", params);
    if (rows?.[0] !== true) {
      throw new Error("PlanRepository.setUserPlan: assign_plan returned false");
    }
    const assigned = await this.getUserPlan(userId);
    return safeParse(
      SetUserPlanRowSchema,
      {
        user_id: userId,
        plan_id: assigned?.plan_id ?? planId,
        plan_assigned_at: assigned?.plan_assigned_at ?? planAssignedAt,
      },
      "PlanRepository.setUserPlan",
    );
  }

  /** Remove a user's plan assignment. */
  async unsetUserPlan(userId: string): Promise<UnsetPlanRow> {
    const rows = await this.callproc("unassign_plan", [userId, "sdk_unassignment"]);
    if (rows?.[0] !== true) {
      throw new Error("PlanRepository.unsetUserPlan: unassign_plan returned false");
    }
    return safeParse(UnsetPlanRowSchema, { user_id: userId }, "PlanRepository.unsetUserPlan");
  }

  /** Pin or unpin the user's current assignment to its catalog revision. */
  async setPlanRevisionPin(userId: string, pinned: boolean): Promise<boolean> {
    const rows = await this.callproc("set_plan_revision_pin", [userId, pinned]);
    return rows?.[0] === true;
  }

  /** Apply one bounded batch of renewal-effective plan changes. */
  async applyDuePlanChanges(limit: number): Promise<number> {
    const rows = await this.callproc("apply_due_plan_assignment_changes", [limit]);
    return Number(rows?.[0] ?? 0);
  }

  /** Check a user's remaining free allowance. */
  async startPlanMigration(
    fromPlanId: string | null,
    toPlanId: string,
  ): Promise<PlanMigrationStartRow> {
    const rows = await this.callproc("start_plan_migration", [fromPlanId, toPlanId]);
    return safeParse(
      PlanMigrationStartRowSchema,
      rows?.[0] ?? null,
      "PlanRepository.startPlanMigration",
    );
  }

  async migratePlanBatch(migrationId: string, batchSize?: number): Promise<PlanMigrationBatchRow> {
    const rows = await this.callproc("migrate_plan_batch", [migrationId, batchSize ?? 100]);
    return safeParse(
      PlanMigrationBatchRowSchema,
      rows?.[0] ?? {},
      "PlanRepository.migratePlanBatch",
    );
  }

  async getQuotaState(userId: string, quotaKey?: string | null): Promise<QuotaStateRow[]> {
    const rows = await this.callproc("get_subject_quota_state", [userId, quotaKey ?? null]);
    return (rows ?? []).map((row) =>
      safeParse(QuotaStateRowSchema, row, "PlanRepository.getQuotaState"),
    );
  }

  async listQuotaEvents(
    userId: string,
    after: string | null,
    limit: number,
    idempotencyKey: string | null,
    afterId: string | null,
  ): Promise<QuotaEventRow[]> {
    const rows = await this.callproc("list_subject_quota_events", [
      userId,
      after,
      limit,
      idempotencyKey,
      afterId,
    ]);
    return (rows ?? []).map((row) =>
      safeParse(QuotaEventRowSchema, row, "PlanRepository.listQuotaEvents"),
    );
  }

  async checkAllowance(userId: string): Promise<AllowanceRow | null> {
    const rows = await this.callproc("get_subject_allowance", [userId]);
    if (!rows || rows.length === 0) return null;
    return safeParse(AllowanceRowSchema, rows[0], "PlanRepository.checkAllowance");
  }
}
