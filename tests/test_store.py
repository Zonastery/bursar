"""Tests for store-level pricing operations."""

from __future__ import annotations

import pytest

from ducto import ConfigError, CreditManager, MemoryStore
from ducto.interface.models import PricingConfigData


def test_get_pricing_when_none() -> None:
    store = MemoryStore()
    result = store.get_active_pricing()
    assert result is None


def test_set_and_get_pricing() -> None:
    store = MemoryStore()
    config = PricingConfigData(
        version=1,
        models={"gpt-4": "input_tokens * 0.01"},
    )
    returned_id = store.set_active_pricing(config, label="v1")
    assert returned_id != ""

    result = store.get_active_pricing()
    assert result is not None
    assert result.config.models == {"gpt-4": "input_tokens * 0.01"}


def test_set_pricing_replaces_active() -> None:
    store = MemoryStore()
    c1 = PricingConfigData(version=1, models={"_default": "input_tokens * 1"})
    store.set_active_pricing(c1, label="first")

    c2 = PricingConfigData(version=1, models={"_default": "input_tokens * 2"})
    store.set_active_pricing(c2, label="second")

    result = store.get_active_pricing()
    assert result is not None
    assert result.config.models["_default"] == "input_tokens * 2"


def test_publish_pricing_from_dict_invalid_data() -> None:
    manager = CreditManager(store=MemoryStore())

    with pytest.raises(ConfigError):
        manager.publish_pricing_from_dict({"version": 1})


def test_load_pricing_file_yaml(tmp_path) -> None:
    """Load a YAML pricing file via _load_pricing_file."""
    from ducto.__main__ import _load_pricing_file

    f = tmp_path / "pricing.yaml"
    f.write_text("version: 1\nmodels:\n  _default: input_tokens * 1\n")
    data = _load_pricing_file(str(f))
    assert data["version"] == 1
    assert data["models"]["_default"] == "input_tokens * 1"


def test_load_pricing_file_json(tmp_path) -> None:
    """Load a JSON pricing file via _load_pricing_file."""
    from ducto.__main__ import _load_pricing_file

    f = tmp_path / "pricing.json"
    f.write_text('{"version": 1, "models": {"_default": "input_tokens * 1"}}')
    data = _load_pricing_file(str(f))
    assert data["version"] == 1
    assert data["models"]["_default"] == "input_tokens * 1"
