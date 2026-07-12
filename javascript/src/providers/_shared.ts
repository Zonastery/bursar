import type { BillingManager, BillingEventResult } from "../billing/index.js";

/**
 * Wrapper around bm.handleEvent that throws on unhandled results (except
 * "unhandled_event_type" which is a permanent no-op). Ensures the provider
 * receives a retryable signal when the event could not be processed.
 */
export async function callBillingManager(
  bm: BillingManager,
  event: Parameters<BillingManager["handleEvent"]>[0],
): Promise<BillingEventResult> {
  const result = await bm.handleEvent(event);
  if (
    !result.handled &&
    result.error !== "unhandled_event_type" &&
    result.error !== "user_not_found"
  ) {
    throw new Error(`BillingManager failed to handle event: ${result.error}`);
  }
  return result;
}
