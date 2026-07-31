import type {
  BillingAutoRechargeAttempt,
  BillingAutoRechargeProfile,
  BillingCustomerRecord,
  BillingTopupResult,
} from "./types/index.js";

/** Narrow billing capability required by AutoRechargeService. */
export interface AutoRechargeBillingPort {
  getActiveBursarConfig(): Promise<Record<string, unknown> | null>;
  resolveTopup(
    provider: string,
    productId?: string | null,
    priceId?: string | null,
  ): Promise<BillingTopupResult | null>;
  resolveTopupByLookup(provider: string, lookupKey: string): Promise<BillingTopupResult | null>;
  getCustomerByUserId(
    userId: string,
    provider?: string | null,
  ): Promise<BillingCustomerRecord | null>;
  getAutoRechargeProfile(userId: string): Promise<BillingAutoRechargeProfile | null>;
  upsertAutoRechargeProfile(profile: BillingAutoRechargeProfile): Promise<void>;
  claimAutoRechargeAttempt(input: {
    userId: string;
    idempotencyKey: string;
  }): Promise<BillingAutoRechargeAttempt | null>;
  updateAutoRechargeAttempt(input: {
    id: string;
    state: string;
    providerAttemptId?: string | null;
    failureCode?: string | null;
    failureMessage?: string | null;
    metadata?: Record<string, unknown>;
  }): Promise<void>;
  /** Count attempts since an exact instant. */
  countAutoRechargeAttempts(userId: string, since: string | Date): Promise<number>;
}
