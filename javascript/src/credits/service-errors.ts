import Decimal from "decimal.js";

import {
  ConcurrencyLimitError,
  ConfigError,
  FeatureNotEntitledError,
  InsufficientCreditsError,
  LeaseExpiredError,
  LeaseNotFoundError,
  OperationNotAllowedError,
  QuotaExceededError,
  StoreError,
} from "../errors.js";

export function raiseLeaseError(error: string, userId: string, amount: Decimal): never {
  switch (error) {
    case "max_concurrent_reached":
      throw new ConcurrencyLimitError(`Concurrency limit reached. user=${userId}`);
    case "quota_exceeded":
      throw new QuotaExceededError(`Usage quota exceeded. user=${userId}`);
    case "feature_not_entitled":
      throw new FeatureNotEntitledError(`Feature not entitled. user=${userId}`);
    case "operation_not_allowed":
      throw new OperationNotAllowedError(`Operation is not allowed. user=${userId}`);
    case "insufficient_headroom":
    case "insufficient_credits":
      throw new InsufficientCreditsError(
        `Insufficient credits. user=${userId}, requested=${amount}`,
      );
    case "expired_lease":
      throw new LeaseExpiredError(`Lease expired. user=${userId}`);
    case "missing_lease":
      throw new LeaseNotFoundError(`Lease not found. user=${userId}`);
    case "released_lease":
    case "settled_lease":
      throw new LeaseNotFoundError(`Lease is already finalized. user=${userId}`);
    case "missing_quota_measure":
    case "invalid_measure":
      throw new ConfigError(`Invalid quota measures for user ${userId}: ${error}`);
    case "invalid_request":
      throw new RangeError(`Invalid amount: ${amount}`);
    default:
      throw new StoreError(`Lease operation failed: ${error}. user=${userId}`);
  }
}

export function raiseDeductError(error: string, userId: string, cost: Decimal): never {
  switch (error) {
    case "quota_exceeded":
      throw new QuotaExceededError(`Usage quota exceeded. user=${userId}`);
    case "feature_not_entitled":
      throw new FeatureNotEntitledError(`Feature not entitled. user=${userId}`);
    case "operation_not_allowed":
      throw new OperationNotAllowedError(`Operation is not allowed. user=${userId}`);
    case "insufficient_headroom":
    case "insufficient_credits":
      throw new InsufficientCreditsError(`Insufficient credits. user=${userId}, requested=${cost}`);
    case "missing_quota_measure":
    case "invalid_measure":
      throw new ConfigError(`Invalid quota measures for user ${userId}: ${error}`);
    case "invalid_amount":
    case "invalid_request":
      throw new RangeError(`Invalid deduction amount: ${cost}`);
    default:
      throw new StoreError(`Credit deduction failed: ${error}. user=${userId}, requested=${cost}`);
  }
}
