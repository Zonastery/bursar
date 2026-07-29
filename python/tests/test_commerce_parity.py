from __future__ import annotations

import json
from pathlib import Path

from bursar.commerce import (
    InvalidOfferQuantityError,
    ProviderCapabilityNotSupportedError,
    QuoteChangedError,
    UnknownOfferError,
    classify_subscription_change,
)
from bursar.config import SubscriptionOffer, load_config_from_dict

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
