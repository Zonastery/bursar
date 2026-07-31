import { describe, expect, it } from "vitest";

import {
  ActiveSubscriptionError,
  BursarError,
  CapabilityNotSupportedError,
  CapReachedError,
  CheckoutCompletedError,
  CheckoutConflictError,
  CommerceError,
  CommerceNotConfiguredError,
  CommerceResourceNotFoundError,
  ConcurrencyLimitError,
  ConfigError,
  CoreBillingDataUnavailableError,
  CreditError,
  ExpressionError,
  FeatureLimitReachedError,
  FeatureNotEntitledError,
  ImportError as BursarImportError,
  InsufficientCreditsError,
  InvalidOfferQuantityError,
  LeaseExpiredError,
  LeaseNotFoundError,
  MissingPaymentMethodError,
  MissingPlanChangePolicyError,
  OperationNotAllowedError,
  PricingNotLoadedError,
  ProviderCapabilityNotSupportedError,
  ProviderSelectionError,
  QuotaExceededError,
  QuoteChangedError,
  RefundError,
  StoreError,
  UnknownOfferError,
  bursarErrorHttpStatus,
  bursarErrorPublicMessage,
  isRetryableBursarError,
  type BursarErrorCategory,
} from "../src/index.js";

interface ErrorCase {
  error: BursarError;
  code: string;
  category: BursarErrorCategory;
  status: number;
  retryable?: boolean;
}

const ERROR_CASES: ErrorCase[] = [
  { error: new BursarError("failure"), code: "BURSAR_ERROR", category: "internal", status: 500 },
  { error: new CreditError("failure"), code: "CREDIT_ERROR", category: "internal", status: 500 },
  { error: new ConfigError("failure"), code: "CONFIG_ERROR", category: "internal", status: 500 },
  {
    error: new ExpressionError("failure"),
    code: "EXPRESSION_ERROR",
    category: "internal",
    status: 500,
  },
  {
    error: new InsufficientCreditsError("failure"),
    code: "INSUFFICIENT_CREDITS",
    category: "payment_required",
    status: 402,
  },
  {
    error: new PricingNotLoadedError("failure"),
    code: "PRICING_NOT_LOADED",
    category: "unavailable",
    status: 503,
  },
  {
    error: new BursarImportError("failure"),
    code: "BURSAR_IMPORT_ERROR",
    category: "unavailable",
    status: 503,
  },
  {
    error: new StoreError("failure"),
    code: "STORE_ERROR",
    category: "unavailable",
    status: 503,
    retryable: true,
  },
  {
    error: new CapReachedError("failure"),
    code: "CAP_REACHED",
    category: "rate_limited",
    status: 429,
  },
  {
    error: new FeatureLimitReachedError("failure"),
    code: "FEATURE_LIMIT_REACHED",
    category: "rate_limited",
    status: 429,
  },
  {
    error: new RefundError("failure"),
    code: "REFUND_REJECTED",
    category: "conflict",
    status: 409,
  },
  {
    error: new ConcurrencyLimitError("failure"),
    code: "CONCURRENCY_LIMIT_REACHED",
    category: "rate_limited",
    status: 429,
  },
  {
    error: new FeatureNotEntitledError("failure"),
    code: "FEATURE_NOT_ENTITLED",
    category: "forbidden",
    status: 403,
  },
  {
    error: new OperationNotAllowedError("failure"),
    code: "OPERATION_NOT_ALLOWED",
    category: "forbidden",
    status: 403,
  },
  {
    error: new QuotaExceededError("failure"),
    code: "QUOTA_EXCEEDED",
    category: "rate_limited",
    status: 429,
  },
  {
    error: new LeaseExpiredError("failure"),
    code: "LEASE_EXPIRED",
    category: "conflict",
    status: 409,
  },
  {
    error: new LeaseNotFoundError("failure"),
    code: "LEASE_NOT_FOUND",
    category: "not_found",
    status: 404,
  },
  {
    error: new CapabilityNotSupportedError("failure"),
    code: "CAPABILITY_NOT_SUPPORTED",
    category: "unavailable",
    status: 503,
  },
  {
    error: new CommerceError("failure", "COMMERCE_ERROR"),
    code: "COMMERCE_ERROR",
    category: "internal",
    status: 500,
  },
  {
    error: new CommerceNotConfiguredError(),
    code: "COMMERCE_NOT_CONFIGURED",
    category: "unavailable",
    status: 503,
  },
  {
    error: new UnknownOfferError(),
    code: "UNKNOWN_OFFER",
    category: "invalid_request",
    status: 400,
  },
  {
    error: new InvalidOfferQuantityError("failure"),
    code: "INVALID_OFFER_QUANTITY",
    category: "invalid_request",
    status: 400,
  },
  {
    error: new ActiveSubscriptionError(),
    code: "ACTIVE_SUBSCRIPTION",
    category: "conflict",
    status: 409,
  },
  {
    error: new CheckoutConflictError(),
    code: "CHECKOUT_CONFLICT",
    category: "conflict",
    status: 409,
  },
  {
    error: new CheckoutCompletedError(),
    code: "CHECKOUT_COMPLETED",
    category: "conflict",
    status: 409,
  },
  {
    error: new CommerceResourceNotFoundError(),
    code: "COMMERCE_RESOURCE_NOT_FOUND",
    category: "not_found",
    status: 404,
  },
  {
    error: new ProviderSelectionError("failure"),
    code: "PROVIDER_SELECTION_FAILED",
    category: "unavailable",
    status: 503,
  },
  {
    error: new ProviderCapabilityNotSupportedError("provider", "capability"),
    code: "PROVIDER_CAPABILITY_NOT_SUPPORTED",
    category: "unavailable",
    status: 503,
  },
  {
    error: new QuoteChangedError({}),
    code: "QUOTE_CHANGED",
    category: "conflict",
    status: 409,
  },
  {
    error: new MissingPaymentMethodError(),
    code: "PAYMENT_METHOD_REQUIRED",
    category: "payment_required",
    status: 402,
  },
  {
    error: new MissingPlanChangePolicyError("upgrade"),
    code: "PLAN_CHANGE_POLICY_MISSING",
    category: "unavailable",
    status: 503,
  },
  {
    error: new CoreBillingDataUnavailableError(),
    code: "CORE_BILLING_DATA_UNAVAILABLE",
    category: "unavailable",
    status: 503,
    retryable: true,
  },
];

describe("Bursar error taxonomy", () => {
  it.each(ERROR_CASES)(
    "$code has stable transport metadata",
    ({ error, code, category, status, retryable }) => {
      expect(error).toBeInstanceOf(BursarError);
      expect(error.code).toBe(code);
      expect(error.category).toBe(category);
      expect(bursarErrorHttpStatus(error)).toBe(status);
      expect(bursarErrorPublicMessage(error)).not.toContain("failure");
      expect(isRetryableBursarError(error)).toBe(retryable ?? false);
    },
  );
});
