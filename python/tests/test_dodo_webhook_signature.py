"""Mirrors JS SDK ``tests/dodo-webhook-signature.test.ts``."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from dodopayments import AsyncDodoPayments, NotFoundError

from bursar.billing.types import BillingEvent, BillingEventResult
from bursar.providers.dodo.provider import DodoProvider
from bursar.providers.types import WebhookRequest
from tests.dodo_fixtures import DODO_ISO_DATE, dodo_event_id


class FakeSink:
    def __init__(self) -> None:
        self.called = False
        self.event: BillingEvent | None = None

    def ingest_billing_event(self, event: BillingEvent) -> BillingEventResult:
        self.called = True
        self.event = event
        return BillingEventResult(handled=True, action="ok")


class FakeLogger:
    def debug(self, msg: str, ctx: dict | None = None, **kwargs: Any) -> None:
        pass

    def info(self, msg: str, ctx: dict | None = None, **kwargs: Any) -> None:
        pass

    def warning(self, msg: str, ctx: dict | None = None, **kwargs: Any) -> None:
        pass

    def error(self, msg: str, ctx: dict | None = None, **kwargs: Any) -> None:
        pass


class FakeClient:
    def __init__(self, unwrap_result: Any = None, unwrap_error: Exception | None = None) -> None:
        self._unwrap_result = unwrap_result
        self._unwrap_error = unwrap_error
        self.webhooks = self
        self.checkout_sessions: Any = None

    def unwrap(self, raw_body: str, headers: dict[str, str], key: str | None = None) -> Any:
        if self._unwrap_error:
            raise self._unwrap_error
        return self._unwrap_result


class FakeModel(SimpleNamespace):
    def model_dump(self, *, mode: str = "python") -> dict[str, Any]:
        del mode

        def convert(value: Any) -> Any:
            if isinstance(value, FakeModel):
                return {key: convert(item) for key, item in vars(value).items()}
            return value

        return {key: convert(value) for key, value in vars(self).items()}


ACCOUNT_ID = "team-account-1"


@pytest.fixture
def sink() -> FakeSink:
    return FakeSink()


@pytest.fixture
def logger() -> FakeLogger:
    return FakeLogger()


@pytest.mark.asyncio
async def test_returns_received_true_when_unwrap_succeeds(sink: FakeSink, logger: FakeLogger) -> None:
    client = FakeClient(
        unwrap_result=SimpleNamespace(
            type="subscription.active",
            timestamp=DODO_ISO_DATE,
            data=FakeModel(
                id="evt_test_valid",
                subscription_id="sub_test_valid",
                metadata={"bursar_account_id": ACCOUNT_ID, "plan_slug": "monk"},
            ),
        ),
    )
    provider = DodoProvider(
        get_client=lambda: cast(AsyncDodoPayments, client),
        webhook_key="test_wh_key_12345",
        event_sink=sink,
        logger=logger,
    )
    req = WebhookRequest(
        raw_body=(f'{{"type":"subscription.active","data":{{"metadata":{{"bursar_account_id":"{ACCOUNT_ID}"}}}}}}}}'),
        headers={"content-type": "application/json", "x-webhook-signature": "valid_signature"},
    )
    result = await provider.handle_webhook(req)
    assert result.received is True
    assert result.retryable is False
    assert result.provider == "dodo"
    assert result.event_id == dodo_event_id("subscription.active", "sub_test_valid")
    assert result.event_type == "subscription.active"
    assert sink.event is not None
    assert sink.event.account_id == ACCOUNT_ID


@pytest.mark.asyncio
async def test_returns_non_retryable_on_signature_failure(sink: FakeSink, logger: FakeLogger) -> None:
    client = FakeClient(unwrap_error=ValueError("Invalid signature"))
    provider = DodoProvider(
        get_client=lambda: cast(AsyncDodoPayments, client),
        webhook_key="test_wh_key_12345",
        event_sink=sink,
        logger=logger,
    )
    req = WebhookRequest(
        raw_body='{"type":"subscription.active","data":{}}',
        headers={"content-type": "application/json", "x-webhook-signature": "tampered_signature"},
    )
    result = await provider.handle_webhook(req)
    assert result.received is False
    assert result.retryable is False
    assert result.provider == "dodo"
    assert result.event_id is None


@pytest.mark.asyncio
async def test_returns_non_retryable_for_malformed_payload(sink: FakeSink, logger: FakeLogger) -> None:
    client = FakeClient(unwrap_error=ValueError("Malformed payload"))
    provider = DodoProvider(
        get_client=lambda: cast(AsyncDodoPayments, client),
        webhook_key="wrong_key",
        event_sink=sink,
        logger=logger,
    )
    req = WebhookRequest(
        raw_body='{"type":"subscription.active","data":{}}',
        headers={"content-type": "application/json"},
    )
    result = await provider.handle_webhook(req)
    assert result.received is False
    assert result.retryable is False


@pytest.mark.asyncio
async def test_leaves_metadata_free_payment_failure_for_persisted_reference_resolution(
    sink: FakeSink,
    logger: FakeLogger,
) -> None:
    client = FakeClient(
        unwrap_result=SimpleNamespace(
            type="payment.failed",
            timestamp=DODO_ISO_DATE,
            data=FakeModel(
                id="evt_payment_failed",
                payment_id="pay_failed",
                total_amount=500,
                currency="USD",
                metadata={"userId": ACCOUNT_ID},
                customer=FakeModel(customer_id="cus_failed", email="guest@example.com"),
            ),
        ),
    )
    provider = DodoProvider(
        get_client=lambda: cast(AsyncDodoPayments, client),
        webhook_key="test_wh_key_12345",
        event_sink=sink,
        logger=logger,
    )
    req = WebhookRequest(
        raw_body='{"type":"payment.failed","data":{"payment_id":"pay_failed","customer":{"customer_id":"cus_failed","email":"guest@example.com"}}}',
        headers={},
    )
    result = await provider.handle_webhook(req)
    assert result.received is True
    assert result.provider == "dodo"
    assert result.event_id == dodo_event_id("payment.failed", "pay_failed")
    assert result.event_type == "payment.failed"
    assert sink.called
    assert sink.event is not None
    assert sink.event.account_id is None


@pytest.mark.asyncio
async def test_get_checkout_session_status_with_requires_customer_action(sink: FakeSink, logger: FakeLogger) -> None:
    class FakeCheckoutSessions:
        async def retrieve(self, _session_id: str) -> SimpleNamespace:
            return SimpleNamespace(payment_status="requires_customer_action")

    client = FakeClient(unwrap_result=None)
    client.checkout_sessions = FakeCheckoutSessions()
    provider = DodoProvider(
        get_client=lambda: cast(AsyncDodoPayments, client),
        webhook_key="test_wh_key_12345",
        event_sink=sink,
        logger=logger,
    )
    result = await provider.get_checkout_session_status("cks_1")
    assert result is not None
    assert result.payment_status == "requires_customer_action"


@pytest.mark.asyncio
async def test_returns_none_for_missing_session(sink: FakeSink, logger: FakeLogger) -> None:
    class FakeCheckoutSessions:
        async def retrieve(self, _session_id: str) -> None:
            response = httpx.Response(
                404,
                request=httpx.Request("GET", "https://test.dodopayments.com/checkouts/cks_missing"),
            )
            raise NotFoundError("Checkout session not found", response=response, body=None)

    client = FakeClient(unwrap_result=None)
    client.checkout_sessions = FakeCheckoutSessions()
    provider = DodoProvider(
        get_client=lambda: cast(AsyncDodoPayments, client),
        webhook_key="test_wh_key_12345",
        event_sink=sink,
        logger=logger,
    )
    result = await provider.get_checkout_session_status("cks_missing")
    assert result is None
