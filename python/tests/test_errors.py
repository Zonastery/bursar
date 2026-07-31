from __future__ import annotations

import pytest

from bursar import (
    ActiveSubscriptionError,
    BursarError,
    BursarImportError,
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
    bursar_error_http_status,
    bursar_error_public_message,
    is_retryable_bursar_error,
)

ERROR_CASES = [
    (BursarError("failure"), "BURSAR_ERROR", "internal", 500, False),
    (CreditError("failure"), "CREDIT_ERROR", "internal", 500, False),
    (ConfigError("failure"), "CONFIG_ERROR", "internal", 500, False),
    (ExpressionError("failure"), "EXPRESSION_ERROR", "internal", 500, False),
    (InsufficientCreditsError("failure"), "INSUFFICIENT_CREDITS", "payment_required", 402, False),
    (PricingNotLoadedError("failure"), "PRICING_NOT_LOADED", "unavailable", 503, False),
    (BursarImportError("failure"), "BURSAR_IMPORT_ERROR", "unavailable", 503, False),
    (StoreError("failure"), "STORE_ERROR", "unavailable", 503, True),
    (CapReachedError("failure"), "CAP_REACHED", "rate_limited", 429, False),
    (FeatureLimitReachedError("failure"), "FEATURE_LIMIT_REACHED", "rate_limited", 429, False),
    (RefundError("failure"), "REFUND_REJECTED", "conflict", 409, False),
    (ConcurrencyLimitError("failure"), "CONCURRENCY_LIMIT_REACHED", "rate_limited", 429, False),
    (FeatureNotEntitledError("failure"), "FEATURE_NOT_ENTITLED", "forbidden", 403, False),
    (OperationNotAllowedError("failure"), "OPERATION_NOT_ALLOWED", "forbidden", 403, False),
    (QuotaExceededError("failure"), "QUOTA_EXCEEDED", "rate_limited", 429, False),
    (LeaseExpiredError("failure"), "LEASE_EXPIRED", "conflict", 409, False),
    (LeaseNotFoundError("failure"), "LEASE_NOT_FOUND", "not_found", 404, False),
    (CapabilityNotSupportedError("failure"), "CAPABILITY_NOT_SUPPORTED", "unavailable", 503, False),
    (CommerceError("failure"), "COMMERCE_ERROR", "internal", 500, False),
    (CommerceNotConfiguredError(), "COMMERCE_NOT_CONFIGURED", "unavailable", 503, False),
    (UnknownOfferError(), "UNKNOWN_OFFER", "invalid_request", 400, False),
    (InvalidOfferQuantityError("failure"), "INVALID_OFFER_QUANTITY", "invalid_request", 400, False),
    (ActiveSubscriptionError(), "ACTIVE_SUBSCRIPTION", "conflict", 409, False),
    (CheckoutConflictError(), "CHECKOUT_CONFLICT", "conflict", 409, False),
    (CheckoutCompletedError(), "CHECKOUT_COMPLETED", "conflict", 409, False),
    (CommerceResourceNotFoundError(), "COMMERCE_RESOURCE_NOT_FOUND", "not_found", 404, False),
    (ProviderSelectionError("failure"), "PROVIDER_SELECTION_FAILED", "unavailable", 503, False),
    (
        ProviderCapabilityNotSupportedError("provider", "capability"),
        "PROVIDER_CAPABILITY_NOT_SUPPORTED",
        "unavailable",
        503,
        False,
    ),
    (QuoteChangedError({}), "QUOTE_CHANGED", "conflict", 409, False),
    (MissingPaymentMethodError(), "PAYMENT_METHOD_REQUIRED", "payment_required", 402, False),
    (MissingPlanChangePolicyError("upgrade"), "PLAN_CHANGE_POLICY_MISSING", "unavailable", 503, False),
    (
        CoreBillingDataUnavailableError(),
        "CORE_BILLING_DATA_UNAVAILABLE",
        "unavailable",
        503,
        True,
    ),
]


@pytest.mark.parametrize(("error", "code", "category", "status", "retryable"), ERROR_CASES)
def test_bursar_error_taxonomy(
    error: BursarError,
    code: str,
    category: str,
    status: int,
    retryable: bool,
) -> None:
    assert isinstance(error, BursarError)
    assert error.code == code
    assert error.category == category
    assert bursar_error_http_status(error) == status
    assert "failure" not in bursar_error_public_message(error)
    assert is_retryable_bursar_error(error) is retryable
