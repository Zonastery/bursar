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
