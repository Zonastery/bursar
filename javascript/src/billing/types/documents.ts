import type { ProviderRef } from "./common.js";

export interface BillingCustomerInfo {
  providerCustomerId?: string | null;
  email?: string | null;
}

export interface BillingInvoiceInfo {
  provider?: string;
  providerInvoiceId: string;
  status?: string | null;
  amountPaidMinor?: number | null;
  amountDueMinor?: number | null;
  currency?: string | null;
  periodStart?: string | null;
  periodEnd?: string | null;
}

export interface BillingPaymentInfo {
  providerPaymentId: string;
  amountMinor: number;
  taxMinor?: number | null;
  currency: string;
  refs?: ProviderRef | null;
  purpose: "subscription" | "credit_topup" | "unknown";
  status?: "pending" | "succeeded" | "failed" | "canceled";
}

export interface BillingRefundInfo {
  providerRefundId: string;
  providerPaymentId?: string | null;
  amountMinor: number;
  currency: string;
  reason?: string | null;
  status?: "pending" | "succeeded" | "failed" | "canceled";
}

export interface BillingDisputeInfo {
  providerDisputeId: string;
  providerPaymentId?: string | null;
  status?: string | null;
  reason?: string | null;
}
