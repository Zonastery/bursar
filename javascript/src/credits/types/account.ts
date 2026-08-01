import type { Decimal } from "decimal.js";

export type BillingMode = "strict" | "overdraft";

export interface CreditMetadata {
  operation?: string | null;
  measures?: Record<string, unknown> | null;
  dimensions?: Record<string, string> | null;
  breakdownTotal?: string | null;
  referenceType?: string | null;
  referenceId?: string | null;
  idempotencyKey?: string | null;
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
  bucket: string;
  idempotent: boolean;
}

export interface DeductionResult {
  entryId: string;
  userId: string;
  amount: Decimal;
  allowanceConsumed: Decimal;
  balanceAfter: Decimal;
  idempotent: boolean;
  error?: string | null;
  bucketBreakdown?: Record<string, Decimal> | null;
}

export interface DeductWithAllowanceOptions {
  idempotencyKey?: string | null;
  operation?: string | null;
  feature?: string | null;
  model?: string | null;
  region?: string | null;
  measures?: Record<string, unknown> | null;
  dimensions?: Record<string, unknown> | null;
  metadata?: CreditMetadata | null;
}

export interface RefundResult {
  refundEntryId: string;
  originalEntryId: string;
  userId: string;
  amount: Decimal;
  newBalance: Decimal;
  error?: string | null;
  bucketBreakdown?: Record<string, Decimal> | null;
}

export interface SweepResult {
  expiredCount: number;
  expiredAmount: Decimal;
  dryRun: boolean;
  expiredByBucket?: Record<string, Decimal> | null;
}
