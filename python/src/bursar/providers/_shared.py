from __future__ import annotations

import time
from typing import TYPE_CHECKING

from bursar.billing.types import BillingEvent, BillingEventResult, BillingSubscriptionStatus

if TYPE_CHECKING:
    from bursar.billing.contracts import BillingEventSink

_BUSY_RETRY_DELAYS_SECONDS = (0.025, 0.05, 0.1, 0.2, 0.4, 0.8)


def call_billing_event_sink(sink: BillingEventSink, event: BillingEvent) -> BillingEventResult:
    """Dispatch a billing event and raise on unexpected failures."""
    for attempt in range(len(_BUSY_RETRY_DELAYS_SECONDS) + 1):
        result = sink.ingest_billing_event(event)
        if result.error == "claim_busy" and attempt < len(_BUSY_RETRY_DELAYS_SECONDS):
            time.sleep(_BUSY_RETRY_DELAYS_SECONDS[attempt])
            continue
        if not result.handled and result.error not in ("unhandled_event_type", "user_not_found"):
            raise RuntimeError(f"Bursar failed to ingest billing event: {result.error}")
        return result
    raise AssertionError("billing event retry loop exhausted without returning")


def parse_status(raw: str | None) -> BillingSubscriptionStatus | None:
    """Safely parse a status string into a BillingSubscriptionStatus enum."""
    if raw is None:
        return None
    try:
        return BillingSubscriptionStatus(raw)
    except ValueError:
        return None
