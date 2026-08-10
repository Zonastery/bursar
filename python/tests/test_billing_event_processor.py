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
    BillingInvoiceInfo,
)
from bursar.errors import StoreError
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
    invoice = (
        BillingInvoiceInfo(
            provider_invoice_id=f"in_{event_id}",
            status="open",
            amount_paid_minor=0,
            amount_due_minor=0,
            currency="USD",
        )
        if event_type.value.startswith("invoice.")
        else None
    )
    customer = (
        BillingCustomerInfo(provider_customer_id="cus_fixture") if event_type.value.startswith("customer.") else None
    )
    return BillingEvent(
        provider="stripe",
        event_id=event_id,
        event_type=event_type,
        occurred_at="2026-08-05T00:00:00Z",
        invoice=invoice,
        customer=customer,
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
    event = _event("evt_unhandled", BillingEventType.invoice_created)

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
    ("claim", "expected_error"),
    [
        (BillingEventClaim(status="invalid_request"), "invalid_request"),
        (
            BillingEventClaim(status="idempotency_conflict", billing_event_id=EVENT_ROW_ID),
            "idempotency_conflict",
        ),
        (
            BillingEventClaim(status="max_retries_exceeded", billing_event_id=EVENT_ROW_ID),
            "max_retries_exceeded",
        ),
        (BillingEventClaim(status="retry"), "claim_failed_retry"),
    ],
)
def test_unclaimed_event_outcomes_are_not_routed(
    claim: BillingEventClaim,
    expected_error: str,
) -> None:
    store = _claimed_store()
    store.claim_billing_event.return_value = claim
    service = BillingService(store)

    result = service.ingest_billing_event(_event(f"evt_{expected_error}", BillingEventType.invoice_created))

    assert result.handled is False
    assert result.error == expected_error
    store.complete_billing_event.assert_not_called()
    store.fail_billing_event.assert_not_called()


@pytest.mark.parametrize("status", ["idempotency_conflict", "max_retries_exceeded"])
def test_stored_terminal_claim_requires_a_billing_event_id(status: str) -> None:
    with pytest.raises(ValueError, match="requires billing_event_id"):
        BillingEventClaim(status=status)  # type: ignore[arg-type]


@pytest.mark.parametrize("raw_message", ["   ", f"  {'x' * 9_000}  "])
def test_processing_errors_are_normalized_before_persistence(
    raw_message: str,
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
    assert result.error == "billing_event_processing_failed:RuntimeError"
    assert store.fail_billing_event.call_args.args[3] == "billing_event_processing_failed:RuntimeError"


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


def test_billing_event_repository_fails_closed_without_mutation_result() -> None:
    repository = BillingEventRepository(MagicMock(return_value=[]))

    with pytest.raises(StoreError) as claim_error:
        repository.claim("stripe", "evt_missing", "invoice.paid", "{}")
    assert claim_error.value.indeterminate is True

    with pytest.raises(StoreError) as completion_error:
        repository.complete("stripe", "evt_missing", EVENT_ROW_ID)
    assert completion_error.value.indeterminate is True
