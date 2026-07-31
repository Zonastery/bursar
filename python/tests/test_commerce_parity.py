from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from bursar.commerce import (
    CommerceOptions,
    CommerceProviderFactoryContext,
    CommerceProviderRegistry,
    InvalidOfferQuantityError,
    ProviderCapabilityNotSupportedError,
    ProviderSelectionError,
    QuoteChangedError,
    UnknownOfferError,
    classify_subscription_change,
)
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


@pytest.mark.asyncio
async def test_provider_registry_validates_default_and_deduplicates_loading() -> None:
    with pytest.raises(
        ProviderSelectionError,
        match="Default payment provider 'missing' is not registered",
    ):
        CommerceProviderRegistry(
            CommerceOptions(
                providers={"mock": lambda context: MockPaymentProvider(context.event_sink)},
                default_provider="missing",
            ),
            CommerceProviderFactoryContext(event_sink=object()),  # type: ignore[reportArgumentType]
        )

    calls = 0

    async def factory(_context):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return MockPaymentProvider(_context.event_sink)

    registry = CommerceProviderRegistry(
        CommerceOptions(providers={"mock": factory}),
        CommerceProviderFactoryContext(event_sink=object()),  # type: ignore[reportArgumentType]
    )
    first, second = await asyncio.gather(
        registry.get("mock"),
        registry.get("mock"),
    )

    assert first is second
    assert calls == 1
    assert registry.configured_providers == ["mock"]
