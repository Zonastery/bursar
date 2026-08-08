import { z } from "zod";
import type { CallProc } from "../../../shared/postgres-types.js";
import {
  optionalRecordRow,
  postgresUuid,
  requireRow,
  safeParse,
} from "../../../shared/postgres-validation.js";
import { StoreError } from "../../../errors.js";

const decimal = z.union([z.string().min(1), z.number().finite()] as const);
const timestamp = z.union([z.string().min(1), z.date()] as const).transform((value, context) => {
  const parsed = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    context.addIssue({ code: "custom", message: "expected a valid timestamp" });
    return z.NEVER;
  }
  return parsed;
});
const safeInteger = z
  .union([z.number().int(), z.string().regex(/^\d+$/)] as const)
  .transform(Number)
  .refine(Number.isSafeInteger, "expected a safe integer");
const entitlementValue = z.object({ value: z.json() }).strict();
const admissionOperation = z
  .object({ max_in_flight: z.number().int().positive().nullable() })
  .strict();

const UserPlanRowSchema = z
  .object({
    user_id: postgresUuid,
    plan_assigned_at: timestamp,
    plan_assignment_ends_at: timestamp.nullable(),
    assignment_source_type: z.enum(["manual", "subscription", "migration", "system"]),
    assignment_source_id: postgresUuid.nullable(),
    catalog_revision_pinned: z.boolean(),
    plan_id: postgresUuid,
    plan_key: z.string().min(1),
    plan_label: z.string().min(1),
    rate_card: z.string().min(1).nullable(),
    allowed_operations: z.array(z.string().min(1)),
    credit_allowance_amount: decimal.nullable(),
    credit_allowance_priority: z.number().int().nonnegative().nullable(),
    credit_allowance_reset_unit: z
      .enum(["second", "minute", "hour", "day", "week", "month", "year"])
      .nullable(),
    credit_allowance_reset_count: z.number().int().positive().nullable(),
    credit_allowance_reset_anchor: z.enum(["calendar", "plan_assignment", "rolling"]).nullable(),
    credit_allowance_reset_timezone: z.string().min(1).nullable(),
    entitlements: z.record(z.string().min(1), entitlementValue),
    credit_policy_type: z.enum(["prepaid", "credit_line"]).nullable(),
    credit_limit: decimal.nullable(),
    admission_max_in_flight: z.number().int().positive().nullable(),
    operation_admission: z.record(z.string().min(1), admissionOperation),
    catalog_revision_no: safeInteger,
  })
  .strict()
  .superRefine((row, context) => {
    const allowance = [
      row.credit_allowance_amount,
      row.credit_allowance_priority,
      row.credit_allowance_reset_unit,
      row.credit_allowance_reset_count,
      row.credit_allowance_reset_anchor,
      row.credit_allowance_reset_timezone,
    ];
    const populated = allowance.filter((value) => value !== null).length;
    if (populated !== 0 && populated !== allowance.length) {
      context.addIssue({
        code: "custom",
        message: "allowance policy fields must be all set or all null",
      });
    }
    if (
      (row.credit_policy_type === null && row.credit_limit !== null) ||
      (row.credit_policy_type === "prepaid" && row.credit_limit !== null) ||
      (row.credit_policy_type === "credit_line" && row.credit_limit === null)
    ) {
      context.addIssue({ code: "custom", message: "credit policy fields are inconsistent" });
    }
  });

const SetUserPlanRowSchema = z
  .object({
    user_id: postgresUuid,
    plan_id: postgresUuid,
    plan_key: z.string().min(1),
    plan_assigned_at: timestamp,
    assignment_state: z.enum(["applied", "scheduled"]),
  })
  .strict();

const EntitlementRowSchema = z
  .object({
    feature_key: z.string().min(1),
    feature_type: z.enum(["boolean", "integer", "string", "enum"]),
    feature_value: z.json(),
    catalog_revision_id: postgresUuid,
    plan_key: z.string().min(1).nullable(),
    value_source: z.enum(["default", "plan"]),
  })
  .strict();

const AllowanceRowSchema = z
  .object({
    plan_id: postgresUuid,
    allowance_remaining: decimal,
    period_start: timestamp.transform((value) => value.toISOString()),
    period_end: timestamp.transform((value) => value.toISOString()),
  })
  .strict();

const UnsetPlanRowSchema = z
  .object({
    user_id: postgresUuid,
  })
  .strict();

const PlanMigrationStartRowSchema = postgresUuid;
const PlanMigrationBatchRowSchema = z
  .object({
    migrated: z.number().int().nonnegative(),
    done: z.boolean(),
    next_cursor: postgresUuid.nullable(),
  })
  .strict();
const QuotaStateRowSchema = z
  .object({
    user_id: postgresUuid,
    quota_key: z.string().min(1),
    operation_key: z.string().min(1),
    measure_key: z.string().min(1),
    quota_limit: decimal,
    consumed: decimal,
    reserved: decimal,
    remaining: decimal,
    overage: decimal,
    enforcement: z.enum(["block", "allow"]),
    window_start: timestamp.transform((value) => value.toISOString()),
    window_end: timestamp.transform((value) => value.toISOString()),
    emit_at_percent: z.array(z.number().finite().min(0).max(100)),
  })
  .strict();
const QuotaEventRowSchema = z
  .object({
    event_id: postgresUuid,
    quota_key: z.string().min(1),
    operation_key: z.string().min(1),
    measure_key: z.string().min(1),
    event_type: z.enum(["threshold", "blocked"]),
    threshold_percent: z.number().finite().min(0).max(100).nullable(),
    idempotency_key: z.string().min(1),
    usage_charge_id: postgresUuid.nullable(),
    created_at: timestamp.transform((value) => value.toISOString()),
  })
  .strict();

export type UnsetPlanRow = z.infer<typeof UnsetPlanRowSchema>;
export type UserPlanRow = z.infer<typeof UserPlanRowSchema>;
export type SetUserPlanRow = z.infer<typeof SetUserPlanRowSchema>;
export type EntitlementRow = z.infer<typeof EntitlementRowSchema>;
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
    const row = optionalRecordRow(rows, "PlanRepository.getUserPlan");
    return row === null ? null : safeParse(UserPlanRowSchema, row, "PlanRepository.getUserPlan");
  }

  async getEntitlement(userId: string, feature: string): Promise<EntitlementRow | null> {
    const rows = await this.callproc("get_subject_entitlements", [userId]);
    const entitlements = (rows ?? []).map((row) =>
      safeParse(EntitlementRowSchema, row, "PlanRepository.getEntitlement"),
    );
    return entitlements.find((candidate) => candidate.feature_key === feature) ?? null;
  }

  /** Assign a plan to a user. */
  async setUserPlan(
    userId: string,
    planKey: string,
    planAssignedAt: string | null,
  ): Promise<SetUserPlanRow> {
    return safeParse(
      SetUserPlanRowSchema,
      requireRow(
        await this.callproc("set_subject_plan", [userId, planKey, planAssignedAt]),
        "PlanRepository.setUserPlan",
      ),
      "PlanRepository.setUserPlan",
    );
  }

  /** Remove a user's plan assignment. */
  async unsetUserPlan(userId: string): Promise<UnsetPlanRow> {
    const rows = await this.callproc("unassign_plan", [userId, "sdk_unassignment"]);
    if (
      !safeParse(
        z.boolean(),
        requireRow(rows, "PlanRepository.unsetUserPlan"),
        "PlanRepository.unsetUserPlan",
      )
    ) {
      throw new StoreError("PlanRepository.unsetUserPlan: unassign_plan returned false");
    }
    return safeParse(UnsetPlanRowSchema, { user_id: userId }, "PlanRepository.unsetUserPlan");
  }

  /** Pin or unpin the user's current assignment to its catalog revision. */
  async setPlanRevisionPin(userId: string, pinned: boolean): Promise<boolean> {
    const rows = await this.callproc("set_plan_revision_pin", [userId, pinned]);
    return safeParse(
      z.boolean(),
      requireRow(rows, "PlanRepository.setPlanRevisionPin"),
      "PlanRepository.setPlanRevisionPin",
    );
  }

  /** Apply one bounded batch of renewal-effective plan changes. */
  async applyDuePlanChanges(limit: number): Promise<number> {
    const rows = await this.callproc("apply_due_plan_assignment_changes", [limit]);
    return safeParse(
      z.number().int().nonnegative(),
      requireRow(rows, "PlanRepository.applyDuePlanChanges"),
      "PlanRepository.applyDuePlanChanges",
    );
  }

  /** Check a user's remaining free allowance. */
  async startPlanMigration(
    fromPlanId: string | null,
    toPlanId: string,
  ): Promise<PlanMigrationStartRow> {
    const rows = await this.callproc("start_plan_migration", [fromPlanId, toPlanId]);
    return safeParse(
      PlanMigrationStartRowSchema,
      requireRow(rows, "PlanRepository.startPlanMigration"),
      "PlanRepository.startPlanMigration",
    );
  }

  async migratePlanBatch(migrationId: string, batchSize?: number): Promise<PlanMigrationBatchRow> {
    const rows = await this.callproc("migrate_plan_batch", [migrationId, batchSize ?? 100]);
    return safeParse(
      PlanMigrationBatchRowSchema,
      requireRow(rows, "PlanRepository.migratePlanBatch"),
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
    const row = optionalRecordRow(rows, "PlanRepository.checkAllowance");
    return row === null
      ? null
      : safeParse(AllowanceRowSchema, row, "PlanRepository.checkAllowance");
  }
}
