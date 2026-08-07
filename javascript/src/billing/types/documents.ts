import type Decimal from "decimal.js";
import type { ProviderRef } from "./common.js";

export interface BillingCustomerInfo {
  providerCustomerId?: string | null;
  email?: string | null;
}

export interface BillingInvoiceInfo {
  providerInvoiceId: string;
  status: "draft" | "open" | "paid" | "void" | "uncollectible";
  amountPaidMinor: number;
  amountDueMinor: number;
  currency: string;
  periodStart?: string | null;
  periodEnd?: string | null;
}

/** Persisted invoice document returned from account-level billing queries. */
export interface BillingInvoiceRecord extends BillingInvoiceInfo {
  provider: string;
}

export interface BillingPaymentInfo {
  providerPaymentId: string;
  amountMinor: number;
  taxMinor: number;
  currency: string;
  refs?: ProviderRef | null;
  purpose: "subscription" | "credit_topup";
  status: "pending" | "succeeded" | "failed" | "canceled";
}

/** Persisted payment state used by billing lifecycle handlers. */
export interface BillingPaymentRecord {
  id: string;
  provider: string;
  providerPaymentId: string;
  providerInvoiceId: string | null;
  userId: string;
  amountMinor: number;
  taxMinor: number;
  currency: string;
  purpose: "subscription" | "credit_topup";
  status: "pending" | "succeeded" | "failed" | "canceled";
  providerUpdatedAt: string;
  metadata: Record<string, unknown>;
}

/** Result of posting a billing grant or refund to the credit ledger. */
export interface BillingCreditPostingResult {
  ledgerEntryId: string | null;
  balanceAfter: Decimal | null;
  replayed: boolean;
  errorCode: string | null;
}

export interface BillingRefundInfo {
  providerRefundId: string;
  providerPaymentId: string;
  amountMinor: number;
  currency: string;
  reason?: string | null;
  status: "pending" | "succeeded" | "failed" | "canceled";
}

export interface BillingDisputeInfo {
  providerDisputeId: string;
  providerPaymentId: string;
  status: "needs_response" | "under_review" | "won" | "lost" | "closed";
  reason?: string | null;
}
