import { isRetryableBursarError } from "./errors.js";

export interface BursarRetryOptions {
  /** Total attempts, including the first call. Defaults to 3. */
  maxAttempts?: number;
  /** Initial exponential-backoff delay. Defaults to 250 ms. */
  baseDelayMs?: number;
  /** Maximum delay between attempts. Defaults to 2 seconds. */
  maxDelayMs?: number;
  /** Optional observer invoked before each retry. */
  onRetry?: (error: unknown, attempt: number, delayMs: number) => void | Promise<void>;
}

/** Execute a Bursar operation with bounded retries for SDK-classified transient failures. */
export async function retryBursarOperation<T>(
  operation: () => Promise<T>,
  options: BursarRetryOptions = {},
): Promise<T> {
  const requestedAttempts = options.maxAttempts ?? 3;
  const maxAttempts = Number.isFinite(requestedAttempts)
    ? Math.max(1, Math.trunc(requestedAttempts))
    : 3;
  const requestedBaseDelay = options.baseDelayMs ?? 250;
  const baseDelayMs = Number.isFinite(requestedBaseDelay) ? Math.max(0, requestedBaseDelay) : 250;
  const requestedMaxDelay = options.maxDelayMs ?? 2_000;
  const maxDelayMs = Number.isFinite(requestedMaxDelay)
    ? Math.max(baseDelayMs, requestedMaxDelay)
    : 2_000;

  for (let attempt = 1; ; attempt += 1) {
    try {
      return await operation();
    } catch (error) {
      if (attempt >= maxAttempts || !isRetryableBursarError(error)) throw error;
      const delayMs = Math.min(maxDelayMs, baseDelayMs * 2 ** (attempt - 1));
      await options.onRetry?.(error, attempt + 1, delayMs);
      if (delayMs > 0) await new Promise((resolve) => setTimeout(resolve, delayMs));
    }
  }
}
