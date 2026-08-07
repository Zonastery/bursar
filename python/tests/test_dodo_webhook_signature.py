"""Mirrors JS SDK ``tests/dodo-webhook-signature.test.ts``."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from dodopayments import NotFoundError

from bursar.billing.types import BillingEvent, BillingEventResult
from bursar.providers.dodo.provider import DodoProvider
from bursar.providers.types import ResolveIdentityInput, WebhookRequest
from tests.dodo_fixtures import DODO_ISO_DATE, dodo_event_id


class FakeSink:
    def __init__(self) -> None:
        self.called = False

    def ingest_billing_event(self, event: BillingEvent) -> BillingEventResult:
        self.called = True
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


class FakeResolveUser:
    def __init__(self) -> None:
        self.called = False

    async def __call__(self, identity: ResolveIdentityInput) -> str | None:
        del identity
        self.called = True
        return "00000000-0000-0000-0000-000000000001"


USER_ID = "00000000-0000-0000-0000-000000000001"


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
                metadata={"userId": USER_ID, "plan_slug": "monk"},
            ),
        ),
    )
    provider = DodoProvider(
        get_client=lambda: client,
        webhook_key="test_wh_key_12345",
        event_sink=sink,
        resolve_user=None,
        logger=logger,
    )
    req = WebhookRequest(
        raw_body=f'{{"type":"subscription.active","data":{{"metadata":{{"userId":"{USER_ID}"}}}}}}',
        headers={"content-type": "application/json", "x-webhook-signature": "valid_signature"},
    )
    result = await provider.handle_webhook(req)
    assert result.received is True
    assert result.retryable is False
    assert result.provider == "dodo"
    assert result.event_id == dodo_event_id("subscription.active", "sub_test_valid")
    assert result.event_type == "subscription.active"


@pytest.mark.asyncio
async def test_returns_non_retryable_on_signature_failure(sink: FakeSink, logger: FakeLogger) -> None:
    client = FakeClient(unwrap_error=ValueError("Invalid signature"))
    provider = DodoProvider(
        get_client=lambda: client,
        webhook_key="test_wh_key_12345",
        event_sink=sink,
        resolve_user=None,
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
        get_client=lambda: client,
        webhook_key="wrong_key",
        event_sink=sink,
        resolve_user=None,
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
async def test_does_not_resolve_anonymous_user_for_payment_failed(
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
                customer=FakeModel(customer_id="cus_failed", email="guest@example.com"),
            ),
        ),
    )
    resolve_user = FakeResolveUser()
    provider = DodoProvider(
        get_client=lambda: client,
        webhook_key="test_wh_key_12345",
        event_sink=sink,
        resolve_user=resolve_user,
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
    assert not resolve_user.called
    assert sink.called


@pytest.mark.asyncio
async def test_get_checkout_session_status_with_requires_customer_action(sink: FakeSink, logger: FakeLogger) -> None:
    class FakeCheckoutSessions:
        async def retrieve(self, _session_id: str) -> SimpleNamespace:
            return SimpleNamespace(payment_status="requires_customer_action")

    client = FakeClient(unwrap_result=None)
    client.checkout_sessions = FakeCheckoutSessions()
    provider = DodoProvider(
        get_client=lambda: client,
        webhook_key="test_wh_key_12345",
        event_sink=sink,
        resolve_user=None,
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
        get_client=lambda: client,
        webhook_key="test_wh_key_12345",
        event_sink=sink,
        resolve_user=None,
        logger=logger,
    )
    result = await provider.get_checkout_session_status("cks_missing")
    assert result is None
