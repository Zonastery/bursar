import type { BillingOfferInterval, ProviderRef, SubscriptionGrant } from "./common.js";

export interface BillingGrantResult {
  mode?: string;
  credits?: number | null;
  bucket?: string;
  replacePrior?: boolean;
}

export interface BillingOfferResult {
  offerId: string;
  offerKey: string;
  planId?: string | null;
  plan?: string | null;
  interval?: string;
  intervalCount?: number;
  grant?: BillingGrantResult;
}

export interface BillingTopupResult {
  topupId: string;
  topupKey: string;
  creditsPerUnit?: number;
  depositTo?: string;
  amountMinor?: number;
  currency?: string;
  minQuantity?: number;
  maxQuantity?: number;
  defaultQuantity?: number;
  minAmountMinor?: number;
  maxAmountMinor?: number;
}

export interface BillingOffer {
  plan: string;
  interval?: BillingOfferInterval;
  intervalCount?: number;
  grant?: SubscriptionGrant;
  providers?: Record<string, ProviderRef>;
  validFrom?: string | null;
  validTo?: string | null;
}

export interface BillingCreditTopup {
  depositTo: string;
  creditsPerUnit?: number;
  minAmountMinor?: number;
  maxAmountMinor?: number;
  taxBehavior?: "exclude_tax" | "include_tax";
  providers?: Record<string, ProviderRef>;
}
