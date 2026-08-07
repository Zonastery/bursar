import type { Decimal } from "decimal.js";
import type { BillingMode } from "./account.js";

interface LeaseResultBase {
  userId: string;
  available: Decimal;
  reservedTotal: Decimal;
  billingMode: BillingMode;
}

export interface LeaseSuccess extends LeaseResultBase {
  error: null;
  leaseId: string;
  amount: Decimal;
  minimumBalance: Decimal;
  expiresAt: string;
}

export interface LeaseFailure extends LeaseResultBase {
  error: string;
  leaseId: null;
  amount: Decimal | null;
  minimumBalance: Decimal | null;
  expiresAt: null;
}

export type LeaseResult = LeaseSuccess | LeaseFailure;

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
