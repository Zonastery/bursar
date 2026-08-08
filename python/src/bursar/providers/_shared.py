from __future__ import annotations

from typing import TYPE_CHECKING

from bursar.billing.types import BillingEvent, BillingEventResult, BillingSubscriptionStatus
from bursar.errors import BursarError, StoreUnavailableError
from bursar.retry import BursarRetryOptions, retry_bursar_operation

if TYPE_CHECKING:
    from bursar.billing.contracts import BillingEventSink


class _BillingClaimBusyError(Exception):
    pass


def require_provider_string(value: object, field: str) -> str:
    """Return a non-empty provider value without manufacturing an identifier."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def optional_provider_string(value: object, field: str) -> str | None:
    """Validate an optional provider string without manufacturing a value."""

    if value is None:
        return None
    return require_provider_string(value, field)


def require_minor_units(value: object, field: str, *, positive: bool = False) -> int:
    """Validate an integral provider amount before it crosses the billing boundary."""

    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    if isinstance(value, int):
        amount = value
    elif isinstance(value, str) and value.isascii() and value.isdigit():
        amount = int(value)
    else:
        raise ValueError(f"{field} must be an integer")
    if amount < (1 if positive else 0):
        qualifier = "positive" if positive else "non-negative"
        raise ValueError(f"{field} must be {qualifier}")
    return amount


def require_currency(value: object, field: str) -> str:
    """Validate and normalize an ISO-style three-letter currency code."""

    if not isinstance(value, str) or len(value) != 3 or not value.isascii() or not value.isalpha():
        raise ValueError(f"{field} must be a three-letter currency code")
    return value.upper()


def call_billing_event_sink(sink: BillingEventSink, event: BillingEvent) -> BillingEventResult:
    """Dispatch a billing event and raise on unexpected failures."""

    def attempt() -> BillingEventResult:
        result = sink.ingest_billing_event(event)
        if result.error == "claim_busy":
            raise _BillingClaimBusyError
        return result

    try:
        result = retry_bursar_operation(
            attempt,
            retry_options=BursarRetryOptions(
                max_attempts=7,
                base_delay_seconds=0.025,
                max_delay_seconds=0.8,
                max_elapsed_seconds=5.0,
                should_retry=lambda error: isinstance(error, _BillingClaimBusyError),
            ),
        )
    except _BillingClaimBusyError as error:
        raise StoreUnavailableError(
            "Billing event claim remained busy",
            cause=error,
            details={"reason": "claim_busy"},
        ) from error

    if result.error == "claim_failed_retry":
        raise StoreUnavailableError(
            "Billing event claim could not be acquired",
            details={"reason": result.error},
        )
    if not result.handled and result.error not in ("unhandled_event_type", "user_not_found"):
        raise BursarError(
            "Bursar failed to ingest the billing event",
            details={"reason": result.error or "unknown"},
        )
    return result


def parse_subscription_status(raw: object) -> BillingSubscriptionStatus | None:
    """Parse an exact provider status without admitting unknown database values."""
    if not isinstance(raw, str):
        return None
    try:
        return BillingSubscriptionStatus(raw)
    except ValueError:
        return None
