import type Decimal from "decimal.js";

import type { UsageMetrics } from "../metrics.js";
import type { Logger } from "../shared/logger.js";
import type { CreditEvent } from "./events.js";
import type {
  BillingMode,
  CreditMetadata,
  DeductionResult,
  DeductionSuccess,
  UsageAnalyticsStore,
  UsageChargeStore,
} from "./types/index.js";

export type PolicyPreset = "strict_prepaid" | "overdraft";
export type MetricsOrAmount = UsageMetrics | Decimal | number;

export interface PostDeductionContext {
  userId: string;
  source: "deduct" | "settle" | "raw";
  deduction: DeductionSuccess;
}

export interface LowBalanceConfig {
  thresholds?: (Decimal | number)[] | null;
  onTrigger?: ((event: CreditEvent) => void | Promise<void>) | null;
  /**
   * Maximum number of subjects whose breached-threshold state is retained in
   * memory. Defaults to 100,000.
   */
  maxTrackedUsers?: number;
}

export interface CreditsServiceOptions {
  logger?: Logger | null;
  /**
   * Optional read-only analytics backend. Defaults to the credit store, which
   * keeps PostgreSQL as the zero-infrastructure behavior.
   */
  analytics?: UsageAnalyticsStore | null;
  /** Optional usage-history backend. Defaults to the credit store. */
  usageStore?: UsageChargeStore | null;
  /** Fallback credit policy for subjects without a plan assignment. */
  policy?: PolicyPreset;
  /** Planless-subject floor used with the `overdraft` fallback. */
  overdraftFloor?: Decimal | number | null;
  /** Planless-subject admission limit fallback. */
  maxConcurrent?: number | null;
  /**
   * Edge-triggered low-balance thresholds and optional non-blocking handler.
   * Defaults to zero when omitted.
   */
  lowBalance?: LowBalanceConfig | null;
  /** Default lease TTL in seconds. Defaults to 600. */
  defaultTtlSeconds?: number;
  /** Sweep a subject's expired credits before balance-sensitive operations. */
  lazyExpiry?: boolean;
  /**
   * Catalog cache TTL in milliseconds. A value of 0 disables automatic
   * reloads. Concurrent reloads are deduplicated. Defaults to 300,000.
   */
  catalogCacheTtlMs?: number;
  /**
   * Awaited after a committed, non-replayed deduction. Hook failures are
   * isolated from the committed credit charge.
   */
  postDeduction?: ((context: PostDeductionContext) => void | Promise<void>) | null;
}

export interface AddCreditsOptions {
  type?: string;
  metadata?: CreditMetadata | null;
  expiresAt?: Date | null;
  /** Target credit bucket; omitted resolves to the catalog's default bucket. */
  bucket?: string | null;
  /** Stable replay key for the ledger mutation. */
  idempotencyKey?: string | null;
}

export interface DeductCreditsOptions {
  entryType?: string;
  bucket?: string | null;
  metadata?: CreditMetadata | null;
  /** Stable replay key for the ledger mutation. */
  idempotencyKey?: string | null;
}

export interface DeductOptions {
  idempotencyKey?: string | null;
  metadata?: CreditMetadata | null;
  /** Entitlement feature required for this operation. */
  feature?: string | null;
}

export type DeductFlatJobOptions = DeductOptions;

export interface RecordUsageOptions {
  idempotencyKey?: string | null;
  metadata?: CreditMetadata | null;
}

export interface RefundCreditsOptions {
  amount?: Decimal | number;
  reason?: string;
  metadata?: CreditMetadata | null;
  idempotencyKey?: string | null;
}

export interface DeductTeamOptions {
  idempotencyKey?: string | null;
  metadata?: CreditMetadata | null;
}

export interface ReserveOptions {
  /** Replay-safe acquisition key. A random key is generated when omitted. */
  idempotencyKey?: string | null;
  operationType?: string;
  billingMode?: BillingMode | null;
  ttl?: number | null;
  metadata?: CreditMetadata | null;
  feature?: string | null;
  /** Model tag used when reserving a raw amount instead of usage metrics. */
  model?: string | null;
}

export interface SettleOptions {
  idempotencyKey?: string | null;
  metadata?: CreditMetadata | null;
  /** Entitlement key supplied at reserve time. */
  feature?: string | null;
}

export interface CanAffordOptions {
  feature?: string | null;
  billingMode?: BillingMode | null;
  operationType?: string;
}

export interface GrantSubscriptionCycleOptions {
  bucket?: string;
  expiresAt?: Date;
  ttlDays?: number;
  planKey?: string;
  idempotencyKey?: string;
  metadata?: CreditMetadata | null;
}

export interface RunBilledOptions<T> {
  estimate: MetricsOrAmount;
  doWork: () => Promise<{ result: T; actual: MetricsOrAmount }>;
  operationType?: string;
  billingMode?: BillingMode | null;
  /** Stable key for the complete reserve/work/settle operation. */
  operationKey?: string | null;
  ttl?: number | null;
  feature?: string | null;
  metadata?: CreditMetadata | null;
  /** Settlement attempts for SDK-classified transient failures. Defaults to 3. */
  settlementAttempts?: number;
}

export interface BeginBilledOperationOptions {
  estimate: MetricsOrAmount;
  /** Stable key for the complete reserve/settle operation. */
  operationKey: string;
  operationType?: string;
  billingMode?: BillingMode | null;
  ttl?: number | null;
  feature?: string | null;
  metadata?: CreditMetadata | null;
}

export interface BilledOperation {
  readonly leaseId: string;
  readonly operationKey: string;
  settle(actual: MetricsOrAmount, metadata?: CreditMetadata | null): Promise<DeductionResult>;
  renew(ttl?: number | null): Promise<void>;
  release(): Promise<void>;
}
