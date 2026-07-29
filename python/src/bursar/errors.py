"""Bursar error classes — mirrors JS SDK's ``errors.ts``.

All error classes used across the SDK, consolidated in one place.
"""

from __future__ import annotations

from typing import Any


class BursarError(Exception):
    """Base exception for all Bursar errors."""


class ConfigError(BursarError):
    """Raised when a Bursar configuration is invalid."""

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


class PricingNotLoadedError(BursarError):
    """Raised when ``deduct()`` is called before pricing is loaded."""


class StoreError(BursarError):
    """Base exception for store-level errors (connection, timeout, etc.)."""


class InsufficientCreditsError(BursarError):
    """Raised when a user does not have enough credits for an operation."""


class CapReachedError(StoreError):
    """Raised when a deduction would exceed a configured ``deny`` spend cap."""


class FeatureLimitReachedError(StoreError):
    """Raised when a call would exceed a configured ``deny`` feature-limit."""


class FeatureNotEntitledError(BursarError):
    """Raised when an operation requires a plan feature the user does not have."""


class OperationNotAllowedError(BursarError):
    """Raised when a user's plan does not allow the requested operation."""


class QuotaExceededError(BursarError):
    """Raised when an operation would exceed a blocking usage quota."""


class ConcurrencyLimitError(BursarError):
    """Raised when a ``reserve`` would exceed an operation's ``max_concurrent`` leases."""


class LeaseExpiredError(BursarError):
    """Raised when settling/renewing a lease whose TTL has already elapsed."""


class LeaseNotFoundError(BursarError):
    """Raised when a lease id does not exist, belongs to another user, or was released."""


class RefundError(StoreError):
    """Raised when a refund is invalid (over-refund, duplicate, wrong type)."""


class CapabilityNotSupportedError(StoreError):
    """Raised when a store does not implement an optional capability."""


class BursarImportError(BursarError):
    """Raised when an optional dependency is missing (mirrors JS ImportError)."""


class CreditError(BursarError):
    """Coherent base for bursar credit-domain errors."""
