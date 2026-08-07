from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types

from bursar import (
    BursarRetryOptions,
    CreditMetadata,
    QuotaExceededError,
    StoreUnavailableError,
    UsageMetrics,
)
from bursar.integrations import ProviderReceipt
from bursar.integrations.google_adk import BursarPlugin

USER_ID = "00000000-0000-0000-0000-000000000901"
LEASE_ID = "00000000-0000-0000-0000-000000000902"
SECOND_LEASE_ID = "00000000-0000-0000-0000-000000000903"
STATE_KEY = "_bursar_model_leases:test:invocation-1"


class FinalResponseLlm(BaseLlm):
    async def generate_content_async(
        self,
        llm_request: LlmRequest,
        stream: bool = False,
    ) -> AsyncGenerator[LlmResponse, None]:
        del llm_request, stream
        yield LlmResponse(
            model_version="provider-model",
            content=genai_types.Content(role="model", parts=[genai_types.Part(text="done")]),
            usage_metadata=genai_types.GenerateContentResponseUsageMetadata(
                prompt_token_count=10,
                candidates_token_count=4,
                total_token_count=14,
                cached_content_token_count=2,
                thoughts_token_count=1,
            ),
        )


@dataclass
class ReceiptSource:
    receipt: ProviderReceipt | None = None
    begin_calls: int = 0
    finish_calls: int = 0

    def begin(self) -> None:
        self.begin_calls += 1

    def finish(self) -> ProviderReceipt | None:
        self.finish_calls += 1
        receipt, self.receipt = self.receipt, None
        return receipt


def _estimate() -> UsageMetrics:
    return UsageMetrics(
        operation="completion",
        measures={
            "calls": 1,
            "input_tokens": 8_000,
            "output_tokens": 4_096,
            "total_tokens": 12_096,
            "cache_read_tokens": 0,
            "reasoning_tokens": 0,
            "tool_calls": 0,
            "provider_cost_usd_micros": 0,
        },
        dimensions={"model": "configured-model", "provider": "openrouter"},
    )


def _plugin(
    credits: MagicMock,
    receipt_source: ReceiptSource | None = None,
    **kwargs,
) -> BursarPlugin:
    return BursarPlugin(
        credits,
        estimate=_estimate(),
        operation_type="completion",
        feature="chat",
        provider="openrouter",
        receipt_source=receipt_source,
        reference_type="chat",
        operation_key_prefix="chat",
        state_namespace="test",
        retry_options=BursarRetryOptions(max_attempts=1, base_delay_seconds=0, max_delay_seconds=0),
        **kwargs,
    )


def _context(state: dict | None = None, *, user_id: str | None = USER_ID):
    state = {} if state is None else state
    return SimpleNamespace(
        invocation_id="invocation-1",
        user_id=user_id,
        session=SimpleNamespace(user_id=user_id, state=state),
        state=state,
    )


def _invocation(state: dict, *, user_id: str | None = USER_ID):
    return SimpleNamespace(
        invocation_id="invocation-1",
        user_id=user_id,
        session=SimpleNamespace(user_id=user_id, state=state),
    )


def _request(model: str = "request-model"):
    return SimpleNamespace(model=model)


def _response(*, partial: bool = False):
    function_calls = [SimpleNamespace(name="web_search"), SimpleNamespace(name="python_exec")]
    return SimpleNamespace(
        partial=partial,
        model_version="response-model",
        usage_metadata=SimpleNamespace(
            prompt_token_count=10,
            candidates_token_count=4,
            total_token_count=14,
            cached_content_token_count=2,
            thoughts_token_count=1,
        ),
        get_function_calls=lambda: function_calls,
    )


def _provider_receipt() -> ProviderReceipt:
    return ProviderReceipt(
        metrics=UsageMetrics(
            operation="completion",
            measures={
                "calls": 1,
                "input_tokens": 11,
                "output_tokens": 5,
                "total_tokens": 16,
                "cache_read_tokens": 3,
                "reasoning_tokens": 2,
                "provider_cost_usd_micros": 1_200,
                "latency_ms": 900,
            },
            dimensions={
                "model": "provider-model",
                "provider": "openrouter",
                "region": "iad",
            },
        ),
        metadata=CreditMetadata(
            provider_request_id="generation-1",
            trace_id="a" * 32,
            span_id="b" * 16,
        ),
    )


@pytest.mark.asyncio
async def test_plugin_reserves_and_settles_authoritative_provider_receipt() -> None:
    credits = MagicMock()
    credits.begin_billed_operation.return_value = SimpleNamespace(lease_id=LEASE_ID)
    operation = MagicMock()
    operation.settle.return_value = SimpleNamespace(amount=Decimal("7"), usage_charge_id="usage-1")
    credits.resume_billed_operation.return_value = operation
    receipts = ReceiptSource()
    plugin = _plugin(credits, receipts)
    context = _context()

    assert await plugin.before_model_callback(callback_context=context, llm_request=_request()) is None
    receipts.receipt = _provider_receipt()
    await plugin.after_model_callback(callback_context=context, llm_response=_response())

    _, options = credits.begin_billed_operation.call_args.args
    assert options.operation_key.startswith("chat:invocation-1:")
    assert options.operation_type == "completion"
    assert options.feature == "chat"
    assert options.estimate.dimensions == {"model": "request-model", "provider": "openrouter"}
    assert receipts.begin_calls == 1

    _, settled_lease_id, operation_key = credits.resume_billed_operation.call_args.args
    assert settled_lease_id == LEASE_ID
    assert operation_key == options.operation_key
    actual = operation.settle.call_args.args[0]
    assert actual.measures["input_tokens"] == 11
    assert actual.measures["provider_cost_usd_micros"] == 1_200
    assert actual.measures["tool_calls"] == 2
    assert "latency_ms" not in actual.measures
    assert actual.dimensions == {"model": "provider-model", "provider": "openrouter"}
    metadata = credits.resume_billed_operation.call_args.kwargs["metadata"]
    assert metadata.reference_type == "chat"
    assert metadata.reference_id == "invocation-1"
    assert metadata.provider_request_id == "generation-1"
    assert metadata.trace_id == "a" * 32
    assert STATE_KEY not in context.state


@pytest.mark.asyncio
async def test_plugin_waits_for_the_final_stream_response() -> None:
    credits = MagicMock()
    credits.begin_billed_operation.return_value = SimpleNamespace(lease_id=LEASE_ID)
    receipts = ReceiptSource()
    plugin = _plugin(credits, receipts)
    context = _context()

    await plugin.before_model_callback(callback_context=context, llm_request=_request())
    await plugin.after_model_callback(callback_context=context, llm_response=_response(partial=True))

    credits.resume_billed_operation.assert_not_called()
    assert receipts.finish_calls == 1  # stale-capture check before reservation only
    assert context.state[STATE_KEY][0]["lease_id"] == LEASE_ID


@pytest.mark.asyncio
async def test_plugin_releases_an_unbilled_model_error() -> None:
    credits = MagicMock()
    credits.begin_billed_operation.return_value = SimpleNamespace(lease_id=LEASE_ID)
    receipts = ReceiptSource()
    plugin = _plugin(credits, receipts)
    context = _context()

    await plugin.before_model_callback(callback_context=context, llm_request=_request())
    await plugin.on_model_error_callback(
        callback_context=context,
        llm_request=_request(),
        error=RuntimeError("provider unavailable"),
    )

    credits.release.assert_called_once_with(USER_ID, LEASE_ID)
    credits.resume_billed_operation.assert_not_called()
    assert STATE_KEY not in context.state


@pytest.mark.asyncio
async def test_plugin_settles_a_provider_receipt_even_when_adk_reports_an_error() -> None:
    credits = MagicMock()
    credits.begin_billed_operation.return_value = SimpleNamespace(lease_id=LEASE_ID)
    operation = MagicMock()
    operation.settle.return_value = SimpleNamespace(amount=Decimal("2"), usage_charge_id=None)
    credits.resume_billed_operation.return_value = operation
    receipts = ReceiptSource()
    plugin = _plugin(credits, receipts)
    context = _context()

    await plugin.before_model_callback(callback_context=context, llm_request=_request())
    receipts.receipt = _provider_receipt()
    await plugin.on_model_error_callback(
        callback_context=context,
        llm_request=_request(),
        error=RuntimeError("post-provider failure"),
    )

    operation.settle.assert_called_once()
    credits.release.assert_not_called()
    assert STATE_KEY not in context.state


@pytest.mark.asyncio
async def test_plugin_returns_an_adk_response_when_admission_is_denied() -> None:
    credits = MagicMock()
    credits.begin_billed_operation.side_effect = QuotaExceededError("quota")
    plugin = _plugin(credits, admission_message=lambda _error: "Chat quota reached.")

    response = await plugin.before_model_callback(callback_context=_context(), llm_request=_request())

    assert response is not None
    assert response.error_code == "ADMISSION_DENIED"
    assert response.error_message == "Chat quota reached."
    assert response.content is not None
    assert response.content.parts is not None
    assert response.content.parts[0].text == "Chat quota reached."


@pytest.mark.asyncio
async def test_plugin_denies_a_billable_call_without_a_subject() -> None:
    credits = MagicMock()
    plugin = _plugin(credits)

    response = await plugin.before_model_callback(
        callback_context=_context(user_id=None),
        llm_request=_request(),
    )

    assert response is not None
    assert response.error_code == "ADMISSION_DENIED"
    credits.begin_billed_operation.assert_not_called()


@pytest.mark.asyncio
async def test_plugin_replays_an_indeterminate_settlement_with_original_metrics() -> None:
    credits = MagicMock()
    credits.begin_billed_operation.return_value = SimpleNamespace(lease_id=LEASE_ID)
    operation = MagicMock()
    operation.settle.side_effect = StoreUnavailableError("database unavailable")
    credits.resume_billed_operation.return_value = operation
    plugin = _plugin(credits)
    context = _context()

    await plugin.before_model_callback(callback_context=context, llm_request=_request())
    await plugin.after_model_callback(callback_context=context, llm_response=_response())

    assert context.state[STATE_KEY][0]["metrics"]["measures"]["input_tokens"] == "10"
    operation.settle.side_effect = None
    operation.settle.return_value = SimpleNamespace(amount=Decimal("1"), usage_charge_id="usage-replayed")

    await plugin.before_run_callback(invocation_context=_invocation(context.state))

    assert operation.settle.call_count == 2
    assert STATE_KEY not in context.state


@pytest.mark.asyncio
async def test_plugin_releases_a_hold_when_a_later_callback_short_circuits() -> None:
    credits = MagicMock()
    credits.begin_billed_operation.return_value = SimpleNamespace(lease_id=LEASE_ID)
    plugin = _plugin(credits)
    context = _context()

    await plugin.before_model_callback(callback_context=context, llm_request=_request())
    await plugin.after_run_callback(invocation_context=_invocation(context.state))

    credits.release.assert_called_once_with(USER_ID, LEASE_ID)
    assert STATE_KEY not in context.state


@pytest.mark.asyncio
async def test_plugin_can_exclude_models_without_creating_a_lease() -> None:
    credits = MagicMock()
    plugin = _plugin(credits, should_bill=lambda _context, request: request.model == "billable")

    assert await plugin.before_model_callback(callback_context=_context(), llm_request=_request("free")) is None

    credits.begin_billed_operation.assert_not_called()


@pytest.mark.asyncio
async def test_plugin_runs_through_the_real_adk_app_lifecycle() -> None:
    credits = MagicMock()
    credits.begin_billed_operation.return_value = SimpleNamespace(lease_id=LEASE_ID)
    operation = MagicMock()
    operation.settle.return_value = SimpleNamespace(amount=Decimal("1"), usage_charge_id="usage-adk")
    credits.resume_billed_operation.return_value = operation
    runner = Runner(
        app=App(
            name="bursar_plugin_test",
            root_agent=Agent(name="billing_agent", model=FinalResponseLlm(model="request-model")),
            plugins=[_plugin(credits)],
        ),
        session_service=InMemorySessionService(),
        auto_create_session=True,
    )

    try:
        events = [
            event
            async for event in runner.run_async(
                user_id=USER_ID,
                session_id="session-1",
                invocation_id="invocation-1",
                new_message=genai_types.Content(role="user", parts=[genai_types.Part(text="hello")]),
            )
        ]
    finally:
        await runner.close()

    assert events[-1].content is not None
    assert events[-1].content.parts is not None
    assert events[-1].content.parts[0].text == "done"
    actual = operation.settle.call_args.args[0]
    assert actual.measures["input_tokens"] == 10
    assert actual.measures["output_tokens"] == 4
    assert actual.measures["cache_read_tokens"] == 2
    assert actual.measures["reasoning_tokens"] == 1
    session = await runner.session_service.get_session(
        app_name="bursar_plugin_test",
        user_id=USER_ID,
        session_id="session-1",
    )
    assert session is not None
    assert session.state.get(STATE_KEY) in (None, [])
