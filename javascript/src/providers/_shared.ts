import type { BillingEvent, BillingEventResult } from "../billing/index.js";
import type { BillingEventSink } from "../bursar.js";

/**
 * Wrapper around the facade event sink that throws on unhandled results (except
 * "unhandled_event_type" which is a permanent no-op). Ensures the provider
 * receives a retryable signal when the event could not be processed.
 */
export async function callBillingEventSink(
  sink: BillingEventSink,
  event: BillingEvent,
): Promise<BillingEventResult> {
  const result = await sink.ingestBillingEvent(event);
  if (
    !result.handled &&
    result.error !== "unhandled_event_type" &&
    result.error !== "user_not_found"
  ) {
    throw new Error(`Bursar failed to ingest billing event: ${result.error}`);
  }
  return result;
}
