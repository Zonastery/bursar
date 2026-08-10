import type { Decimal } from "decimal.js";

export interface BillingGrantResult {
  mode: "cycle_grant";
  credits: Decimal;
  bucket: string;
  replacePrior: boolean;
}

export interface BillingOfferResult {
  offerId: string;
  offerKey: string;
  planId: string;
  plan: string;
  interval: "day" | "week" | "month" | "year";
  intervalCount: number;
  grant: BillingGrantResult | null;
}

export interface BillingTopupResult {
  topupId: string;
  topupKey: string;
  creditsPerUnit: Decimal;
  depositTo: string;
  amountMinor: number;
  currency: string;
  minQuantity: number;
  maxQuantity: number;
  defaultQuantity: number;
  minAmountMinor: number;
  maxAmountMinor: number;
}
