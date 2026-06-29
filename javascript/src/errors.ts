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
