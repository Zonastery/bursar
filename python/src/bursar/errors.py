"""Bursar error classes — mirrors JS SDK's ``errors.ts``.

All error classes used across the SDK, consolidated in one place.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Literal, TypeGuard

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

    def __init__(
        self,
        message: str,
        *,
        cause: BaseException | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.details = MappingProxyType(dict(details)) if details else None
        self._cause = cause

    @property
    def cause(self) -> BaseException | None:
        """Return the preserved native failure, including ``raise from`` causes."""

        return self._cause if self._cause is not None else self.__cause__

    def to_dict(self) -> dict[str, Any]:
        """Return a stable, safe representation for logs and protocol adapters."""

        result: dict[str, Any] = {
            "name": type(self).__name__,
            "message": str(self),
            "code": self.code,
            "category": self.category,
            "retryable": self.retryable,
        }
        if self.details is not None:
            result["details"] = dict(self.details)
        if isinstance(self, StoreError):
            result["indeterminate"] = self.indeterminate
        return result

    def to_json(self) -> dict[str, Any]:
        """Alias matching the JavaScript SDK's serializable error contract."""

        return self.to_dict()


class ConfigError(BursarError):
    """Raised when a Bursar configuration is invalid."""

    code = "CONFIG_ERROR"

    def __init__(
        self,
        message: str | None = None,
        *,
        validation_error: Any | None = None,
        cause: BaseException | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(
            str(validation_error) if validation_error is not None else message or "invalid Bursar configuration",
            cause=cause,
            details=details,
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


class BillingError(BursarError):
    """Base for billing and payment orchestration failures."""

    code = "BILLING_ERROR"


class AutoRechargeNotConfiguredError(BillingError):
    code = "AUTO_RECHARGE_NOT_CONFIGURED"
    category: BursarErrorCategory = "unavailable"

    def __init__(self) -> None:
        super().__init__("Auto-recharge is not configured for this catalog")


class AutoRechargeDisabledError(BillingError):
    code = "AUTO_RECHARGE_DISABLED"
    category: BursarErrorCategory = "conflict"

    def __init__(self) -> None:
        super().__init__("Auto-recharge is disabled for this account")


class PaymentMethodRequiredError(BillingError):
    code = "PAYMENT_METHOD_REQUIRED"
    category: BursarErrorCategory = "payment_required"

    def __init__(self) -> None:
        super().__init__("A saved payment method is required")


class ProviderCapabilityNotSupportedError(BillingError):
    code = "PROVIDER_CAPABILITY_NOT_SUPPORTED"
    category: BursarErrorCategory = "unavailable"

    def __init__(self, provider: str, capability: str) -> None:
        self.provider = provider
        self.capability = capability
        super().__init__(
            f"Payment provider {provider!r} does not support {capability!r}",
            details={"provider": provider, "capability": capability},
        )


class ProviderResponseError(BillingError):
    """Raised when a payment provider violates its documented response contract."""

    code = "PROVIDER_RESPONSE_INVALID"
    category: BursarErrorCategory = "unavailable"

    def __init__(
        self,
        provider: str,
        operation: str,
        *,
        cause: BaseException | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.provider = provider
        self.operation = operation
        super().__init__(
            f"Payment provider {provider!r} returned an invalid response for {operation!r}",
            cause=cause,
            details={**(details or {}), "provider": provider, "operation": operation},
        )


class CatalogNotLoadedError(CreditError):
    """Raised when an operation requires a catalog that is not loaded."""

    code = "CATALOG_NOT_LOADED"
    category: BursarErrorCategory = "unavailable"


class StoreError(BursarError):
    """Base exception for store-level errors (connection, timeout, etc.)."""

    code = "STORE_ERROR"
    category: BursarErrorCategory = "unavailable"

    def __init__(
        self,
        message: str,
        *,
        retryable: bool | None = None,
        indeterminate: bool = False,
        cause: BaseException | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message, cause=cause, details=details)
        self.retryable = type(self).retryable if retryable is None else retryable
        self.indeterminate = indeterminate


class StoreUnavailableError(StoreError):
    """A transient store or transport failure suitable for bounded retry."""

    code = "STORE_UNAVAILABLE"
    retryable = True


class StoreTimeoutError(StoreUnavailableError):
    """A store operation exceeded its configured deadline."""

    code = "STORE_TIMEOUT"


class StoreClosedError(StoreError, RuntimeError):
    """The application attempted to use a store after closing it."""

    code = "STORE_CLOSED"
    category: BursarErrorCategory = "conflict"


class InsufficientCreditsError(CreditError):
    """Raised when a user does not have enough credits for an operation."""

    code = "INSUFFICIENT_CREDITS"
    category: BursarErrorCategory = "payment_required"


class CapReachedError(StoreError):
    """Raised when a deduction would exceed a configured ``deny`` spend cap."""

    code = "CAP_REACHED"
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


class CapabilityNotConfiguredError(BursarError):
    """A facade capability was intentionally omitted from SDK composition."""

    code = "CAPABILITY_NOT_CONFIGURED"
    category: BursarErrorCategory = "unavailable"

    def __init__(self, capability: str) -> None:
        self.capability = capability
        super().__init__(
            f"Bursar {capability} capability is not configured",
            details={"capability": capability},
        )


class BursarImportError(BursarError):
    """Raised when an optional dependency is missing (mirrors JS ImportError)."""

    code = "BURSAR_IMPORT_ERROR"
    category: BursarErrorCategory = "unavailable"


def is_bursar_error(error: object) -> TypeGuard[BursarError]:
    """Return whether an exception belongs to the Bursar error contract."""

    return isinstance(error, BursarError)


def is_retryable_bursar_error(error: BaseException) -> bool:
    """Return whether retrying the failed Bursar operation can be useful."""

    if not is_bursar_error(error):
        return False
    return error.retryable


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
