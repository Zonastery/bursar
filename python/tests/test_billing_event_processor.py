from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from bursar.billing.billing_service import BillingService
from bursar.billing.billing_store import BillingStore
from bursar.billing.postgres.repositories.event import BillingEventRepository
from bursar.billing.types import (
    BillingCustomerInfo,
    BillingEvent,
    BillingEventClaim,
    BillingEventType,
)
from bursar.shared.diagnostics import bounded_diagnostic_message, optional_bounded_diagnostic_message

USER_ID = "00000000-0000-0000-0000-000000000001"
EVENT_ROW_ID = "00000000-0000-0000-0000-000000000002"


def _claimed_store() -> MagicMock:
    store = MagicMock(spec=BillingStore)
    store.claim_billing_event.return_value = BillingEventClaim(
        status="claimed",
        claim_token="00000000-0000-0000-0000-000000000003",
        billing_event_id=EVENT_ROW_ID,
    )
    store.complete_billing_event.return_value = True
    store.fail_billing_event.return_value = True
    return store


def _event(event_id: str, event_type: BillingEventType) -> BillingEvent:
    return BillingEvent(
        provider="stripe",
        event_id=event_id,
        event_type=event_type,
        occurred_at="2026-08-05T00:00:00Z",
    )


def test_rejected_completion_is_reported_and_requeued() -> None:
    store = _claimed_store()
    store.complete_billing_event.return_value = False
    service = BillingService(store)

    result = service.ingest_billing_event(_event("evt_completion_rejected", BillingEventType.invoice_upcoming))

    assert result.handled is False
    assert result.error == "billing_event_completion_rejected"
    store.fail_billing_event.assert_called_once_with(
        "stripe",
        "evt_completion_rejected",
        "00000000-0000-0000-0000-000000000003",
        "billing_event_completion_rejected",
    )


def test_unhandled_event_is_failed_instead_of_completed() -> None:
    store = _claimed_store()
    service = BillingService(store)
    event = BillingEvent.model_construct(
        provider="stripe",
        event_id="evt_unhandled",
        event_type="provider.unknown",
        occurred_at="2026-08-05T00:00:00Z",
    )

    result = service.ingest_billing_event(event)

    assert result.handled is False
    assert result.error == "unhandled_event_type"
    store.complete_billing_event.assert_not_called()
    store.fail_billing_event.assert_called_once_with(
        "stripe",
        "evt_unhandled",
        "00000000-0000-0000-0000-000000000003",
        "unhandled_event_type",
    )


@pytest.mark.parametrize(
    ("raw_message", "expected"),
    [
        ("   ", "RuntimeError"),
        (f"  {'x' * 9_000}  ", "x" * 8_192),
    ],
)
def test_processing_errors_are_normalized_before_persistence(
    raw_message: str,
    expected: str,
) -> None:
    store = _claimed_store()
    store.upsert_billing_customer.side_effect = RuntimeError(raw_message)
    service = BillingService(store)
    event = _event("evt_failure_message", BillingEventType.customer_created).model_copy(
        update={
            "user_id": USER_ID,
            "customer": BillingCustomerInfo(provider_customer_id="cus_failure"),
        }
    )

    result = service.ingest_billing_event(event)

    assert result.handled is False
    assert result.error == expected
    assert store.fail_billing_event.call_args.args[3] == expected


def test_diagnostic_normalization_preserves_none_and_removes_nul() -> None:
    assert optional_bounded_diagnostic_message(None) is None
    assert bounded_diagnostic_message("  failed\x00message  ") == "failed\ufffdmessage"


def test_billing_event_repository_returns_rpc_acknowledgements() -> None:
    execute = MagicMock(
        side_effect=[
            [{"completed": True}],
            [{"failed": False}],
        ]
    )
    repository = BillingEventRepository(execute)

    assert repository.complete("stripe", "evt_repository", EVENT_ROW_ID) is True
    assert repository.fail("stripe", "evt_repository", EVENT_ROW_ID, "failed") is False
