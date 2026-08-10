import { Decimal } from "decimal.js";
import { describe, expect, it } from "vitest";

import { raiseDeductError, raiseLeaseError } from "../src/credits/service-errors.js";
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
} from "../src/errors.js";

describe("raiseLeaseError", () => {
  const cases = [
    ["max_concurrent_reached", ConcurrencyLimitError],
    ["quota_exceeded", QuotaExceededError],
    ["feature_not_entitled", FeatureNotEntitledError],
    ["operation_not_allowed", OperationNotAllowedError],
    ["insufficient_headroom", InsufficientCreditsError],
    ["insufficient_credits", InsufficientCreditsError],
    ["expired_lease", LeaseExpiredError],
    ["missing_lease", LeaseNotFoundError],
    ["released_lease", LeaseNotFoundError],
    ["settled_lease", LeaseNotFoundError],
    ["missing_quota_measure", ConfigError],
    ["invalid_measure", ConfigError],
    ["invalid_request", RangeError],
    ["unexpected_store_code", StoreError],
  ] as const;

  it.each(cases)("maps store code %s", (code, type) => {
    expect(() => raiseLeaseError(code, "user-1", new Decimal(5))).toThrow(type);
  });

  it("mentions the user and amount in insufficient-credit failures", () => {
    expect(() => raiseLeaseError("insufficient_credits", "user-1", new Decimal(5))).toThrow(
      /user-1/,
    );
  });
});

describe("raiseDeductError", () => {
  const cases = [
    ["quota_exceeded", QuotaExceededError],
    ["feature_not_entitled", FeatureNotEntitledError],
    ["operation_not_allowed", OperationNotAllowedError],
    ["insufficient_headroom", InsufficientCreditsError],
    ["insufficient_credits", InsufficientCreditsError],
    ["missing_quota_measure", ConfigError],
    ["invalid_measure", ConfigError],
    ["invalid_amount", RangeError],
    ["invalid_request", RangeError],
    ["unexpected_store_code", StoreError],
  ] as const;

  it.each(cases)("maps store code %s", (code, type) => {
    expect(() => raiseDeductError(code, "user-1", new Decimal(1))).toThrow(type);
  });
});
