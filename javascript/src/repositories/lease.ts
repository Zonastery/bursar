import { z } from "zod";
import type { CallProc } from "./types.js";
import { DeductionRowSchema } from "./deduction.js";
import type { DeductionRow } from "./deduction.js";
import { pgBoolean, safeParse } from "./_shared.js";

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
    released: pgBoolean.nullable().optional(),
    reason: z.string().nullable().optional(),
  })
  .passthrough();

export type LeaseRow = z.infer<typeof LeaseRowSchema>;
export type ReleaseRow = z.infer<typeof ReleaseRowSchema>;

/** Repository for lease lifecycle operations (admission control). */
export class LeaseRepository {
  constructor(private callproc: CallProc) {}

  /** Atomically acquire a lease (hold) — admission control. */
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
    return safeParse(LeaseRowSchema, rows?.[0] ?? {}, "LeaseRepository.createLease");
  }

  /** Charge the actual cost against a lease and mark it settled. */
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
    return safeParse(DeductionRowSchema, rows?.[0] ?? {}, "LeaseRepository.settleLease");
  }

  /** Release a lease without charging — idempotent. */
  async releaseLease(userId: string, leaseId: string): Promise<ReleaseRow> {
    const rows = await this.callproc("release_lease", [userId, leaseId]);
    return safeParse(ReleaseRowSchema, rows?.[0] ?? {}, "LeaseRepository.releaseLease");
  }

  /** Extend an active lease's TTL. */
  async renewLease(userId: string, leaseId: string, ttlSeconds: number): Promise<LeaseRow> {
    const rows = await this.callproc("renew_lease", [userId, leaseId, ttlSeconds]);
    return safeParse(LeaseRowSchema, rows?.[0] ?? {}, "LeaseRepository.renewLease");
  }
}
