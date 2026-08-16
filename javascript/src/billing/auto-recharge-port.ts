import type {
  BillingAutoRechargeAttempt,
  BillingAutoRechargeAttemptState,
  BillingAutoRechargeProfile,
  BillingCustomerRecord,
  BillingTopupResult,
} from "./types/index.js";
import type { JsonObject } from "../shared/json.js";

/** Narrow billing capability required by AutoRechargeService. */
export interface AutoRechargeBillingPort {
  getActiveCatalogDocument(): Promise<JsonObject | null>;
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
  upsertAutoRechargeProfile(
    profile: BillingAutoRechargeProfile,
    options?: { resetCooldown?: boolean },
  ): Promise<void>;
  claimAutoRechargeAttempt(input: {
    userId: string;
    idempotencyKey: string;
  }): Promise<BillingAutoRechargeAttempt | null>;
  updateAutoRechargeAttempt(input: {
    id: string;
    state: BillingAutoRechargeAttemptState;
    providerAttemptId?: string | null;
    failureCode?: string | null;
    failureMessage?: string | null;
    metadata?: JsonObject;
  }): Promise<void>;
  /** Count attempts since an exact instant. */
  countAutoRechargeAttempts(userId: string, since: string | Date): Promise<number>;
}
