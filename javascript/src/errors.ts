export class ConfigError extends Error {
  override readonly name = "ConfigError";
}

export class ExpressionError extends Error {
  override readonly name = "ExpressionError";
}

export class InsufficientCreditsError extends Error {
  override readonly name = "InsufficientCreditsError";
}

export class PricingNotLoadedError extends Error {
  override readonly name = "PricingNotLoadedError";
}

export class ImportError extends Error {
  override readonly name = "ImportError";
}

export class StoreError extends Error {
  override readonly name = "StoreError";
}

export class CapReachedError extends Error {
  override readonly name = "CapReachedError";
}

/**
 * Raised when a call would exceed a configured `deny` feature-limit.
 *
 * Stores return `error: "feature_limit_reached"` on the result object rather
 * than throwing; the manager maps that code to this exception — mirrors
 * `CapReachedError`.
 */
export class FeatureLimitReachedError extends Error {
  override readonly name = "FeatureLimitReachedError";
}

export class RefundError extends Error {
  override readonly name = "RefundError";
}

export class ConcurrencyLimitError extends Error {
  override readonly name = "ConcurrencyLimitError";
}

export class FeatureNotEntitledError extends Error {
  override readonly name = "FeatureNotEntitledError";
}

export class LeaseExpiredError extends Error {
  override readonly name = "LeaseExpiredError";
}

export class LeaseNotFoundError extends Error {
  override readonly name = "LeaseNotFoundError";
}

/**
 * Thrown by the default (concrete) implementation of an optional `CreditStore`
 * capability (analytics, transaction listing, teams — WS8) when a custom store
 * subclass does not override it.
 */
export class CapabilityNotSupportedError extends Error {
  override readonly name = "CapabilityNotSupportedError";
}
