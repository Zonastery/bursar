import type { Decimal } from "decimal.js";

export interface CheckFeatureResult {
  userId: string;
  feature: string;
  value: unknown;
  hasFeature: boolean;
}

export interface QuotaState {
  userId: string;
  quotaKey: string;
  operation: string;
  measure: string;
  limit: Decimal;
  consumed: Decimal;
  reserved: Decimal;
  remaining: Decimal;
  overage: Decimal;
  enforcement: "block" | "allow";
  windowStart: string;
  windowEnd: string;
  emitAtPercent: number[];
}

export interface QuotaEvent {
  eventId: string;
  quotaKey: string;
  operation: string;
  measure: string;
  eventType: "threshold" | "blocked";
  thresholdPercent: number | null;
  idempotencyKey: string;
  usageChargeId: string | null;
  createdAt: string;
}

export interface ListQuotaEventsOptions {
  after?: Date | null;
  limit?: number;
  idempotencyKey?: string | null;
}

/** @deprecated Use `QuotaState`. */
export interface FeatureLimitResult {
  userId: string;
  feature: string;
  limited: boolean;
  limit: number;
  used: number;
  remaining: number;
  periodStart: string;
  periodEnd: string;
  action: "deny" | "warn" | "notify" | null;
}
