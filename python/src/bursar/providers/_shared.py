from __future__ import annotations

from typing import TYPE_CHECKING

from bursar.billing.types import BillingEvent, BillingEventResult, BillingSubscriptionStatus
from bursar.errors import BursarError, StoreUnavailableError
from bursar.retry import BursarRetryOptions, retry_bursar_operation

if TYPE_CHECKING:
    from bursar.billing.contracts import BillingEventSink


class _BillingClaimBusyError(Exception):
    pass


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


def parse_status(raw: str | None) -> BillingSubscriptionStatus | None:
    """Safely parse a status string into a BillingSubscriptionStatus enum."""
    if raw is None:
        return None
    try:
        return BillingSubscriptionStatus(raw)
    except ValueError:
        return None
