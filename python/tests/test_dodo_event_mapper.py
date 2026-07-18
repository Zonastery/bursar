"""Unit tests for the Dodo webhook event mapper.

Mirrors JavaScript tests/dodo-event-mapper.test.ts.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from bursar.billing.models import BillingEventResult, BillingEventType
from bursar.providers.dodo.event_mapper import (
    _normalize_date,
    handle_dodo_billing_event,
)
from tests.dodo_fixtures import (
    DODO_DISPUTE_CREATED,
    DODO_ISO_DATE,
    DODO_JS_DATE,
    DODO_PAYMENT_FAILED,
    DODO_PAYMENT_SUCCEEDED,
    DODO_REFUND_SUCCEEDED,
    DODO_SUBSCRIPTION_ACTIVE,
    DODO_SUBSCRIPTION_ACTIVE_NO_DATES,
    DODO_SUBSCRIPTION_ACTIVE_PLAN_SLUG,
    DODO_SUBSCRIPTION_CANCELLATION_SCHEDULED,
    DODO_SUBSCRIPTION_CANCELLED,
    DODO_SUBSCRIPTION_EXPIRED,
    DODO_SUBSCRIPTION_FAILED,
    DODO_SUBSCRIPTION_ON_HOLD,
    DODO_SUBSCRIPTION_PLAN_CHANGED,
    DODO_SUBSCRIPTION_RENEWED,
    DODO_SUBSCRIPTION_UPDATED,
)


@pytest.fixture
def sink():
    m = MagicMock()
    m.ingest_billing_event.return_value = BillingEventResult(handled=True)
    return m


# ── _normalize_date unit tests ───────────────────────────────────────


class TestNormalizeDate:
    def test_converts_js_date_tostring_to_iso(self):
        assert _normalize_date(DODO_JS_DATE) == DODO_ISO_DATE

    def test_passes_through_valid_iso_unchanged(self):
        assert _normalize_date("2026-07-18T05:15:24.000Z") == "2026-07-18T05:15:24+00:00"
        assert _normalize_date("2026-07-18T00:00:00Z") == "2026-07-18T00:00:00+00:00"

    def test_returns_none_for_none(self):
        assert _normalize_date(None) is None

    def test_returns_none_for_empty_string(self):
        assert _normalize_date("") is None

    def test_returns_none_for_unparseable_string(self):
        assert _normalize_date("not-a-date") is None


# ── RawId fallback (Bug 1 regression tests) ─────────────────────────


@pytest.mark.asyncio
async def test_uses_data_id_when_present(sink):
    await handle_dodo_billing_event("payment.succeeded", DODO_PAYMENT_SUCCEEDED, "user_1", {}, sink)
    call = sink.ingest_billing_event.call_args
    assert call is not None
    assert call[0][0].event_id == "pay_dodo_success_001"


@pytest.mark.asyncio
async def test_falls_back_to_dodo_type_subscription_id_for_subscription_active(sink):
    await handle_dodo_billing_event("subscription.active", DODO_SUBSCRIPTION_ACTIVE, "user_1", {}, sink)
    call = sink.ingest_billing_event.call_args
    assert call is not None
    assert call[0][0].event_id == "dodo:subscription.active:sub_dodo_active_001"


@pytest.mark.asyncio
async def test_falls_back_to_dodo_type_subscription_id_for_subscription_renewed(sink):
    await handle_dodo_billing_event("subscription.renewed", DODO_SUBSCRIPTION_RENEWED, "user_1", {}, sink)
    call = sink.ingest_billing_event.call_args
    assert call is not None
    assert call[0][0].event_id == "dodo:subscription.renewed:sub_dodo_renewed_001"


@pytest.mark.asyncio
async def test_falls_back_to_dodo_type_subscription_id_for_subscription_updated(sink):
    await handle_dodo_billing_event("subscription.updated", DODO_SUBSCRIPTION_UPDATED, "user_1", {}, sink)
    call = sink.ingest_billing_event.call_args
    assert call is not None
    assert call[0][0].event_id == "dodo:subscription.updated:sub_dodo_updated_001"


@pytest.mark.asyncio
async def test_unique_rawids_for_different_subscriptions_same_type(sink):
    alpha = {**DODO_SUBSCRIPTION_ACTIVE, "subscription_id": "sub_alpha"}
    beta = {**DODO_SUBSCRIPTION_ACTIVE, "subscription_id": "sub_beta"}
    await handle_dodo_billing_event("subscription.active", alpha, "user_1", {}, sink)
    await handle_dodo_billing_event("subscription.active", beta, "user_1", {}, sink)
    assert sink.ingest_billing_event.call_count == 2
    calls = sink.ingest_billing_event.call_args_list
    assert calls[0][0][0].event_id == "dodo:subscription.active:sub_alpha"
    assert calls[1][0][0].event_id == "dodo:subscription.active:sub_beta"


@pytest.mark.asyncio
async def test_falls_back_to_dodo_type_customer_id(sink):
    payload = {"customer_id": "cus_dodo_001", "status": "active"}
    await handle_dodo_billing_event("subscription.active", payload, "user_1", {}, sink)
    call = sink.ingest_billing_event.call_args
    assert call is not None
    assert call[0][0].event_id == "dodo:subscription.active:cus_dodo_001"


@pytest.mark.asyncio
async def test_empty_suffix_when_both_missing(sink):
    await handle_dodo_billing_event("subscription.active", {}, "user_1", {}, sink)
    call = sink.ingest_billing_event.call_args
    assert call is not None
    assert call[0][0].event_id == "dodo:subscription.active:"


# ── Date normalization (Bug 2 regression tests) ──────────────────────


@pytest.mark.asyncio
async def test_subscription_active_converts_js_dates_to_iso(sink):
    await handle_dodo_billing_event("subscription.active", DODO_SUBSCRIPTION_ACTIVE, "user_1", {}, sink)
    call = sink.ingest_billing_event.call_args
    assert call is not None
    event = call[0][0]
    assert event.subscription.period_start.endswith("+00:00")
    assert event.subscription.period_end.endswith("+00:00")


@pytest.mark.asyncio
async def test_subscription_renewed_converts_js_dates_to_iso(sink):
    await handle_dodo_billing_event("subscription.renewed", DODO_SUBSCRIPTION_RENEWED, "user_1", {}, sink)
    call = sink.ingest_billing_event.call_args
    assert call is not None
    event = call[0][0]
    assert event.subscription.period_start is not None
    assert event.subscription.period_end is not None


@pytest.mark.asyncio
async def test_omits_period_start_end_when_dates_absent(sink):
    await handle_dodo_billing_event(
        "subscription.active",
        DODO_SUBSCRIPTION_ACTIVE_NO_DATES,
        "user_1",
        {},
        sink,
    )
    call = sink.ingest_billing_event.call_args
    assert call is not None
    event = call[0][0]
    assert event.subscription.period_start is None
    assert event.subscription.period_end is None


# ── Event type routing ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_subscription_active_to_subscription_created(sink):
    await handle_dodo_billing_event("subscription.active", DODO_SUBSCRIPTION_ACTIVE, "user_1", {}, sink)
    call = sink.ingest_billing_event.call_args
    assert call is not None
    assert call[0][0].event_type == BillingEventType.subscription_created


@pytest.mark.asyncio
async def test_subscription_renewed_to_subscription_activated(sink):
    await handle_dodo_billing_event("subscription.renewed", DODO_SUBSCRIPTION_RENEWED, "user_1", {}, sink)
    call = sink.ingest_billing_event.call_args
    assert call is not None
    assert call[0][0].event_type == BillingEventType.subscription_activated


@pytest.mark.asyncio
async def test_subscription_cancelled_to_subscription_canceled(sink):
    await handle_dodo_billing_event("subscription.cancelled", DODO_SUBSCRIPTION_CANCELLED, None, {}, sink)
    call = sink.ingest_billing_event.call_args
    assert call is not None
    assert call[0][0].event_type == BillingEventType.subscription_canceled


@pytest.mark.asyncio
async def test_subscription_expired_to_subscription_expired(sink):
    await handle_dodo_billing_event("subscription.expired", DODO_SUBSCRIPTION_EXPIRED, None, {}, sink)
    call = sink.ingest_billing_event.call_args
    assert call is not None
    assert call[0][0].event_type == BillingEventType.subscription_expired


@pytest.mark.asyncio
async def test_subscription_failed_to_updated_with_past_due(sink):
    await handle_dodo_billing_event("subscription.failed", DODO_SUBSCRIPTION_FAILED, None, {}, sink)
    call = sink.ingest_billing_event.call_args
    assert call is not None
    assert call[0][0].event_type == BillingEventType.subscription_updated
    assert call[0][0].subscription.status.value == "past_due"


@pytest.mark.asyncio
async def test_subscription_on_hold_to_updated_with_past_due(sink):
    await handle_dodo_billing_event("subscription.on_hold", DODO_SUBSCRIPTION_ON_HOLD, None, {}, sink)
    call = sink.ingest_billing_event.call_args
    assert call is not None
    assert call[0][0].event_type == BillingEventType.subscription_updated
    assert call[0][0].subscription.status.value == "past_due"


@pytest.mark.asyncio
async def test_cancellation_scheduled(sink):
    await handle_dodo_billing_event(
        "subscription.cancellation_scheduled",
        DODO_SUBSCRIPTION_CANCELLATION_SCHEDULED,
        None,
        {},
        sink,
    )
    call = sink.ingest_billing_event.call_args
    assert call is not None
    assert call[0][0].event_type == BillingEventType.subscription_cancellation_scheduled
    assert call[0][0].subscription.cancel_at_period_end is True


# cancellation_unscheduled, checkout.expired, and dynamic dispute.* dispatch
# are covered by the JS test suite (those handlers don't exist in the Python mapper yet).


@pytest.mark.asyncio
async def test_subscription_plan_changed_with_product_id(sink):
    await handle_dodo_billing_event(
        "subscription.plan_changed",
        DODO_SUBSCRIPTION_PLAN_CHANGED,
        "user_1",
        {},
        sink,
    )
    call = sink.ingest_billing_event.call_args
    assert call is not None
    assert call[0][0].event_type == BillingEventType.subscription_plan_changed
    assert call[0][0].subscription.refs.product_id == "prod_sage"


@pytest.mark.asyncio
async def test_payment_succeeded(sink):
    await handle_dodo_billing_event("payment.succeeded", DODO_PAYMENT_SUCCEEDED, None, {}, sink)
    call = sink.ingest_billing_event.call_args
    assert call is not None
    assert call[0][0].event_type == BillingEventType.payment_succeeded
    assert call[0][0].payment.provider_payment_id == "pay_dodo_success_001"
    assert call[0][0].payment.amount_minor == 2999


@pytest.mark.asyncio
async def test_payment_failed(sink):
    await handle_dodo_billing_event("payment.failed", DODO_PAYMENT_FAILED, "user_1", {}, sink)
    call = sink.ingest_billing_event.call_args
    assert call is not None
    assert call[0][0].event_type == BillingEventType.payment_failed


# checkout.expired handler is in JS mapper only (Python mapper doesn't have it yet).


@pytest.mark.asyncio
async def test_refund_succeeded_to_refund_created(sink):
    await handle_dodo_billing_event("refund.succeeded", DODO_REFUND_SUCCEEDED, None, {}, sink)
    call = sink.ingest_billing_event.call_args
    assert call is not None
    assert call[0][0].event_type == BillingEventType.refund_created
    assert call[0][0].refund.provider_refund_id == "refund_dodo_001"
    assert call[0][0].refund.amount_minor == 2999


@pytest.mark.asyncio
async def test_dispute_created(sink):
    await handle_dodo_billing_event("dispute.created", DODO_DISPUTE_CREATED, None, {}, sink)
    call = sink.ingest_billing_event.call_args
    assert call is not None
    assert call[0][0].event_type == BillingEventType.dispute_created


# dispute.won/lost/etc → dispute.closed routing is in the JS mapper only (Python mapper doesn't have it yet).


@pytest.mark.asyncio
async def test_unknown_event_type_does_not_call_sink(sink):
    await handle_dodo_billing_event("unknown.event.type", {}, None, {}, sink)
    assert sink.ingest_billing_event.call_count == 0


@pytest.mark.asyncio
async def test_passes_metadata_through(sink):
    metadata = {"userId": "user_1", "plan_slug": "monk", "billing_interval": "month"}
    await handle_dodo_billing_event("subscription.active", DODO_SUBSCRIPTION_ACTIVE, "user_1", metadata, sink)
    call = sink.ingest_billing_event.call_args
    assert call is not None
    assert call[0][0].metadata == metadata


# ── Ref resolution ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_uses_data_product_id_when_present(sink):
    await handle_dodo_billing_event("subscription.active", DODO_SUBSCRIPTION_ACTIVE, "user_1", {}, sink)
    call = sink.ingest_billing_event.call_args
    assert call is not None
    assert call[0][0].subscription.refs.product_id == "prod_monk"


@pytest.mark.asyncio
async def test_falls_back_to_metadata_plan_slug(sink):
    await handle_dodo_billing_event(
        "subscription.active",
        DODO_SUBSCRIPTION_ACTIVE_PLAN_SLUG,
        "user_1",
        {"plan_slug": "sage"},
        sink,
    )
    call = sink.ingest_billing_event.call_args
    assert call is not None
    assert call[0][0].subscription.refs.lookup_key == "sage"


@pytest.mark.asyncio
async def test_undefined_when_no_refs(sink):
    payload = {"subscription_id": "sub_no_refs", "status": "active"}
    await handle_dodo_billing_event("subscription.active", payload, "user_1", {}, sink)
    call = sink.ingest_billing_event.call_args
    assert call is not None
    assert call[0][0].subscription.refs is None


# ── Edge cases ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_skips_cancelled_without_subscription_id(sink):
    await handle_dodo_billing_event("subscription.cancelled", {}, None, {}, sink)
    assert sink.ingest_billing_event.call_count == 0


@pytest.mark.asyncio
async def test_skips_expired_without_subscription_id(sink):
    await handle_dodo_billing_event("subscription.expired", {}, None, {}, sink)
    assert sink.ingest_billing_event.call_count == 0


@pytest.mark.asyncio
async def test_skips_subscription_active_without_user_id(sink):
    await handle_dodo_billing_event("subscription.active", DODO_SUBSCRIPTION_ACTIVE, None, {}, sink)
    assert sink.ingest_billing_event.call_count == 0


@pytest.mark.asyncio
async def test_skips_subscription_renewed_without_user_id(sink):
    await handle_dodo_billing_event("subscription.renewed", DODO_SUBSCRIPTION_RENEWED, None, {}, sink)
    assert sink.ingest_billing_event.call_count == 0


@pytest.mark.asyncio
async def test_normalizes_cadence_fields(sink):
    payload = {
        "subscription_id": "sub_cadence",
        "status": "active",
        "product_id": "prod_yearly",
        "payment_frequency_interval": "Year",
        "payment_frequency_count": 1,
    }
    await handle_dodo_billing_event("subscription.active", payload, "user_1", {}, sink)
    call = sink.ingest_billing_event.call_args
    assert call is not None
    assert call[0][0].subscription.interval == "year"
    assert call[0][0].subscription.interval_count == 1
