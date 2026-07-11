import { z } from "zod";
import type { CallProc } from "./types.js";
import { DeductionRowSchema } from "./deduction.js";
import type { DeductionRow } from "./deduction.js";

export const LeaseRowSchema = z
  .object({
    lease_id: z.string().optional(),
    user_id: z.string().optional(),
    amount: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
    available: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
    reserved: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
    billing_mode: z.string().optional(),
    expires_at: z.string().optional(),
    error: z.string().optional(),
  })
  .passthrough();

export const ReleaseRowSchema = z
  .object({
    released: z
      .union([z.boolean(), z.string()] as const)
      .nullable()
      .optional(),
    reason: z.string().nullable().optional(),
  })
  .passthrough();

export type LeaseRow = z.infer<typeof LeaseRowSchema>;
export type ReleaseRow = z.infer<typeof ReleaseRowSchema>;

export class LeaseRepository {
  constructor(private callproc: CallProc) {}

  async createLease(params: {
    userId: string;
    amount: string;
    operationType: string;
    billingMode: string;
    floor: string;
    maxConcurrent: number | null;
    ttlSeconds: number;
    model: string | null;
    overdraftFloor: string | null;
    metadata: string;
    periodStart: string | null;
    feature: string | null;
    featureMaxCalls: number | null;
    featureOnExceed: string | null;
    featurePeriodStart: string | null;
    featurePeriodEnd: string | null;
  }): Promise<LeaseRow> {
    const rows = await this.callproc("create_lease", [
      params.userId,
      params.amount,
      params.operationType,
      params.billingMode,
      params.floor,
      params.maxConcurrent,
      params.ttlSeconds,
      params.model,
      params.overdraftFloor,
      params.metadata,
      params.periodStart,
      params.feature,
      params.featureMaxCalls,
      params.featureOnExceed,
      params.featurePeriodStart,
      params.featurePeriodEnd,
    ]);
    return LeaseRowSchema.parse(rows?.[0] ?? {});
  }

  async settleLease(params: {
    userId: string;
    leaseId: string;
    amount: string;
    idempotencyKey: string | null;
    minBalance: string;
    model: string | null;
    metadata: string;
    skipAllowance: boolean;
    periodStart: string | null;
    feature: string | null;
    featureMaxCalls: number | null;
    featureOnExceed: string | null;
    featurePeriodStart: string | null;
    featurePeriodEnd: string | null;
  }): Promise<DeductionRow> {
    const rows = await this.callproc("settle_lease", [
      params.userId,
      params.leaseId,
      params.amount,
      params.idempotencyKey,
      params.minBalance,
      params.model,
      params.metadata,
      params.skipAllowance,
      params.periodStart,
      params.feature,
      params.featureMaxCalls,
      params.featureOnExceed,
      params.featurePeriodStart,
      params.featurePeriodEnd,
    ]);
    return DeductionRowSchema.parse(rows?.[0] ?? {});
  }

  async releaseLease(userId: string, leaseId: string): Promise<ReleaseRow> {
    const rows = await this.callproc("release_lease", [userId, leaseId]);
    return ReleaseRowSchema.parse(rows?.[0] ?? {});
  }

  async renewLease(userId: string, leaseId: string, ttlSeconds: number): Promise<LeaseRow> {
    const rows = await this.callproc("renew_lease", [userId, leaseId, ttlSeconds]);
    return LeaseRowSchema.parse(rows?.[0] ?? {});
  }
}
