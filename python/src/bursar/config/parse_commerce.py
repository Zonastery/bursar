"""Commerce config parser — mirrors JS SDK's ``config/parse-commerce.ts``."""

from __future__ import annotations

from bursar.config.types import (
    CommerceConfig,
    CustomObjectReference,
    CustomProvider,
    DodoProductReference,
    DodoProvider,
    ProviderReference,
    StripePriceReference,
    StripeProvider,
    _validate_map_keys,
)


def has_provider_binding(reference: ProviderReference, environment: str) -> bool:
    """Return whether a reference has an ID in the selected environment."""
    if isinstance(reference, StripePriceReference):
        value = reference.price_id
    elif isinstance(reference, DodoProductReference):
        value = reference.product_id
    else:
        value = reference.external_id
    return isinstance(value, str) or environment in value


def _validate_commerce(commerce: CommerceConfig) -> CommerceConfig:
    _validate_map_keys(commerce.providers, "commerce.providers")
    _validate_map_keys(commerce.offers, "commerce.offers")
    seen_provider_objects: set[tuple[str, str, str]] = set()
    for offer_key, offer in commerce.offers.items():
        for provider_key, reference in offer.providers.items():
            provider = commerce.providers.get(provider_key)
            if provider is None:
                raise ValueError(f"commerce.offers.{offer_key} references unknown provider '{provider_key}'")
            compatible = (
                (isinstance(provider, StripeProvider) and isinstance(reference, StripePriceReference))
                or (isinstance(provider, DodoProvider) and isinstance(reference, DodoProductReference))
                or (isinstance(provider, CustomProvider) and isinstance(reference, CustomObjectReference))
            )
            if not compatible:
                raise ValueError(
                    f"commerce.offers.{offer_key}.providers.{provider_key} has an incompatible provider reference"
                )
            if isinstance(reference, StripePriceReference):
                external_id = reference.price_id
            elif isinstance(reference, DodoProductReference):
                external_id = reference.product_id
            else:
                external_id = reference.external_id
            bindings = {"*": external_id} if isinstance(external_id, str) else external_id
            for environment, value in bindings.items():
                object_key = (provider_key, environment, value)
                if object_key in seen_provider_objects:
                    raise ValueError(f"duplicate provider object reference {provider_key}/{environment}/{value}")
                seen_provider_objects.add(object_key)
    return commerce
