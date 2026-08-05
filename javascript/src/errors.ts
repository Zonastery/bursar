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

/** Structured, non-secret context that is safe to attach to an SDK error. */
export type BursarErrorDetails = Readonly<Record<string, unknown>>;

/** Options shared by all Bursar errors. */
export interface BursarErrorOptions extends ErrorOptions {
  details?: BursarErrorDetails;
}

export interface StoreErrorOptions extends BursarErrorOptions {
  indeterminate?: boolean;
  retryable?: boolean;
}

export interface SerializedBursarError {
  name: string;
  message: string;
  code: string;
  category: BursarErrorCategory;
  retryable: boolean;
  details?: BursarErrorDetails;
  indeterminate?: boolean;
}

const BURSAR_ERROR_BRAND = Symbol.for("@zonastery/bursar.error");

/** Base error for all Bursar SDK failures. */
export class BursarError extends Error {
  override readonly name: string = "BursarError";
  readonly code: string = "BURSAR_ERROR";
  readonly category: BursarErrorCategory = "internal";
  readonly retryable: boolean = false;

  readonly details?: BursarErrorDetails;

  constructor(message: string, options: BursarErrorOptions = {}) {
    super(message, options.cause === undefined ? undefined : { cause: options.cause });
    this.details = options.details ? Object.freeze({ ...options.details }) : undefined;
    Object.defineProperty(this, BURSAR_ERROR_BRAND, { value: true });
  }

  /** A predictable representation for logs and protocol adapters. */
  toJSON(): SerializedBursarError {
    return {
      name: this.name,
      message: this.message,
      code: this.code,
      category: this.category,
      retryable: this.retryable,
      ...(this.details ? { details: this.details } : {}),
    };
  }
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
    options: BursarErrorOptions = {},
  ) {
    super(message, options);
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
  override readonly retryable: boolean;

  /**
   * Whether the operation may have committed even though no result was
   * observed. Callers must reuse the same idempotency key before retrying an
   * indeterminate mutation.
   */
  readonly indeterminate: boolean;

  constructor(message: string, options: StoreErrorOptions = {}) {
    super(message, options);
    this.indeterminate = options.indeterminate ?? false;
    this.retryable = options.retryable ?? false;
  }

  override toJSON(): SerializedBursarError {
    return { ...super.toJSON(), indeterminate: this.indeterminate };
  }
}

/** A transient store/transport failure for which a bounded retry can help. */
export class StoreUnavailableError extends StoreError {
  override readonly name: string = "StoreUnavailableError";
  override readonly code: string = "STORE_UNAVAILABLE";
  override readonly retryable = true;
}

/** A store operation exceeded its configured deadline. */
export class StoreTimeoutError extends StoreUnavailableError {
  override readonly name = "StoreTimeoutError";
  override readonly code = "STORE_TIMEOUT";
}

/** The application attempted to use a store after closing it. */
export class StoreClosedError extends StoreError {
  override readonly name = "StoreClosedError";
  override readonly code = "STORE_CLOSED";
  override readonly category = "conflict" as const;
}

export class CapReachedError extends StoreError {
  override readonly name = "CapReachedError";
  override readonly code = "CAP_REACHED";
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

/**
 * Cross-package-copy-safe check for an SDK error. `instanceof` alone fails
 * when an application installs two copies of the package.
 */
export function isBursarError(error: unknown): error is BursarError {
  if (error instanceof BursarError) return true;
  return (
    typeof error === "object" &&
    error !== null &&
    (error as Record<PropertyKey, unknown>)[BURSAR_ERROR_BRAND] === true
  );
}

/** Return whether retrying the failed Bursar operation can be useful. */
export function isRetryableBursarError(error: unknown): boolean {
  return isBursarError(error) && error.retryable;
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
