import type Decimal from "decimal.js";

import type { UsageMetrics } from "../metrics.js";
import type { Logger } from "../shared/logger.js";
import type { CreditEvent } from "./events.js";
import type {
  BillingMode,
  CreditMetadata,
  DeductionResult,
  UsageAnalyticsStore,
} from "./types/index.js";

export type PolicyPreset = "strict_prepaid" | "overdraft";
export type MetricsOrAmount = UsageMetrics | Decimal | number;

export interface PostDeductionContext {
  userId: string;
  source: "deduct" | "settle" | "raw";
  deduction:
    | DeductionResult
    | {
        entryId: string;
        userId: string;
        amount: Decimal;
        balanceAfter: Decimal;
        idempotent: boolean;
      };
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
  /** Fallback credit policy for subjects without a plan assignment. */
  policy?: PolicyPreset;
  /** Planless-subject floor used with the `overdraft` fallback. */
  overdraftFloor?: Decimal | number | null;
  /** Planless-subject admission limit fallback. */
  maxConcurrent?: number | null;
  /**
   * Edge-triggered low-balance thresholds and optional non-blocking handler.
   * Defaults to `engine.minBalance * 2` when omitted.
   */
  lowBalance?: LowBalanceConfig | null;
  /** Default lease TTL in seconds. Defaults to 600. */
  defaultTtlSeconds?: number;
  /** Sweep a subject's expired credits before balance-sensitive operations. */
  lazyExpiry?: boolean;
  /**
   * Pricing-engine cache TTL in milliseconds. A value of 0 disables automatic
   * reloads. Concurrent reloads are deduplicated.
   */
  pricingTtl?: number;
  /**
   * Awaited after a committed, non-replayed deduction. Hook failures are
   * isolated from the committed credit charge.
   */
  postDeduction?: ((context: PostDeductionContext) => void | Promise<void>) | null;
}

export interface ReserveOptions {
  /** Replay-safe acquisition key. A random key is generated when omitted. */
  idempotencyKey?: string | null;
  operationType?: string;
  billingMode?: BillingMode | null;
  /** @deprecated Use `feature`. */
  requiredFeature?: string | null;
  ttl?: number | null;
  metadata?: CreditMetadata | null;
  feature?: string | null;
}

export interface SettleOptions {
  idempotencyKey?: string | null;
  metadata?: CreditMetadata | null;
  /** Feature supplied at reserve time, used for invocation-count accounting. */
  feature?: string | null;
}

export interface CanAffordOptions {
  feature?: string | null;
  /** @deprecated Use `feature`. */
  requiredFeature?: string | null;
  billingMode?: BillingMode | null;
  operationType?: string;
}

export interface GrantSubscriptionCycleOptions {
  bucket?: string;
  expiresAt?: Date;
  ttlDays?: number;
  /** Expire the prior cycle balance first. Defaults to true. */
  replacePrior?: boolean;
  planKey?: string;
  idempotencyKey?: string;
  metadata?: CreditMetadata | null;
}

export interface RunBilledOptions<T> {
  estimate: MetricsOrAmount;
  doWork: () => Promise<{ result: T; actual: MetricsOrAmount }>;
  operationType?: string;
  billingMode?: BillingMode | null;
  /** @deprecated Use `feature`. */
  requiredFeature?: string | null;
  idempotencyKey?: string | null;
  ttl?: number | null;
  feature?: string | null;
}
