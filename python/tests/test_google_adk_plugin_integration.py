"""Real-Postgres lifecycle coverage for the Google ADK billing plugin."""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from bursar import CreditMetadata
from bursar.credits.postgres.store import PostgresStore
from bursar.credits.service import CreditsService
from bursar.integrations import ProviderReceipt
from bursar.integrations.google_adk import BursarPlugin
from bursar.metrics import UsageMetrics
from tests.test_store_integration import CONFIG

pytestmark = pytest.mark.integration


PLUGIN_USER_ID = "00000000-0000-0000-0000-000000000981"
PLUGIN_STATE_KEY = "_bursar_model_leases:integration:invocation-1"


def _estimate() -> UsageMetrics:
    return UsageMetrics(
        operation="completion",
        measures={"input_tokens": 10, "output_tokens": 4},
        dimensions={"model": "configured-model"},
    )


def _context(state: dict[str, object], *, user_id: str = PLUGIN_USER_ID) -> SimpleNamespace:
    session = SimpleNamespace(user_id=user_id, state=state)
    return SimpleNamespace(
        invocation_id="invocation-1",
        user_id=user_id,
        session=session,
        state=state,
    )


def _response(*, with_usage: bool = True) -> SimpleNamespace:
    usage = (
        SimpleNamespace(
            prompt_token_count=3,
            candidates_token_count=2,
            total_token_count=5,
        )
        if with_usage
        else None
    )
    return SimpleNamespace(
        partial=False,
        model_version="provider-model",
        usage_metadata=usage,
        get_function_calls=list,
    )


def _malformed_response() -> SimpleNamespace:
    def function_calls() -> list[object]:
        raise RuntimeError("provider tool metadata unavailable")

    return SimpleNamespace(
        partial=False,
        model_version="provider-model",
        usage_metadata=SimpleNamespace(
            prompt_token_count=True,
            candidates_token_count="not-a-number",
            total_token_count=None,
            cached_content_token_count=-1,
            thoughts_token_count=None,
        ),
        get_function_calls=function_calls,
    )


class BrokenReceiptSource:
    def begin(self) -> None:
        raise RuntimeError("receipt capture unavailable")

    def finish(self) -> None:
        raise RuntimeError("receipt capture unavailable")


class OneShotReceiptSource:
    def __init__(self, receipt: ProviderReceipt | None = None) -> None:
        self.receipt = receipt

    def begin(self) -> None:
        return None

    def finish(self) -> ProviderReceipt | None:
        receipt, self.receipt = self.receipt, None
        return receipt


def _receipt(*, invalid_metadata: bool = False) -> Any:
    metrics = UsageMetrics(
        operation="completion",
        measures={"input_tokens": 3, "output_tokens": 2},
        dimensions={"model": "provider-model"},
    )
    if invalid_metadata:
        return SimpleNamespace(
            metrics=metrics,
            metadata=SimpleNamespace(model_dump=lambda **_kwargs: {"trace_id": "invalid"}),
        )
    metadata = CreditMetadata(provider_request_id="provider-request")
    return ProviderReceipt(
        metrics=metrics,
        metadata=metadata,
    )


def _plugin(service: CreditsService, receipt_source: OneShotReceiptSource | None = None) -> BursarPlugin:
    return BursarPlugin(
        service,
        estimate=_estimate(),
        provider="openrouter",
        receipt_source=receipt_source,
        operation_key_prefix="integration",
        state_namespace="integration",
    )


def _service(store: PostgresStore, user_id: str = PLUGIN_USER_ID) -> CreditsService:
    service = CreditsService(store=store)
    service.publish_and_activate_catalog(deepcopy(CONFIG))
    service.add_credits(user_id, Decimal("100"), idempotency_key=f"adk-integration-funding:{user_id}")
    return service


@pytest.mark.asyncio
async def test_plugin_settles_real_postgres_model_callback(pg_store: PostgresStore) -> None:
    service = _service(pg_store)
    plugin = _plugin(service)
    state: dict[str, object] = {}
    context = _context(state)

    assert (
        await plugin.before_model_callback(
            callback_context=context,
            llm_request=SimpleNamespace(model="gpt-5"),
        )
        is None
    )
    await plugin.after_model_callback(callback_context=context, llm_response=_response())

    assert PLUGIN_STATE_KEY not in state
    assert service.get_available(PLUGIN_USER_ID).available == Decimal("95")
    usage = service.list_usage_entries(PLUGIN_USER_ID, limit=10)
    assert len(usage.items) == 1
    assert usage.items[0].entry_type == "usage"


@pytest.mark.asyncio
async def test_plugin_releases_real_postgres_reservation_on_agent_failure(pg_store: PostgresStore) -> None:
    service = _service(pg_store)
    plugin = _plugin(service)
    state: dict[str, object] = {}
    context = _context(state)

    await plugin.before_model_callback(callback_context=context, llm_request=SimpleNamespace(model="gpt-5"))
    await plugin.on_agent_error_callback(
        agent=object(),
        callback_context=context,
        error=RuntimeError("downstream agent failed"),
    )

    assert PLUGIN_STATE_KEY not in state
    assert service.get_available(PLUGIN_USER_ID).available == Decimal("100")
    assert service.list_usage_entries(PLUGIN_USER_ID, limit=10).items == []


@pytest.mark.asyncio
async def test_plugin_replays_durable_settlement_from_real_postgres_lease_state(
    pg_store: PostgresStore,
) -> None:
    service = _service(pg_store)
    plugin = _plugin(service)
    state: dict[str, object] = {}
    context = _context(state)

    await plugin.before_model_callback(callback_context=context, llm_request=SimpleNamespace(model="gpt-5"))
    pending = state[PLUGIN_STATE_KEY]
    assert isinstance(pending, list)
    pending[0]["metrics"] = UsageMetrics(
        operation="completion",
        measures={"input_tokens": 3, "output_tokens": 2},
        dimensions={"model": "gpt-5"},
    ).model_dump(mode="json")

    await plugin.before_run_callback(
        invocation_context=SimpleNamespace(
            invocation_id="invocation-1",
            user_id=PLUGIN_USER_ID,
            session=SimpleNamespace(user_id=PLUGIN_USER_ID, state=state),
        )
    )

    assert PLUGIN_STATE_KEY not in state
    assert service.get_available(PLUGIN_USER_ID).available == Decimal("95")
    assert len(service.list_usage_entries(PLUGIN_USER_ID, limit=10).items) == 1


@pytest.mark.asyncio
async def test_plugin_settles_estimate_when_provider_omits_usage_metadata(pg_store: PostgresStore) -> None:
    service = _service(pg_store)
    plugin = _plugin(service)
    state: dict[str, object] = {}
    context = _context(state)

    await plugin.before_model_callback(callback_context=context, llm_request=SimpleNamespace(model="gpt-5"))
    await plugin.after_model_callback(callback_context=context, llm_response=_response(with_usage=False))

    assert PLUGIN_STATE_KEY not in state
    assert service.get_available(PLUGIN_USER_ID).available == Decimal("86")


@pytest.mark.asyncio
async def test_plugin_releases_corrupt_durable_settlement_state(pg_store: PostgresStore) -> None:
    service = _service(pg_store)
    plugin = _plugin(service)
    state: dict[str, object] = {}
    context = _context(state)

    await plugin.before_model_callback(callback_context=context, llm_request=SimpleNamespace(model="gpt-5"))
    pending = state[PLUGIN_STATE_KEY]
    assert isinstance(pending, list)
    pending[0]["metrics"] = {
        "operation": "completion",
        "measures": {"input_tokens": "not-a-number"},
        "dimensions": {},
    }

    await plugin.before_run_callback(
        invocation_context=SimpleNamespace(
            invocation_id="invocation-1",
            user_id=PLUGIN_USER_ID,
            session=SimpleNamespace(user_id=PLUGIN_USER_ID, state=state),
        )
    )

    assert PLUGIN_STATE_KEY not in state
    assert service.get_available(PLUGIN_USER_ID).available == Decimal("100")
    assert service.list_usage_entries(PLUGIN_USER_ID, limit=10).items == []


@pytest.mark.asyncio
async def test_plugin_discards_provider_response_for_released_lease(pg_store: PostgresStore) -> None:
    service = _service(pg_store)
    plugin = _plugin(service)
    state: dict[str, object] = {}
    context = _context(state)

    await plugin.before_model_callback(callback_context=context, llm_request=SimpleNamespace(model="gpt-5"))
    pending = state[PLUGIN_STATE_KEY]
    assert isinstance(pending, list)
    service.release(PLUGIN_USER_ID, pending[0]["lease_id"])

    await plugin.after_model_callback(callback_context=context, llm_response=_response())

    assert PLUGIN_STATE_KEY not in state
    assert service.get_available(PLUGIN_USER_ID).available == Decimal("100")
    assert service.list_usage_entries(PLUGIN_USER_ID, limit=10).items == []


@pytest.mark.asyncio
async def test_plugin_denies_when_selector_and_admission_message_fail(pg_store: PostgresStore) -> None:
    service = _service(pg_store)

    def selector(_context: object, _request: object) -> bool:
        raise RuntimeError("selector backend unavailable")

    def message(_error: BaseException | None) -> str:
        raise RuntimeError("message backend unavailable")

    plugin = BursarPlugin(
        service,
        estimate=_estimate(),
        should_bill=selector,
        admission_message=message,
        state_namespace="integration-selector",
    )
    response = await plugin.before_model_callback(
        callback_context=_context({}),
        llm_request=SimpleNamespace(model="gpt-5"),
    )

    assert response is not None
    assert response.error_code == "ADMISSION_DENIED"
    assert response.error_message == "Billing service is temporarily unavailable. Please try again."
    assert service.list_usage_entries(PLUGIN_USER_ID, limit=10).items == []


@pytest.mark.asyncio
async def test_plugin_ignores_provider_receipt_capture_failure(pg_store: PostgresStore) -> None:
    service = _service(pg_store)
    plugin = BursarPlugin(
        service,
        estimate=_estimate(),
        receipt_source=BrokenReceiptSource(),
        state_namespace="integration-receipt-failure",
    )
    state: dict[str, object] = {}
    context = _context(state)

    await plugin.before_model_callback(callback_context=context, llm_request=SimpleNamespace(model="gpt-5"))
    await plugin.after_model_callback(callback_context=context, llm_response=_response())

    assert state == {}
    assert service.get_available(PLUGIN_USER_ID).available == Decimal("95")


@pytest.mark.asyncio
async def test_plugin_releases_a_reservation_when_the_run_fails(pg_store: PostgresStore) -> None:
    service = _service(pg_store)
    plugin = _plugin(service)
    state: dict[str, object] = {}
    context = _context(state)

    await plugin.before_model_callback(callback_context=context, llm_request=SimpleNamespace(model="gpt-5"))
    await plugin.on_run_error_callback(invocation_context=context, error=RuntimeError("run failed"))

    assert state == {}
    assert service.get_available(PLUGIN_USER_ID).available == Decimal("100")
    assert service.list_usage_entries(PLUGIN_USER_ID, limit=10).items == []


@pytest.mark.asyncio
async def test_plugin_falls_back_safely_for_malformed_provider_usage(pg_store: PostgresStore) -> None:
    service = _service(pg_store)
    plugin = _plugin(service)
    state: dict[str, object] = {}
    context = _context(state)

    await plugin.before_model_callback(callback_context=context, llm_request=SimpleNamespace(model="gpt-5"))
    await plugin.after_model_callback(callback_context=context, llm_response=_malformed_response())

    assert state == {}
    assert service.get_available(PLUGIN_USER_ID).available == Decimal("100")
    assert service.list_usage_entries(PLUGIN_USER_ID, limit=10).items == []


@pytest.mark.asyncio
async def test_plugin_settles_receipt_when_agent_callback_fails(pg_store: PostgresStore) -> None:
    service = _service(pg_store)
    source = OneShotReceiptSource()
    plugin = _plugin(service, source)
    state: dict[str, object] = {}
    context = _context(state)

    await plugin.before_model_callback(callback_context=context, llm_request=SimpleNamespace(model="gpt-5"))
    source.receipt = _receipt(invalid_metadata=True)
    await plugin.on_agent_error_callback(
        agent=object(),
        callback_context=context,
        error=RuntimeError("downstream agent failed after provider response"),
    )

    assert state == {}
    assert service.get_available(PLUGIN_USER_ID).available == Decimal("95")


@pytest.mark.asyncio
async def test_plugin_settles_receipt_when_run_error_escapes(pg_store: PostgresStore) -> None:
    service = _service(pg_store)
    source = OneShotReceiptSource()
    plugin = _plugin(service, source)
    state: dict[str, object] = {}
    context = _context(state)

    await plugin.before_model_callback(callback_context=context, llm_request=SimpleNamespace(model="gpt-5"))
    source.receipt = _receipt()
    await plugin.on_run_error_callback(invocation_context=context, error=RuntimeError("run failed"))

    assert state == {}
    assert service.get_available(PLUGIN_USER_ID).available == Decimal("95")


@pytest.mark.asyncio
async def test_plugin_discards_stale_receipt_before_reserving_next_call(pg_store: PostgresStore) -> None:
    service = _service(pg_store)
    source = OneShotReceiptSource(_receipt())
    plugin = _plugin(service, source)
    state: dict[str, object] = {}
    context = _context(state)

    await plugin.before_model_callback(callback_context=context, llm_request=SimpleNamespace(model="gpt-5"))
    await plugin.after_model_callback(callback_context=context, llm_response=_response())

    assert state == {}
    assert service.get_available(PLUGIN_USER_ID).available == Decimal("95")
    assert len(service.list_usage_entries(PLUGIN_USER_ID, limit=10).items) == 1
