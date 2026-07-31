"""Bursar error classes — mirrors JS SDK's ``errors.ts``.

All error classes used across the SDK, consolidated in one place.
"""

from __future__ import annotations

from typing import Any, Literal

BursarErrorCategory = Literal[
    "invalid_request",
    "payment_required",
    "forbidden",
    "not_found",
    "conflict",
    "rate_limited",
    "unavailable",
    "internal",
]


class BursarError(Exception):
    """Base exception for all Bursar errors."""

    code = "BURSAR_ERROR"
    category: BursarErrorCategory = "internal"
    retryable = False


class ConfigError(BursarError):
    """Raised when a Bursar configuration is invalid."""

    code = "CONFIG_ERROR"

    def __init__(
        self,
        message: str | None = None,
        *,
        validation_error: Any | None = None,
    ) -> None:
        super().__init__(
            str(validation_error) if validation_error is not None else message or "invalid Bursar configuration"
        )
        self.validation_error = validation_error

    def errors(self) -> list[dict[str, Any]]:
        if self.validation_error is None:
            return [{"type": "invalid_config", "loc": (), "msg": str(self), "input": None}]
        return self.validation_error.errors(include_url=False)


class ExpressionError(BursarError):
    """Raised on invalid or unsafe expressions."""

    code = "EXPRESSION_ERROR"


class CreditError(BursarError):
    """Base for credit-domain admission and settlement failures."""

    code = "CREDIT_ERROR"


class PricingNotLoadedError(CreditError):
    """Raised when ``deduct()`` is called before pricing is loaded."""

    code = "PRICING_NOT_LOADED"
    category: BursarErrorCategory = "unavailable"


class StoreError(BursarError):
    """Base exception for store-level errors (connection, timeout, etc.)."""

    code = "STORE_ERROR"
    category: BursarErrorCategory = "unavailable"
    retryable = True


class InsufficientCreditsError(CreditError):
    """Raised when a user does not have enough credits for an operation."""

    code = "INSUFFICIENT_CREDITS"
    category: BursarErrorCategory = "payment_required"


class CapReachedError(StoreError):
    """Raised when a deduction would exceed a configured ``deny`` spend cap."""

    code = "CAP_REACHED"
    category: BursarErrorCategory = "rate_limited"
    retryable = False


class FeatureLimitReachedError(StoreError):
    """Raised when a call would exceed a configured ``deny`` feature-limit."""

    code = "FEATURE_LIMIT_REACHED"
    category: BursarErrorCategory = "rate_limited"
    retryable = False


class FeatureNotEntitledError(CreditError):
    """Raised when an operation requires a plan feature the user does not have."""

    code = "FEATURE_NOT_ENTITLED"
    category: BursarErrorCategory = "forbidden"


class OperationNotAllowedError(CreditError):
    """Raised when a user's plan does not allow the requested operation."""

    code = "OPERATION_NOT_ALLOWED"
    category: BursarErrorCategory = "forbidden"


class QuotaExceededError(CreditError):
    """Raised when an operation would exceed a blocking usage quota."""

    code = "QUOTA_EXCEEDED"
    category: BursarErrorCategory = "rate_limited"


class ConcurrencyLimitError(CreditError):
    """Raised when a ``reserve`` would exceed an operation's ``max_concurrent`` leases."""

    code = "CONCURRENCY_LIMIT_REACHED"
    category: BursarErrorCategory = "rate_limited"


class LeaseExpiredError(CreditError):
    """Raised when settling/renewing a lease whose TTL has already elapsed."""

    code = "LEASE_EXPIRED"
    category: BursarErrorCategory = "conflict"


class LeaseNotFoundError(CreditError):
    """Raised when a lease id does not exist, belongs to another user, or was released."""

    code = "LEASE_NOT_FOUND"
    category: BursarErrorCategory = "not_found"


class RefundError(StoreError):
    """Raised when a refund is invalid (over-refund, duplicate, wrong type)."""

    code = "REFUND_REJECTED"
    category: BursarErrorCategory = "conflict"
    retryable = False


class CapabilityNotSupportedError(StoreError):
    """Raised when a store does not implement an optional capability."""

    code = "CAPABILITY_NOT_SUPPORTED"
    retryable = False


class BursarImportError(BursarError):
    """Raised when an optional dependency is missing (mirrors JS ImportError)."""

    code = "BURSAR_IMPORT_ERROR"
    category: BursarErrorCategory = "unavailable"


def is_retryable_bursar_error(error: BaseException) -> bool:
    """Return whether retrying the failed Bursar operation can be useful."""

    return isinstance(error, BursarError) and error.retryable


def bursar_error_http_status(error: BursarError) -> int:
    """Project a Bursar failure category to its conventional HTTP status."""

    return {
        "invalid_request": 400,
        "payment_required": 402,
        "forbidden": 403,
        "not_found": 404,
        "conflict": 409,
        "rate_limited": 429,
        "unavailable": 503,
        "internal": 500,
    }[error.category]


def bursar_error_public_message(error: BursarError) -> str:
    """Return safe default copy for exposing a Bursar failure to an end user."""

    return {
        "invalid_request": "The billing request is invalid.",
        "payment_required": "Payment is required to continue.",
        "forbidden": "Your current plan does not allow this operation.",
        "not_found": "The requested billing resource was not found.",
        "conflict": "The request conflicts with the current billing state.",
        "rate_limited": "A billing or usage limit has been reached.",
        "unavailable": "Billing service is temporarily unavailable. Please try again.",
        "internal": "Billing service is temporarily unavailable. Please try again.",
    }[error.category]
