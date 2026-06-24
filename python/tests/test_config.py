"""Tests for pricing config parsing and validation."""

import pytest
from pydantic import ValidationError

from ducto.config import (
    ConfigError,
    PricingConfig,
    load_config_from_dict,
)
from ducto.interface.models import PricingConfigData


class TestConfigValidation:
    """Tests for config loading and validation."""

    def test_valid_full_config(self) -> None:
        """Loading a full config dict populates all sections."""
        config = load_config_from_dict(
            {
                "models": {"gpt-4": "input_tokens * 0.01"},
                "tools": {"_default": "tool_calls * 0.1"},
            }
        )
        assert config.models["gpt-4"] == "input_tokens * 0.01"

    def test_minimal_config(self) -> None:
        """Minimal config with only version and models works."""
        config = load_config_from_dict(
            {
                "models": {"_default": "input_tokens * 0.001"},
            }
        )
        assert config.models["_default"] == "input_tokens * 0.001"

    def test_invalid_expression_raises_error(self) -> None:
        """An expression with disallowed syntax raises ConfigError."""
        with pytest.raises(ConfigError, match="invalid expression"):
            load_config_from_dict(
                {
                    "models": {"gpt-4": "lambda x: x"},
                }
            )

    def test_missing_models_raises_error(self) -> None:
        """Missing models section raises ConfigError."""
        with pytest.raises(ConfigError, match="models"):
            load_config_from_dict({})

    def test_negative_fixed_cost_raises_error(self) -> None:
        """Negative fixed cost values raise pydantic ValidationError."""
        with pytest.raises(ValidationError):
            load_config_from_dict(
                {
                    "models": {"_default": "input_tokens * 1"},
                    "fixed": {"bad_job": -5},
                }
            )

    def test_tool_specific_costs(self) -> None:
        """Tool-specific expression strings are stored correctly."""
        config = load_config_from_dict(
            {
                "models": {"_default": "input_tokens * 1"},
                "tools": {"_default": "tool_calls * 0", "web_search": "web_search_calls * 2"},
            }
        )
        assert config.tools["web_search"] == "web_search_calls * 2"

    def test_fixed_costs_are_positive(self) -> None:
        """Positive fixed cost values are accepted."""
        config = load_config_from_dict(
            {
                "models": {"_default": "input_tokens * 1"},
                "fixed": {"batch_job": 20, "slow_job": 10},
            }
        )
        assert config.fixed["batch_job"] == 20

    def test_pricing_config_field_alignment(self) -> None:
        """PricingConfig and PricingConfigData fields stay in sync.

        Prevents silent data loss when fields are added to one model but not the other.
        The validated config (PricingConfig) and raw data model (PricingConfigData)
        must share the same set of fields for reliable round-trip through stores.
        """
        config_fields = set(PricingConfig.model_fields.keys())
        data_fields = set(PricingConfigData.model_fields.keys())
        assert config_fields == data_fields, (
            f"Field drift: PricingConfig has {config_fields - data_fields}, "
            f"PricingConfigData has {data_fields - config_fields}"
        )
