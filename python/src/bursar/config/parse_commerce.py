"""Commerce config parser — mirrors JS SDK's ``config/parse-commerce.ts``."""

from __future__ import annotations

from bursar.config.types import (
    CommerceConfig,
    _validate_map_keys,
)


def _validate_commerce(commerce: CommerceConfig) -> CommerceConfig:
    _validate_map_keys(commerce.providers, "commerce.providers")
    _validate_map_keys(commerce.offers, "commerce.offers")
    return commerce
