import type { BillingEvent, BillingEventResult } from "../billing/index.js";
import type { BillingEventSink } from "../bursar.js";

const BUSY_RETRY_DELAYS_MS = [25, 50, 100, 200, 400, 800];

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

/**
 * Wrapper around the facade event sink that throws on unhandled results (except
 * "unhandled_event_type" which is a permanent no-op). Ensures the provider
 * receives a retryable signal when the event could not be processed.
 */
export async function callBillingEventSink(
  sink: BillingEventSink,
  event: BillingEvent,
): Promise<BillingEventResult> {
  for (let attempt = 0; ; attempt += 1) {
    const result = await sink.ingestBillingEvent(event);
    if (result.error === "claim_busy" && attempt < BUSY_RETRY_DELAYS_MS.length) {
      await delay(BUSY_RETRY_DELAYS_MS[attempt]);
      continue;
    }
    if (
      !result.handled &&
      result.error !== "unhandled_event_type" &&
      result.error !== "user_not_found"
    ) {
      throw new Error(`Bursar failed to ingest billing event: ${result.error}`);
    }
    return result;
  }
}
