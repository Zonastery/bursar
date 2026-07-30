/** Base error for all Bursar SDK failures. */
export class BursarError extends Error {
  override readonly name: string = "BursarError";
}

/** Base error for credit-domain admission and settlement failures. */
export class CreditError extends BursarError {
  override readonly name: string = "CreditError";
}

export class ConfigError extends BursarError {
  override readonly name = "ConfigError";

  constructor(
    message: string,
    readonly validationErrors: readonly unknown[] = [],
  ) {
    super(message);
  }
}

export class ExpressionError extends BursarError {
  override readonly name = "ExpressionError";
}

export class InsufficientCreditsError extends CreditError {
  override readonly name = "InsufficientCreditsError";
}

export class PricingNotLoadedError extends CreditError {
  override readonly name = "PricingNotLoadedError";
}

export class ImportError extends BursarError {
  override readonly name = "ImportError";
}

export class StoreError extends BursarError {
  override readonly name: string = "StoreError";
}

export class CapReachedError extends StoreError {
  override readonly name = "CapReachedError";
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
}

export class RefundError extends StoreError {
  override readonly name = "RefundError";
}

export class ConcurrencyLimitError extends CreditError {
  override readonly name = "ConcurrencyLimitError";
}

export class FeatureNotEntitledError extends CreditError {
  override readonly name = "FeatureNotEntitledError";
}

export class OperationNotAllowedError extends CreditError {
  override readonly name = "OperationNotAllowedError";
}

export class QuotaExceededError extends CreditError {
  override readonly name = "QuotaExceededError";
}

export class LeaseExpiredError extends CreditError {
  override readonly name = "LeaseExpiredError";
}

export class LeaseNotFoundError extends CreditError {
  override readonly name = "LeaseNotFoundError";
}

/**
 * Thrown by the default (concrete) implementation of an optional `CreditStore`
 * capability (analytics, ledger listing, teams — WS8) when a custom store
 * subclass does not override it.
 */
export class CapabilityNotSupportedError extends StoreError {
  override readonly name = "CapabilityNotSupportedError";
}
