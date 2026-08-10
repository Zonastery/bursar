from __future__ import annotations

import asyncio
import gc
import json
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from bursar.commerce import (
    AccountCreditDisplay,
    CommerceOptions,
    CommerceProviderFactoryContext,
    CreateCheckoutInput,
    InvalidOfferQuantityError,
    PlanChangePreviewResult,
    ProviderCapabilityNotSupportedError,
    ProviderSelectionError,
    QuoteChangedError,
    UnknownOfferError,
    classify_subscription_change,
)
from bursar.commerce.provider_registry import CommerceProviderRegistry
from bursar.config import SubscriptionOffer, load_config_from_dict
from bursar.providers.mock.provider import MockPaymentProvider

FIXTURE = json.loads((Path(__file__).parents[2] / "common" / "commerce-parity.json").read_text())


def test_shared_transition_classification_fixture() -> None:
    config = load_config_from_dict(FIXTURE["catalog"])
    for transition in FIXTURE["transitions"]:
        offer = config.commerce.offers[transition["target_offer"]]
        assert isinstance(offer, SubscriptionOffer)
        result = classify_subscription_change(
            config,
            transition["current_plan"],
            transition["current_interval"],
            offer,
        )
        assert result.classification == transition["classification"]
        assert (result.policy.effective if result.policy is not None else None) == transition["effective"]
        assert (result.policy.proration if result.policy is not None else None) == transition["proration"]


def test_shared_error_codes_and_provider_agnostic_public_contract() -> None:
    assert UnknownOfferError.code == FIXTURE["error_codes"]["unknown_offer"]
    assert InvalidOfferQuantityError.code == FIXTURE["error_codes"]["invalid_quantity"]
    assert QuoteChangedError.code == FIXTURE["error_codes"]["quote_changed"]
    assert ProviderCapabilityNotSupportedError.code == FIXTURE["error_codes"]["provider_capability"]
    assert FIXTURE["public_contract"] == {
        "offer_input": "offerKey",
        "quote_field": "quoteFingerprint",
        "provider_product_ids": "provider_internal",
    }


def test_public_commerce_types_hide_provider_product_and_quote_aliases() -> None:
    source = (Path(__file__).parents[1] / "src" / "bursar" / "commerce" / "types.py").read_text()
    assert "product_id" not in source
    assert "quote_hash" not in source


def test_public_commerce_models_reject_ambiguous_states() -> None:
    with pytest.raises(ValidationError):
        CommerceOptions(provider_environment="test", providers={})
    with pytest.raises(ValidationError):
        CommerceOptions(
            provider_environment="test",
            providers={" ": lambda context: MockPaymentProvider(event_sink=context.event_sink)},
        )
    with pytest.raises(ValidationError):
        PlanChangePreviewResult(
            unchanged=False,
            classification="upgrade",
            scheduled=False,
            plan_id="pro",
            interval="month",
        )
    with pytest.raises(ValidationError):
        PlanChangePreviewResult(
            unchanged=True,
            classification="upgrade",
            scheduled=False,
            plan_id="pro",
            interval="month",
        )
    with pytest.raises(ValidationError):
        AccountCreditDisplay(currency="USD", units_per_major=Decimal(0))
    with pytest.raises(ValidationError, match="account_id"):
        CreateCheckoutInput.model_validate(
            {
                "subject_id": "actor-1",
                "offer_key": "pro",
                "return_url": "https://app.example/return",
                "cancel_url": "https://app.example/cancel",
                "operation_key": "checkout-1",
            }
        )


@pytest.mark.asyncio
async def test_provider_registry_validates_default_and_deduplicates_loading() -> None:
    with pytest.raises(
        ProviderSelectionError,
        match="Default payment provider 'missing' is not registered",
    ):
        CommerceProviderRegistry(
            CommerceOptions(
                provider_environment="test",
                providers={"mock": lambda context: MockPaymentProvider(event_sink=context.event_sink)},
                default_provider="missing",
            ),
            CommerceProviderFactoryContext(provider_environment="test", event_sink=object()),  # type: ignore[reportArgumentType]
        )

    calls = 0

    async def factory(_context):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return MockPaymentProvider(event_sink=_context.event_sink)

    registry = CommerceProviderRegistry(
        CommerceOptions(provider_environment="test", providers={"mock": factory}),
        CommerceProviderFactoryContext(provider_environment="test", event_sink=object()),  # type: ignore[reportArgumentType]
    )
    first, second = await asyncio.gather(
        registry.get("mock"),
        registry.get("mock"),
    )

    assert first is second
    assert calls == 1
    assert registry.configured_providers == ["mock"]


@pytest.mark.asyncio
async def test_provider_registry_validates_factories_and_clear_invalidates_in_flight_loads() -> None:
    invalid = CommerceProviderRegistry(
        CommerceOptions(provider_environment="test", providers={"mock": lambda _context: object()}),  # type: ignore[dict-item]
        CommerceProviderFactoryContext(provider_environment="test", event_sink=object()),  # type: ignore[reportArgumentType]
    )
    with pytest.raises(ProviderSelectionError, match="did not return a valid payment provider"):
        await invalid.get("mock")

    calls = 0
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def factory(context):
        nonlocal calls
        calls += 1
        if calls == 1:
            first_started.set()
            await release_first.wait()
        return MockPaymentProvider(event_sink=context.event_sink)

    registry = CommerceProviderRegistry(
        CommerceOptions(provider_environment="test", providers={"mock": factory}),
        CommerceProviderFactoryContext(provider_environment="test", event_sink=object()),  # type: ignore[reportArgumentType]
    )
    stale_task = asyncio.create_task(registry.get("mock"))
    await first_started.wait()
    registry.clear()
    current = await registry.get("mock")
    release_first.set()
    stale = await stale_task

    assert stale is not current
    assert await registry.get("mock") is current
    assert calls == 2


@pytest.mark.asyncio
async def test_cancelling_one_provider_waiter_preserves_the_shared_factory_load() -> None:
    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def factory(context):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return MockPaymentProvider(event_sink=context.event_sink)

    registry = CommerceProviderRegistry(
        CommerceOptions(provider_environment="test", providers={"mock": factory}),
        CommerceProviderFactoryContext(provider_environment="test", event_sink=object()),  # type: ignore[reportArgumentType]
    )
    cancelled_waiter = asyncio.create_task(registry.get("mock"))
    await started.wait()
    cancelled_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_waiter

    surviving_waiter = asyncio.create_task(registry.get("mock"))
    release.set()
    provider = await surviving_waiter

    assert provider is await registry.get("mock")
    assert calls == 1


@pytest.mark.asyncio
async def test_cancelled_provider_waiter_does_not_leave_an_unobserved_factory_failure() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    reported: list[dict[str, object]] = []

    async def factory(_context):
        started.set()
        await release.wait()
        try:
            raise RuntimeError("provider factory failed")
        finally:
            finished.set()

    registry = CommerceProviderRegistry(
        CommerceOptions(provider_environment="test", providers={"mock": factory}),
        CommerceProviderFactoryContext(provider_environment="test", event_sink=object()),  # type: ignore[reportArgumentType]
    )
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: reported.append(context))
    try:
        waiter = asyncio.create_task(registry.get("mock"))
        await started.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

        release.set()
        await finished.wait()
        await asyncio.sleep(0)
        gc.collect()
        await asyncio.sleep(0)

        assert reported == []
    finally:
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
async def test_provider_registry_propagates_factory_failure_to_active_waiter() -> None:
    async def factory(_context):
        await asyncio.sleep(0)
        raise RuntimeError("provider factory failed")

    registry = CommerceProviderRegistry(
        CommerceOptions(provider_environment="test", providers={"mock": factory}),
        CommerceProviderFactoryContext(provider_environment="test", event_sink=object()),  # type: ignore[reportArgumentType]
    )

    with pytest.raises(RuntimeError, match="provider factory failed"):
        await registry.get("mock")
