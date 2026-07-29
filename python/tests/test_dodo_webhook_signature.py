"""Mirrors JS SDK ``tests/dodo-webhook-signature.test.ts``."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from bursar.billing.types import BillingEventResult
from bursar.providers.dodo.provider import DodoProvider
from bursar.providers.types import WebhookRequest


class FakeSink:
    def __init__(self) -> None:
        self.called = False

    def ingest_billing_event(self, _event: Any) -> BillingEventResult:
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

    async def unwrap(self, raw_body: str, headers: dict[str, str], key: str | None = None) -> Any:
        if self._unwrap_error:
            raise self._unwrap_error
        return self._unwrap_result


class FakeResolveUser:
    def __init__(self) -> None:
        self.called = False

    async def __call__(self, data: dict[str, Any], metadata: dict[str, str]) -> str | None:
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
            data=SimpleNamespace(
                id="evt_test_valid",
                subscription_id="sub_test_valid",
                metadata={"userId": USER_ID, "plan_slug": "monk"},
            ),
        ),
    )
    provider = DodoProvider(
        get_client=lambda: client,
        config={"webhook_key": "test_wh_key_12345"},
        sink=sink,
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
    assert result.event_id == "evt_test_valid"
    assert result.event_type == "subscription.active"


@pytest.mark.asyncio
async def test_returns_non_retryable_on_signature_failure(sink: FakeSink, logger: FakeLogger) -> None:
    client = FakeClient(unwrap_error=ValueError("Invalid signature"))
    provider = DodoProvider(
        get_client=lambda: client,
        config={"webhook_key": "test_wh_key_12345"},
        sink=sink,
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
async def test_returns_non_retryable_regardless_of_error(sink: FakeSink, logger: FakeLogger) -> None:
    client = FakeClient(unwrap_error=RuntimeError("Network error"))
    provider = DodoProvider(
        get_client=lambda: client,
        config={"webhook_key": "wrong_key"},
        sink=sink,
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
            data=SimpleNamespace(
                id="evt_payment_failed",
                payment_id="pay_failed",
                customer=SimpleNamespace(customer_id="cus_failed", email="guest@example.com"),
            ),
        ),
    )
    resolve_user = FakeResolveUser()
    provider = DodoProvider(
        get_client=lambda: client,
        config={"webhook_key": "test_wh_key_12345"},
        sink=sink,
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
    assert result.event_id == "evt_payment_failed"
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
        config={"webhook_key": "test_wh_key_12345"},
        sink=sink,
        resolve_user=None,
        logger=logger,
    )
    result = await provider.get_checkout_session_status("cks_1")
    assert result == {"paymentStatus": "requires_customer_action"}


@pytest.mark.asyncio
async def test_returns_none_for_missing_session(sink: FakeSink, logger: FakeLogger) -> None:
    class FakeCheckoutSessions:
        async def retrieve(self, _session_id: str) -> None:
            raise RuntimeError("not found")

    client = FakeClient(unwrap_result=None)
    client.checkout_sessions = FakeCheckoutSessions()
    provider = DodoProvider(
        get_client=lambda: client,
        config={"webhook_key": "test_wh_key_12345"},
        sink=sink,
        resolve_user=None,
        logger=logger,
    )
    result = await provider.get_checkout_session_status("cks_missing")
    assert result is None
