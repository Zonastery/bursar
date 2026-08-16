import pRetry from "p-retry";
import { z } from "zod";

import { isRetryableBursarError } from "./errors.js";

const MAX_TIMER_MS = 2_147_483_647;

const abortSignalSchema = z.custom<AbortSignal>(
  (value) =>
    z
      .object({
        aborted: z.boolean(),
        addEventListener: z.function(),
        removeEventListener: z.function(),
        throwIfAborted: z.function(),
      })
      .safeParse(value).success,
  "signal must be an AbortSignal",
);

export interface BursarRetryOptions {
  /** Total attempts, including the first call. Defaults to 3. */
  maxAttempts?: number;
  /** Initial exponential-backoff delay. Defaults to 250 ms. */
  baseDelayMs?: number;
  /** Maximum delay between attempts. Defaults to 2 seconds. */
  maxDelayMs?: number;
  /** Exponential-backoff multiplier. Defaults to 2. */
  factor?: number;
  /** Randomize backoff to prevent a thundering herd. Defaults to true. */
  jitter?: boolean;
  /** Maximum elapsed retry budget. Defaults to 30 seconds. */
  maxElapsedMs?: number;
  /** Abort retries and pending backoff when this signal is aborted. */
  signal?: AbortSignal;
  /** Allow Node.js to exit while waiting to retry. Defaults to false. */
  unref?: boolean;
  /**
   * Override the default `error.retryable` decision. This does not make an
   * operation idempotent; mutation retries must reuse their idempotency key.
   */
  shouldRetry?: (error: Error) => boolean | Promise<boolean>;
  /** Observer invoked immediately before each retry is scheduled. */
  onRetry?: (error: Error, nextAttempt: number, delayMs: number) => void | Promise<void>;
}

function finiteNumber(
  value: number,
  name: string,
  minimum: number,
  maximum = Number.MAX_VALUE,
): number {
  if (!Number.isFinite(value) || value < minimum || value > maximum) {
    throw new RangeError(`${name} must be a finite number between ${minimum} and ${maximum}`);
  }
  return value;
}

/**
 * Execute an operation with bounded, cancellable exponential backoff.
 *
 * Retries are deliberately opt-in at the error level: by default only typed
 * Bursar errors whose `retryable` flag is true are attempted again. The
 * operation must be read-only or idempotent because a transport failure can
 * make a mutation's commit outcome indeterminate.
 */
export async function retryBursarOperation<T>(
  operation: () => PromiseLike<T> | T,
  options: BursarRetryOptions = {},
): Promise<T> {
  if (!z.function().safeParse(operation).success) {
    throw new TypeError("operation must be a function");
  }
  if (options.jitter !== undefined && !z.boolean().safeParse(options.jitter).success) {
    throw new TypeError("jitter must be a boolean");
  }
  if (options.unref !== undefined && !z.boolean().safeParse(options.unref).success) {
    throw new TypeError("unref must be a boolean");
  }
  if (options.shouldRetry !== undefined && !z.function().safeParse(options.shouldRetry).success) {
    throw new TypeError("shouldRetry must be a function");
  }
  if (options.onRetry !== undefined && !z.function().safeParse(options.onRetry).success) {
    throw new TypeError("onRetry must be a function");
  }
  if (options.signal !== undefined && !abortSignalSchema.safeParse(options.signal).success) {
    throw new TypeError("signal must be an AbortSignal");
  }

  const maxAttempts = options.maxAttempts ?? 3;
  if (!Number.isSafeInteger(maxAttempts) || maxAttempts < 1) {
    throw new RangeError("maxAttempts must be a positive safe integer");
  }

  const baseDelayMs = finiteNumber(options.baseDelayMs ?? 250, "baseDelayMs", 0, MAX_TIMER_MS);
  const maxDelayMs = finiteNumber(
    options.maxDelayMs ?? Math.max(2_000, baseDelayMs),
    "maxDelayMs",
    0,
    MAX_TIMER_MS,
  );
  if (maxDelayMs < baseDelayMs) {
    throw new RangeError("maxDelayMs must be greater than or equal to baseDelayMs");
  }
  const factor = finiteNumber(options.factor ?? 2, "factor", Number.EPSILON);
  const maxElapsedMs = finiteNumber(
    options.maxElapsedMs ?? 30_000,
    "maxElapsedMs",
    0,
    MAX_TIMER_MS,
  );

  return pRetry(operation, {
    retries: maxAttempts - 1,
    minTimeout: baseDelayMs,
    maxTimeout: maxDelayMs,
    factor,
    randomize: options.jitter ?? true,
    maxRetryTime: maxElapsedMs,
    signal: options.signal,
    unref: options.unref ?? false,
    shouldRetry: async ({ error, attemptNumber, retryDelay }) => {
      const retry = options.shouldRetry
        ? await options.shouldRetry(error)
        : isRetryableBursarError(error);
      if (!retry) return false;
      await options.onRetry?.(error, attemptNumber + 1, retryDelay);
      return true;
    },
  });
}
