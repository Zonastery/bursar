export const AUTO_RECHARGE_STATES = ["disabled", "active", "paused"] as const;
export type BillingAutoRechargeState = (typeof AUTO_RECHARGE_STATES)[number];

export interface BillingAutoRechargeProfile {
  userId: string;
  enabled: boolean;
  state: BillingAutoRechargeState;
  armed?: boolean;
  provider: string | null;
  topupId: string | null;
  quantity: number;
  threshold: number;
  maxChargesPerWindow: number | null;
  windowUnit: "second" | "minute" | "hour" | "day" | "week" | "month" | "year";
  windowCount: number;
  windowAnchor: "calendar" | "plan_assignment" | "rolling";
  windowTimezone: string;
  updatedAt?: string | null;
}

export interface BillingAutoRechargeAttempt {
  id: string;
  userId: string;
  provider: string;
  idempotencyKey: string;
  providerAttemptId: string | null;
  topupId: string;
  quantity: number;
  state:
    | "claimed"
    | "submitted"
    | "processing"
    | "unknown"
    | "succeeded"
    | "failed"
    | "action_required";
  windowStart: string;
  windowEnd: string;
  quotedAmountMinor: number | null;
  currency: string | null;
  failureCode: string | null;
  failureMessage: string | null;
  metadata: Record<string, unknown>;
  createdAt: string;
  updatedAt: string;
}

export interface BillingAutoRechargeStatus {
  enabled: boolean;
  state: BillingAutoRechargeState;
  thresholdCredits: number;
  topupKey: string;
  quantity: number;
  maxRecharges: number;
  /** @deprecated Prefer the exact `windowStart` and `windowEnd` boundaries. */
  windowDays: number;
  windowStart: string;
  windowEnd: string;
  rechargesInWindow: number;
  paymentMethodId: string | null;
  paymentMethodLast4: string | null;
  paymentMethodBrand: string | null;
  suspendedReason: string | null;
  pendingAttemptId: string | null;
  quoteAmountMinor: number | null;
  quoteCurrency: string | null;
}
