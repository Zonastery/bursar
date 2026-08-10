import pRetry from "p-retry";

import type { BillingEvent, BillingEventResult } from "../billing/index.js";
import type { BillingEventSink } from "../bursar.js";
import { BursarError, StoreUnavailableError } from "../errors.js";

class BillingClaimBusyError extends Error {
  override readonly name = "BillingClaimBusyError";
}

const ACKNOWLEDGED_BILLING_EVENT_ERRORS = new Set([
  "unhandled_event_type",
  "account_not_found",
  "invalid_request",
  "idempotency_conflict",
  "max_retries_exceeded",
]);

export function requireProviderString(value: unknown, field: string): string {
  if (typeof value !== "string" || value.trim().length === 0) {
    throw new TypeError(`${field} must be a non-empty string`);
  }
  return value.trim();
}

export function optionalProviderString(value: unknown, field: string): string | undefined {
  if (value === null || value === undefined) return undefined;
  return requireProviderString(value, field);
}

export function optionalProviderBoolean(value: unknown, field: string): boolean | undefined {
  if (value === null || value === undefined) return undefined;
  if (typeof value !== "boolean") {
    throw new TypeError(`${field} must be a boolean`);
  }
  return value;
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
 * Wrapper around the facade event sink that throws on retryable or unexpected
 * failures. Permanent claim outcomes are returned so the provider can
 * acknowledge them without scheduling another delivery.
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
  if (!result.handled && !ACKNOWLEDGED_BILLING_EVENT_ERRORS.has(result.error ?? "")) {
    throw new BursarError("Bursar failed to ingest the billing event", {
      details: { reason: result.error ?? "unknown" },
    });
  }
  return result;
}
