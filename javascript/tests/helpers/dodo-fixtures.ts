/**
 * Realistic Dodo webhook payloads.
 *
 * These match what Dodo actually sends — no `data.id`/`data.payment_id` on
 * subscription events, and dates in JS Date.prototype.toString() format.
 */
import type { BillingEventSink } from "../../src/billing/contracts.js";
import { handleDodoBillingEvent } from "../../src/providers/dodo/event-mapper.js";

export const DODO_JS_DATE = "Sat Jul 18 2026 05:15:24 GMT+0000 (Coordinated Universal Time)";
export const DODO_ISO_DATE = "2026-07-18T05:15:24.000Z";

export function dodoEventId(eventType: string, objectId: string): string {
  return `dodo:${eventType}:${objectId}:${DODO_ISO_DATE}`;
}

export async function mapDodoEvent(
  eventType: string,
  data: Record<string, unknown>,
  userId: string | null,
  metadata: Record<string, string>,
  sink: BillingEventSink,
): Promise<void> {
  await handleDodoBillingEvent(
    { type: eventType, timestamp: DODO_ISO_DATE, data },
    userId,
    metadata,
    sink,
  );
}

export const DODO_SUBSCRIPTION_ACTIVE = {
  subscription_id: "sub_dodo_active_001",
  status: "active",
  product_id: "prod_monk",
  payment_frequency_interval: "Month",
  payment_frequency_count: 1,
  previous_billing_date: "Sat Jul 18 2026 05:15:24 GMT+0000 (Coordinated Universal Time)",
  next_billing_date: "Sat Aug 18 2026 05:15:24 GMT+0000 (Coordinated Universal Time)",
};

export const DODO_SUBSCRIPTION_ACTIVE_PLAN_SLUG = {
  subscription_id: "sub_dodo_slug_001",
  status: "active",
  payment_frequency_interval: "Month",
  payment_frequency_count: 1,
  previous_billing_date: DODO_JS_DATE,
  next_billing_date: DODO_JS_DATE,
};

export const DODO_SUBSCRIPTION_ACTIVE_NO_DATES = {
  subscription_id: "sub_dodo_no_dates",
  status: "active",
  product_id: "prod_monk",
};

export const DODO_SUBSCRIPTION_RENEWED = {
  subscription_id: "sub_dodo_renewed_001",
  status: "active",
  product_id: "prod_monk",
  payment_frequency_interval: "Month",
  payment_frequency_count: 1,
  previous_billing_date: DODO_JS_DATE,
  next_billing_date: DODO_JS_DATE,
};

export const DODO_SUBSCRIPTION_UPDATED = {
  subscription_id: "sub_dodo_updated_001",
  status: "active",
  product_id: "prod_monk",
  next_billing_date: DODO_JS_DATE,
};

export const DODO_SUBSCRIPTION_CANCELLED = {
  subscription_id: "sub_dodo_cancelled_001",
  product_id: "prod_monk",
};

export const DODO_SUBSCRIPTION_EXPIRED = {
  subscription_id: "sub_dodo_expired_001",
};

export const DODO_SUBSCRIPTION_FAILED = {
  subscription_id: "sub_dodo_failed_001",
};

export const DODO_SUBSCRIPTION_ON_HOLD = {
  subscription_id: "sub_dodo_on_hold_001",
};

export const DODO_SUBSCRIPTION_PLAN_CHANGED = {
  subscription_id: "sub_dodo_plan_change_001",
  product_id: "prod_sage",
  cancel_at_next_billing_date: true,
};

export const DODO_PAYMENT_SUCCEEDED = {
  id: "pay_dodo_success_001",
  payment_id: "pay_dodo_success_001",
  subscription_id: "sub_dodo_active_001",
  settlement_amount: 2999,
  settlement_currency: "USD",
  settlement_tax: 240,
  product_id: "prod_monk",
};

export const DODO_PAYMENT_FAILED = {
  id: "pay_dodo_failed_001",
  payment_id: "pay_dodo_failed_001",
  subscription_id: "sub_dodo_active_001",
  total_amount: 2999,
  currency: "USD",
  tax: 240,
};

export const DODO_REFUND_SUCCEEDED = {
  id: "refund_dodo_001",
  payment_id: "pay_dodo_success_001",
  refund_amount: 2999,
  currency: "USD",
  reason: "Customer requested",
};

export const DODO_DISPUTE_OPENED = {
  id: "dispute_dodo_001",
  payment_id: "pay_dodo_success_001",
  reason: "fraudulent",
};

export const DODO_DISPUTE_WON_CLOSED = {
  id: "dispute_dodo_won_001",
  payment_id: "pay_dodo_success_001",
};
