import type { Decimal } from "decimal.js";

export type BillingMode = "strict" | "overdraft";

export interface CreditMetadata {
  operation?: string | null;
  measures?: Record<string, unknown> | null;
  dimensions?: Record<string, string | number | boolean | Decimal.Value> | null;
  breakdownTotal?: string | null;
  referenceType?: string | null;
  referenceId?: string | null;
  idempotencyKey?: string | null;
  providerRequestId?: string | null;
  traceId?: string | null;
  spanId?: string | null;
  [key: string]: unknown;
}

export interface BucketDefinition {
  label: string;
  priority: number;
  expires: boolean;
  ttlDays?: number | null;
  allowOverdraft?: boolean;
  default?: boolean;
}

export interface BucketBalance {
  bucketKey: string;
  label: string;
  priority: number;
  expires: boolean;
  balance: Decimal;
}

export interface BucketBalancesResult {
  userId: string;
  buckets: BucketBalance[];
  totalBalance: Decimal;
}

export interface BalanceResult {
  userId: string;
  balance: Decimal;
  lifetimePurchased: Decimal;
}

export interface AddCreditsResult {
  entryId: string;
  userId: string;
  amount: Decimal;
  newBalance: Decimal;
  lifetimePurchased: Decimal;
  /** Destination bucket for grants; `null` when a debit spans one or more lots. */
  bucket: string | null;
  idempotent: boolean;
}

interface DeductionResultBase {
  userId: string;
  amount: Decimal;
  allowanceConsumed: Decimal;
  idempotent: boolean;
}

/** A committed usage charge. Allowance-only charges legitimately have no ledger entry. */
export interface DeductionSuccess extends DeductionResultBase {
  error: null;
  entryId: string | null;
  /** Canonical usage receipt, including zero-cost or allowance-only charges. */
  usageChargeId: string | null;
  balanceAfter: Decimal;
  bucketBreakdown: Record<string, Decimal> | null;
}

/** An expected database rejection. Nullable fields were not committed and are not fabricated. */
export interface DeductionFailure extends DeductionResultBase {
  error: string;
  entryId: null;
  usageChargeId: string | null;
  balanceAfter: Decimal | null;
  idempotent: false;
  bucketBreakdown: null;
}

export type DeductionResult = DeductionSuccess | DeductionFailure;

export interface DeductWithAllowanceOptions {
  idempotencyKey: string;
  operation?: string | null;
  feature?: string | null;
  model?: string | null;
  region?: string | null;
  measures?: Record<string, unknown> | null;
  dimensions?: Record<string, unknown> | null;
  metadata?: CreditMetadata | null;
}

interface RefundResultBase {
  originalEntryId: string;
  userId: string | null;
}

export interface RefundSuccess extends RefundResultBase {
  error: null;
  refundEntryId: string;
  userId: string;
  amount: Decimal;
  newBalance: Decimal;
  bucketBreakdown: Record<string, Decimal> | null;
}

export interface RefundFailure extends RefundResultBase {
  error: string;
  refundEntryId: null;
  amount: Decimal | null;
  newBalance: Decimal | null;
  bucketBreakdown: null;
}

export type RefundResult = RefundSuccess | RefundFailure;

/** Credits removed from all remaining lots created by one ledger operation. */
export interface RevokeCreditsResult {
  userId: string;
  entryType: string;
  revoked: Decimal;
  balanceAfter: Decimal;
}

export interface SweepResult {
  expiredCount: number;
  expiredAmount: Decimal;
  dryRun: boolean;
  expiredByBucket: Record<string, Decimal>;
}
