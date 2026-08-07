import pRetry from "p-retry";

import type { BillingEvent, BillingEventResult } from "../billing/index.js";
import type { BillingEventSink } from "../bursar.js";
import { BursarError, StoreUnavailableError } from "../errors.js";

class BillingClaimBusyError extends Error {
  override readonly name = "BillingClaimBusyError";
}

export function requireProviderString(value: unknown, field: string): string {
  if (typeof value !== "string" || value.trim().length === 0) {
    throw new TypeError(`${field} must be a non-empty string`);
  }
  return value.trim();
}

export function requireMinorUnits(value: unknown, field: string, positive = false): number {
  const amount =
    typeof value === "number"
      ? value
      : typeof value === "string" && /^\d+$/.test(value)
        ? Number(value)
        : Number.NaN;
  if (!Number.isSafeInteger(amount) || amount < (positive ? 1 : 0)) {
    throw new TypeError(
      `${field} must be a ${positive ? "positive" : "non-negative"} safe integer`,
    );
  }
  return amount;
}

export function requireCurrency(value: unknown, field: string): string {
  if (typeof value !== "string" || !/^[A-Za-z]{3}$/.test(value)) {
    throw new TypeError(`${field} must be a three-letter currency code`);
  }
  return value.toUpperCase();
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
  let result: BillingEventResult;
  try {
    result = await pRetry(
      async () => {
        const attempted = await sink.ingestBillingEvent(event);
        if (attempted.error === "claim_busy") throw new BillingClaimBusyError();
        return attempted;
      },
      {
        retries: 6,
        minTimeout: 25,
        maxTimeout: 800,
        factor: 2,
        randomize: true,
        maxRetryTime: 5_000,
        shouldRetry: ({ error }) => error instanceof BillingClaimBusyError,
      },
    );
  } catch (cause) {
    if (cause instanceof BillingClaimBusyError) {
      throw new StoreUnavailableError("Billing event claim remained busy", {
        cause,
        details: { reason: "claim_busy" },
      });
    }
    throw cause;
  }

  if (result.error === "claim_failed_retry") {
    throw new StoreUnavailableError("Billing event claim could not be acquired", {
      details: { reason: result.error },
    });
  }
  if (
    !result.handled &&
    result.error !== "unhandled_event_type" &&
    result.error !== "user_not_found"
  ) {
    throw new BursarError("Bursar failed to ingest the billing event", {
      details: { reason: result.error ?? "unknown" },
    });
  }
  return result;
}
