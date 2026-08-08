"""Unit tests for the Dodo webhook event mapper.

Mirrors JavaScript tests/dodo-event-mapper.test.ts.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from bursar.billing.types import BillingEventResult, BillingEventType, BillingSubscriptionStatus
from bursar.providers.dodo.event_mapper import _normalize_date
from tests.dodo_fixtures import (
    DODO_DISPUTE_OPENED,
    DODO_ISO_DATE,
    DODO_JS_DATE,
    DODO_PAYMENT_FAILED,
    DODO_PAYMENT_SUCCEEDED,
    DODO_REFUND_SUCCEEDED,
    DODO_SUBSCRIPTION_ACTIVE,
    DODO_SUBSCRIPTION_ACTIVE_NO_DATES,
    DODO_SUBSCRIPTION_ACTIVE_PLAN_SLUG,
    DODO_SUBSCRIPTION_CANCELLED,
    DODO_SUBSCRIPTION_EXPIRED,
    DODO_SUBSCRIPTION_FAILED,
    DODO_SUBSCRIPTION_ON_HOLD,
    DODO_SUBSCRIPTION_PLAN_CHANGED,
    DODO_SUBSCRIPTION_RENEWED,
    DODO_SUBSCRIPTION_UPDATED,
    dodo_event_id,
    map_dodo_event,
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


# ── Canonical event IDs ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_uses_canonical_payment_event_id(sink):
    await map_dodo_event("payment.succeeded", DODO_PAYMENT_SUCCEEDED, "user_1", {}, sink)
    call = sink.ingest_billing_event.call_args
    assert call is not None
    assert call[0][0].event_id == dodo_event_id("payment.succeeded", "pay_dodo_success_001")


@pytest.mark.asyncio
async def test_uses_subscription_id_for_subscription_active(sink):
    await map_dodo_event("subscription.active", DODO_SUBSCRIPTION_ACTIVE, "user_1", {}, sink)
    call = sink.ingest_billing_event.call_args
    assert call is not None
    assert call[0][0].event_id == dodo_event_id("subscription.active", "sub_dodo_active_001")


@pytest.mark.asyncio
async def test_uses_subscription_id_for_subscription_renewed(sink):
    await map_dodo_event("subscription.renewed", DODO_SUBSCRIPTION_RENEWED, "user_1", {}, sink)
    call = sink.ingest_billing_event.call_args
    assert call is not None
    assert call[0][0].event_id == dodo_event_id("subscription.renewed", "sub_dodo_renewed_001")


@pytest.mark.asyncio
async def test_uses_subscription_id_for_subscription_updated(sink):
    await map_dodo_event("subscription.updated", DODO_SUBSCRIPTION_UPDATED, "user_1", {}, sink)
    call = sink.ingest_billing_event.call_args
    assert call is not None
    assert call[0][0].event_id == dodo_event_id("subscription.updated", "sub_dodo_updated_001")


@pytest.mark.asyncio
async def test_unique_rawids_for_different_subscriptions_same_type(sink):
    alpha = {**DODO_SUBSCRIPTION_ACTIVE, "subscription_id": "sub_alpha"}
    beta = {**DODO_SUBSCRIPTION_ACTIVE, "subscription_id": "sub_beta"}
    await map_dodo_event("subscription.active", alpha, "user_1", {}, sink)
    await map_dodo_event("subscription.active", beta, "user_1", {}, sink)
    assert sink.ingest_billing_event.call_count == 2
    calls = sink.ingest_billing_event.call_args_list
    assert calls[0][0][0].event_id == dodo_event_id("subscription.active", "sub_alpha")
    assert calls[1][0][0].event_id == dodo_event_id("subscription.active", "sub_beta")


@pytest.mark.asyncio
async def test_rejects_customer_id_as_subscription_identifier(sink):
    payload = {"customer_id": "cus_dodo_001", "status": "active"}
    with pytest.raises(ValueError, match="subscription_id"):
        await map_dodo_event("subscription.active", payload, "user_1", {}, sink)
    sink.ingest_billing_event.assert_not_called()


@pytest.mark.asyncio
async def test_rejects_missing_subscription_identifier(sink):
    with pytest.raises(ValueError, match="subscription_id"):
        await map_dodo_event("subscription.active", {}, "user_1", {}, sink)
    sink.ingest_billing_event.assert_not_called()


# ── Date normalization (Bug 2 regression tests) ──────────────────────


@pytest.mark.asyncio
async def test_subscription_active_converts_js_dates_to_iso(sink):
    await map_dodo_event("subscription.active", DODO_SUBSCRIPTION_ACTIVE, "user_1", {}, sink)
    call = sink.ingest_billing_event.call_args
    assert call is not None
    event = call[0][0]
    assert event.subscription.period_start.endswith("+00:00")
    assert event.subscription.period_end.endswith("+00:00")


@pytest.mark.asyncio
async def test_subscription_renewed_converts_js_dates_to_iso(sink):
    await map_dodo_event("subscription.renewed", DODO_SUBSCRIPTION_RENEWED, "user_1", {}, sink)
    call = sink.ingest_billing_event.call_args
    assert call is not None
    event = call[0][0]
    assert event.subscription.period_start is not None
    assert event.subscription.period_end is not None


@pytest.mark.asyncio
async def test_omits_period_start_end_when_dates_absent(sink):
    await map_dodo_event(
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
    await map_dodo_event("subscription.active", DODO_SUBSCRIPTION_ACTIVE, "user_1", {}, sink)
    call = sink.ingest_billing_event.call_args
    assert call is not None
    assert call[0][0].event_type == BillingEventType.subscription_created


@pytest.mark.asyncio
async def test_subscription_active_preserves_trialing_status(sink):
    payload = {**DODO_SUBSCRIPTION_ACTIVE, "status": "trialing"}
    await map_dodo_event("subscription.active", payload, "user_1", {}, sink)
    call = sink.ingest_billing_event.call_args
    assert call is not None
    assert call[0][0].subscription.status == BillingSubscriptionStatus.trialing


@pytest.mark.asyncio
async def test_subscription_renewed_to_subscription_renewed(sink):
    await map_dodo_event("subscription.renewed", DODO_SUBSCRIPTION_RENEWED, "user_1", {}, sink)
    call = sink.ingest_billing_event.call_args
    assert call is not None
    assert call[0][0].event_type == BillingEventType.subscription_renewed


@pytest.mark.asyncio
async def test_subscription_cancelled_to_subscription_canceled(sink):
    await map_dodo_event("subscription.cancelled", DODO_SUBSCRIPTION_CANCELLED, None, {}, sink)
    call = sink.ingest_billing_event.call_args
    assert call is not None
    assert call[0][0].event_type == BillingEventType.subscription_canceled
    assert call[0][0].subscription.status.value == "canceled"
    assert call[0][0].subscription.refs.product_id == "prod_monk"


@pytest.mark.asyncio
async def test_subscription_expired_to_subscription_expired(sink):
    await map_dodo_event("subscription.expired", DODO_SUBSCRIPTION_EXPIRED, None, {}, sink)
    call = sink.ingest_billing_event.call_args
    assert call is not None
    assert call[0][0].event_type == BillingEventType.subscription_expired
    assert call[0][0].subscription.status.value == "expired"


@pytest.mark.asyncio
async def test_subscription_failed_to_updated_with_past_due(sink):
    await map_dodo_event("subscription.failed", DODO_SUBSCRIPTION_FAILED, None, {}, sink)
    call = sink.ingest_billing_event.call_args
    assert call is not None
    assert call[0][0].event_type == BillingEventType.subscription_updated
    assert call[0][0].subscription.status.value == "past_due"


@pytest.mark.asyncio
async def test_subscription_on_hold_to_updated_with_past_due(sink):
    await map_dodo_event("subscription.on_hold", DODO_SUBSCRIPTION_ON_HOLD, None, {}, sink)
    call = sink.ingest_billing_event.call_args
    assert call is not None
    assert call[0][0].event_type == BillingEventType.subscription_updated
    assert call[0][0].subscription.status.value == "past_due"


@pytest.mark.asyncio
async def test_subscription_plan_changed_with_product_id(sink):
    await map_dodo_event(
        "subscription.plan_changed",
        DODO_SUBSCRIPTION_PLAN_CHANGED,
        "user_1",
        {},
        sink,
    )
    call = sink.ingest_billing_event.call_args
    assert call is not None
    assert call[0][0].event_type == BillingEventType.subscription_plan_changed
    assert call[0][0].subscription.cancel_at_period_end is True
    assert call[0][0].subscription.refs.product_id == "prod_sage"


@pytest.mark.asyncio
async def test_payment_succeeded(sink):
    await map_dodo_event("payment.succeeded", DODO_PAYMENT_SUCCEEDED, None, {}, sink)
    call = sink.ingest_billing_event.call_args
    assert call is not None
    assert call[0][0].event_type == BillingEventType.payment_succeeded
    assert call[0][0].payment.provider_payment_id == "pay_dodo_success_001"
    assert call[0][0].payment.amount_minor == 2999


@pytest.mark.asyncio
async def test_payment_failed(sink):
    await map_dodo_event("payment.failed", DODO_PAYMENT_FAILED, "user_1", {}, sink)
    call = sink.ingest_billing_event.call_args
    assert call is not None
    assert call[0][0].event_type == BillingEventType.payment_failed


@pytest.mark.asyncio
async def test_refund_succeeded_to_refund_created(sink):
    await map_dodo_event("refund.succeeded", DODO_REFUND_SUCCEEDED, None, {}, sink)
    call = sink.ingest_billing_event.call_args
    assert call is not None
    assert call[0][0].event_type == BillingEventType.refund_created
    assert call[0][0].refund.provider_refund_id == "refund_dodo_001"
    assert call[0][0].refund.amount_minor == 2999


@pytest.mark.asyncio
async def test_dispute_opened(sink):
    await map_dodo_event("dispute.opened", DODO_DISPUTE_OPENED, None, {}, sink)
    call = sink.ingest_billing_event.call_args
    assert call is not None
    assert call[0][0].event_type == BillingEventType.dispute_created


# dispute.won/lost/etc → dispute.closed routing is in the JS mapper only (Python mapper doesn't have it yet).


@pytest.mark.asyncio
async def test_unknown_event_type_does_not_call_sink(sink):
    await map_dodo_event("unknown.event.type", {}, None, {}, sink)
    assert sink.ingest_billing_event.call_count == 0


@pytest.mark.asyncio
async def test_passes_metadata_through(sink):
    metadata = {"userId": "user_1", "plan_slug": "monk", "billing_interval": "month"}
    await map_dodo_event("subscription.active", DODO_SUBSCRIPTION_ACTIVE, "user_1", metadata, sink)
    call = sink.ingest_billing_event.call_args
    assert call is not None
    assert call[0][0].metadata == metadata


# ── Ref resolution ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_uses_data_product_id_when_present(sink):
    await map_dodo_event("subscription.active", DODO_SUBSCRIPTION_ACTIVE, "user_1", {}, sink)
    call = sink.ingest_billing_event.call_args
    assert call is not None
    assert call[0][0].subscription.refs.product_id == "prod_monk"


@pytest.mark.asyncio
async def test_falls_back_to_metadata_plan_slug(sink):
    await map_dodo_event(
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
async def test_rejects_blank_product_id_instead_of_manufacturing_a_reference(sink):
    payload = {"subscription_id": "sub_trimmed_refs", "product_id": "   "}
    with pytest.raises(ValueError, match="product_id"):
        await map_dodo_event(
            "subscription.updated",
            payload,
            "user_1",
            {"plan_slug": "  sage  "},
            sink,
        )
    sink.ingest_billing_event.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        ("subscription.cancelled", DODO_SUBSCRIPTION_CANCELLED),
        ("subscription.expired", DODO_SUBSCRIPTION_EXPIRED),
        ("subscription.failed", DODO_SUBSCRIPTION_FAILED),
        ("subscription.on_hold", DODO_SUBSCRIPTION_ON_HOLD),
        ("subscription.updated", DODO_SUBSCRIPTION_UPDATED),
    ],
)
async def test_subscription_state_events_include_offer_and_cadence_context(sink, event_type, payload):
    await map_dodo_event(
        event_type,
        {
            **payload,
            "product_id": " prod_monk ",
            "payment_frequency_interval": "Month",
            "payment_frequency_count": 1,
            "previous_billing_date": DODO_JS_DATE,
        },
        "user_1",
        {},
        sink,
    )
    call = sink.ingest_billing_event.call_args
    assert call is not None
    subscription = call[0][0].subscription
    assert subscription.refs.product_id == "prod_monk"
    assert subscription.interval == "month"
    assert subscription.interval_count == 1
    assert subscription.period_start == DODO_ISO_DATE


@pytest.mark.asyncio
async def test_undefined_when_no_refs(sink):
    payload = {"subscription_id": "sub_no_refs", "status": "active"}
    await map_dodo_event("subscription.active", payload, "user_1", {}, sink)
    call = sink.ingest_billing_event.call_args
    assert call is not None
    assert call[0][0].subscription.refs is None


@pytest.mark.asyncio
async def test_uses_official_payment_product_cart(sink):
    payload = {
        **DODO_PAYMENT_SUCCEEDED,
        "subscription_id": None,
        "product_id": None,
        "product_cart": [{"product_id": "prod_credit_pack"}],
    }
    await map_dodo_event("payment.succeeded", payload, "user_1", {}, sink)
    call = sink.ingest_billing_event.call_args
    assert call is not None
    event = call[0][0]
    assert event.payment.purpose == "credit_topup"
    assert event.payment.refs.product_id == "prod_credit_pack"
    assert event.subscription is None


@pytest.mark.asyncio
async def test_uses_official_nested_customer_identity(sink):
    payload = {
        **DODO_SUBSCRIPTION_ACTIVE,
        "customer": {
            "customer_id": "cus_official_001",
            "email": "learner@example.com",
        },
    }
    await map_dodo_event("subscription.active", payload, "user_1", {}, sink)
    call = sink.ingest_billing_event.call_args
    assert call is not None
    assert call[0][0].customer.provider_customer_id == "cus_official_001"
    assert call[0][0].customer.email == "learner@example.com"


# ── Edge cases ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rejects_non_boolean_cancellation_flag(sink):
    payload = {
        **DODO_SUBSCRIPTION_PLAN_CHANGED,
        "cancel_at_next_billing_date": "true",
    }
    with pytest.raises(ValueError, match="cancel_at_next_billing_date"):
        await map_dodo_event("subscription.plan_changed", payload, "user_1", {}, sink)
    sink.ingest_billing_event.assert_not_called()


@pytest.mark.asyncio
async def test_rejects_cancelled_without_subscription_id(sink):
    with pytest.raises(ValueError, match="subscription_id"):
        await map_dodo_event("subscription.cancelled", {}, None, {}, sink)
    assert sink.ingest_billing_event.call_count == 0


@pytest.mark.asyncio
async def test_rejects_expired_without_subscription_id(sink):
    with pytest.raises(ValueError, match="subscription_id"):
        await map_dodo_event("subscription.expired", {}, None, {}, sink)
    assert sink.ingest_billing_event.call_count == 0


@pytest.mark.asyncio
async def test_skips_subscription_active_without_user_id(sink):
    await map_dodo_event("subscription.active", DODO_SUBSCRIPTION_ACTIVE, None, {}, sink)
    assert sink.ingest_billing_event.call_count == 0


@pytest.mark.asyncio
async def test_skips_subscription_renewed_without_user_id(sink):
    await map_dodo_event("subscription.renewed", DODO_SUBSCRIPTION_RENEWED, None, {}, sink)
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
    await map_dodo_event("subscription.active", payload, "user_1", {}, sink)
    call = sink.ingest_billing_event.call_args
    assert call is not None
    assert call[0][0].subscription.interval == "year"
    assert call[0][0].subscription.interval_count == 1
