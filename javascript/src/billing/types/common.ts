export type BillingProvider = "stripe" | "dodo" | "mock";

export type BillingOfferInterval = "day" | "week" | "month" | "year";

export type EntitlementMode = "allowance" | "cycle_grant";

export interface ProviderRef {
  productId?: string | null;
  priceId?: string | null;
  variantId?: string | null;
  lookupKey?: string | null;
}

export interface SubscriptionGrant {
  mode?: EntitlementMode;
  credits?: number | null;
  bucket?: string | null;
  replacePrior?: boolean;
}
