from __future__ import annotations

from bursar.billing.models import BillingEvent, BillingEventResult, BillingSubscriptionStatus
from bursar.bursar import BillingEventSink


def call_billing_event_sink(sink: BillingEventSink, event: BillingEvent) -> BillingEventResult:
    """Dispatch a billing event and raise on unexpected failures."""
    result = sink.ingest_billing_event(event)
    if not result.handled and result.error not in ("unhandled_event_type", "user_not_found"):
        raise RuntimeError(f"Bursar failed to ingest billing event: {result.error}")
    return result


def parse_status(raw: str | None) -> BillingSubscriptionStatus | None:
    """Safely parse a status string into a BillingSubscriptionStatus enum."""
    if raw is None:
        return None
    try:
        return BillingSubscriptionStatus(raw)
    except ValueError:
        return None
