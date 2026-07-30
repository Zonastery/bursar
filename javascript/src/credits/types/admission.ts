import type { Decimal } from "decimal.js";
import type { BillingMode } from "./account.js";

export interface LeaseResult {
  leaseId: string;
  userId: string;
  amount: Decimal;
  available: Decimal;
  reservedTotal: Decimal;
  minimumBalance: Decimal;
  billingMode: BillingMode;
  expiresAt: string;
  error?: string | null;
}

/** Immutable pricing references captured when an operation lease is admitted. */
export interface LeasePricingContext {
  catalogVersion: number;
  planId: string | null;
  planKey: string | null;
  rateCard: string | null;
}

export interface ReleaseResult {
  leaseId: string;
  userId: string;
  released: boolean;
  reason?: string | null;
}

export interface CanAffordResult {
  affordable: boolean;
  spendable: Decimal;
  worstCase: Decimal;
  reason?: string | null;
}

export interface AvailableResult {
  userId: string;
  balance: Decimal;
  reserved: Decimal;
  available: Decimal;
}
