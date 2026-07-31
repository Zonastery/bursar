/**
 * Transport-neutral failure categories for SaaS application boundaries.
 *
 * The category is intentionally coarser than `code`: applications can use it
 * for protocol behavior while retaining the stable code for product-specific
 * copy and analytics.
 */
export type BursarErrorCategory =
  | "invalid_request"
  | "payment_required"
  | "forbidden"
  | "not_found"
  | "conflict"
  | "rate_limited"
  | "unavailable"
  | "internal";

/** Base error for all Bursar SDK failures. */
export class BursarError extends Error {
  override readonly name: string = "BursarError";
  readonly code: string = "BURSAR_ERROR";
  readonly category: BursarErrorCategory = "internal";
  readonly retryable: boolean = false;
}

/** Base error for credit-domain admission and settlement failures. */
export class CreditError extends BursarError {
  override readonly name: string = "CreditError";
  override readonly code: string = "CREDIT_ERROR";
}

export class ConfigError extends BursarError {
  override readonly name = "ConfigError";
  override readonly code = "CONFIG_ERROR";

  constructor(
    message: string,
    readonly validationErrors: readonly unknown[] = [],
  ) {
    super(message);
  }
}

export class ExpressionError extends BursarError {
  override readonly name = "ExpressionError";
  override readonly code = "EXPRESSION_ERROR";
}

export class InsufficientCreditsError extends CreditError {
  override readonly name = "InsufficientCreditsError";
  override readonly code = "INSUFFICIENT_CREDITS";
  override readonly category = "payment_required" as const;
}

export class PricingNotLoadedError extends CreditError {
  override readonly name = "PricingNotLoadedError";
  override readonly code = "PRICING_NOT_LOADED";
  override readonly category = "unavailable" as const;
}

export class ImportError extends BursarError {
  override readonly name = "ImportError";
  override readonly code = "BURSAR_IMPORT_ERROR";
  override readonly category = "unavailable" as const;
}

export class StoreError extends BursarError {
  override readonly name: string = "StoreError";
  override readonly code: string = "STORE_ERROR";
  override readonly category: BursarErrorCategory = "unavailable";

  /**
   * Store failures are retryable by default. Domain/capability subclasses
   * override this so hosts never have to reconstruct Bursar's error taxonomy.
   */
  readonly retryable: boolean = true;
}

export class CapReachedError extends StoreError {
  override readonly name = "CapReachedError";
  override readonly code = "CAP_REACHED";
  override readonly category = "rate_limited" as const;
  override readonly retryable = false;
}

/**
 * Raised when a call would exceed a configured `deny` feature-limit.
 *
 * Stores return `error: "feature_limit_reached"` on the result object rather
 * than throwing; the manager maps that code to this exception — mirrors
 * `CapReachedError`.
 */
export class FeatureLimitReachedError extends StoreError {
  override readonly name = "FeatureLimitReachedError";
  override readonly code = "FEATURE_LIMIT_REACHED";
  override readonly category = "rate_limited" as const;
  override readonly retryable = false;
}

export class RefundError extends StoreError {
  override readonly name = "RefundError";
  override readonly code = "REFUND_REJECTED";
  override readonly category = "conflict" as const;
  override readonly retryable = false;
}

export class ConcurrencyLimitError extends CreditError {
  override readonly name = "ConcurrencyLimitError";
  override readonly code = "CONCURRENCY_LIMIT_REACHED";
  override readonly category = "rate_limited" as const;
}

export class FeatureNotEntitledError extends CreditError {
  override readonly name = "FeatureNotEntitledError";
  override readonly code = "FEATURE_NOT_ENTITLED";
  override readonly category = "forbidden" as const;
}

export class OperationNotAllowedError extends CreditError {
  override readonly name = "OperationNotAllowedError";
  override readonly code = "OPERATION_NOT_ALLOWED";
  override readonly category = "forbidden" as const;
}

export class QuotaExceededError extends CreditError {
  override readonly name = "QuotaExceededError";
  override readonly code = "QUOTA_EXCEEDED";
  override readonly category = "rate_limited" as const;
}

export class LeaseExpiredError extends CreditError {
  override readonly name = "LeaseExpiredError";
  override readonly code = "LEASE_EXPIRED";
  override readonly category = "conflict" as const;
}

export class LeaseNotFoundError extends CreditError {
  override readonly name = "LeaseNotFoundError";
  override readonly code = "LEASE_NOT_FOUND";
  override readonly category = "not_found" as const;
}

/**
 * Thrown by the default (concrete) implementation of an optional `CreditStore`
 * capability (analytics, ledger listing, teams — WS8) when a custom store
 * subclass does not override it.
 */
export class CapabilityNotSupportedError extends StoreError {
  override readonly name = "CapabilityNotSupportedError";
  override readonly code = "CAPABILITY_NOT_SUPPORTED";
  override readonly retryable = false;
}

/** Return whether retrying the failed Bursar operation can be useful. */
export function isRetryableBursarError(error: unknown): boolean {
  return error instanceof BursarError && error.retryable;
}

/** Project a Bursar failure category to its conventional HTTP status. */
export function bursarErrorHttpStatus(error: BursarError): number {
  switch (error.category) {
    case "invalid_request":
      return 400;
    case "payment_required":
      return 402;
    case "forbidden":
      return 403;
    case "not_found":
      return 404;
    case "conflict":
      return 409;
    case "rate_limited":
      return 429;
    case "unavailable":
      return 503;
    case "internal":
      return 500;
  }
}

/** Return safe default copy for exposing a Bursar failure to an end user. */
export function bursarErrorPublicMessage(error: BursarError): string {
  switch (error.category) {
    case "invalid_request":
      return "The billing request is invalid.";
    case "payment_required":
      return "Payment is required to continue.";
    case "forbidden":
      return "Your current plan does not allow this operation.";
    case "not_found":
      return "The requested billing resource was not found.";
    case "conflict":
      return "The request conflicts with the current billing state.";
    case "rate_limited":
      return "A billing or usage limit has been reached.";
    case "unavailable":
    case "internal":
      return "Billing service is temporarily unavailable. Please try again.";
  }
}
